"""Treat photos: frame selection, real H.264 decoding, and the full capture path.

The end-to-end test encodes a real H.264 stream whose colour changes every
second (red, green, blue, white), plays it to the camera-link code frame by
frame at 25 fps through a fake TUTK library, tosses a treat mid-stream, and
checks that the photos 1 and 2 seconds after the toss show the colours that
were on screen at those moments.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from furbo_p2p import (
    CMD_REJECTED,
    IPCAM_START,
    IPCAM_STOP,
    CameraLink,
    Frame,
    P2PCredentials,
    P2PError,
    clip_ending_at,
)
import photos

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")

FPS = 25
COLOURS = ("red", "green", "blue", "white")  # one second each
EXPECTED_RGB = {"red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255), "white": (255, 255, 255)}


# --- frame selection ------------------------------------------------------------


def test_clip_starts_at_last_keyframe_before_the_moment():
    frames = [
        Frame(0.0, True, b"K0"),
        Frame(0.1, False, b"p1"),
        Frame(0.2, True, b"K2"),
        Frame(0.3, False, b"p3"),
        Frame(0.4, False, b"p4"),
    ]
    assert clip_ending_at(frames, 0.35) == b"K2p3"
    assert clip_ending_at(frames, 0.15) == b"K0p1"
    assert clip_ending_at(frames, 10) == b"K2p3p4"
    assert clip_ending_at(frames, -1) is None
    assert clip_ending_at([Frame(0.0, False, b"p")], 1) is None


# --- a real H.264 stream ----------------------------------------------------------


def _encode_stream(tmp: Path) -> bytes:
    inputs = []
    for colour in COLOURS:
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r={FPS}:d=1"]
    out = tmp / "stream.h264"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", *inputs,
            "-filter_complex", "[0][1][2][3]concat=n=4:v=1,format=yuv420p",
            "-c:v", "libx264", "-bf", "0",
            # A keyframe every 10 frames, and an access unit delimiter per frame
            # so the stream can be cut into frames as the camera delivers them.
            "-x264-params", "keyint=10:min-keyint=10:scenecut=0:aud=1",
            "-f", "h264", str(out),
        ],
        check=True,
    )  # fmt: skip
    return out.read_bytes()


def _split_frames(stream: bytes) -> list[tuple[bool, bytes]]:
    aud = b"\x00\x00\x00\x01\x09"
    starts = []
    i = stream.find(aud)
    while i != -1:
        starts.append(i)
        i = stream.find(aud, i + 1)
    frames = []
    for a, b in zip(starts, [*starts[1:], len(stream)], strict=True):
        unit = stream[a:b]
        nal_types = {unit[j + 3] & 0x1F for j in range(len(unit) - 3) if unit[j : j + 3] == b"\x00\x00\x01"}
        frames.append((5 in nal_types, unit))
    return frames


def _dominant_rgb(jpeg: bytes) -> tuple[int, int, int]:
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        input=jpeg, capture_output=True, check=True,
    ).stdout  # fmt: skip
    return tuple(raw[:3])  # type: ignore[return-value]


def _closest(rgb: tuple[int, int, int]) -> str:
    return min(
        EXPECTED_RGB, key=lambda c: sum((a - b) ** 2 for a, b in zip(rgb, EXPECTED_RGB[c], strict=True))
    )


@pytest.fixture(scope="module")
def stream_frames(tmp_path_factory) -> list[tuple[bool, bytes]]:
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    return _split_frames(_encode_stream(tmp_path_factory.mktemp("h264")))


@needs_ffmpeg
def test_stream_fixture_is_what_we_think(stream_frames):
    assert len(stream_frames) == FPS * len(COLOURS)
    assert [i for i, (key, _) in enumerate(stream_frames) if key][:3] == [0, 10, 20]


@needs_ffmpeg
def test_decoder_returns_the_last_picture(stream_frames):
    # Frames 30..39 are a full GOP in the second second (green).
    clip = b"".join(data for _, data in stream_frames[30:40])
    jpeg = photos.last_picture_jpeg(clip)
    assert jpeg and jpeg[:2] == b"\xff\xd8"
    assert _closest(_dominant_rgb(jpeg)) == "green"


def test_decoder_failure_is_none_not_an_error():
    assert photos.last_picture_jpeg(b"not video") is None


# --- the capture path, against a fake camera ------------------------------------------


class FakeVideoLib:
    """Plays a frame list in real time once video starts, and acks the toss."""

    def __init__(self, frames, *, ack=True, reject=False, video_ok=True):
        self.frames = frames
        self.ack, self.reject, self.video_ok = ack, reject, video_ok
        self.started_at: float | None = None
        self.sent: list[int] = []
        self.next_frame = 0
        self.replies: list[tuple[int, bytes]] = []

    # session plumbing
    def IOTC_Get_SessionID(self):
        return 1

    def IOTC_Connect_ByUIDEx(self, *a):
        return 1

    def avClientStartEx(self, *a):
        return 2

    def avSendIOCtrlExit(self, *a):
        pass

    def avClientStop(self, *a):
        pass

    def IOTC_Connect_Stop_BySID(self, *a):
        pass

    def IOTC_Session_Close(self, *a):
        pass

    def avSendIOCtrl(self, chan, opcode, data, size):
        op = opcode.value
        self.sent.append(op)
        if op == IPCAM_START and self.video_ok:
            self.started_at = time.monotonic()
        if op == 131079:
            if self.reject:
                self.replies.append((CMD_REJECTED, bytes([3, 0x07, 0x00, 0, 0])))
            elif self.ack:
                self.replies.append((131080, b"\x00"))
        return 0

    def avRecvIOCtrl(self, chan, opcode_ref, buf, size, timeout):
        if not self.replies:
            return -20011
        code, data = self.replies.pop(0)
        opcode_ref._obj.value = code
        ctypes.memmove(buf, data, len(data))
        return len(data)

    def avRecvFrameData2(self, chan, buf, size, out_size, expected, info, info_size, isz, frame_no):
        if self.started_at is None or self.next_frame >= len(self.frames):
            return -20012
        if time.monotonic() < self.started_at + self.next_frame / FPS:
            return -20012
        key, data = self.frames[self.next_frame]
        self.next_frame += 1
        ctypes.memmove(buf, data, len(data))
        out_size._obj.value = len(data)
        info._obj.is_keyframe = 1 if key else 0
        return 0


CREDS = P2PCredentials("Cam", "UID", "k" * 8, "a", "p", False, "v3")


def open_link(lib) -> CameraLink:
    link = CameraLink(lib, CREDS)
    link.open()
    return link


@needs_ffmpeg
def test_photos_show_the_scene_one_and_two_seconds_after_the_toss(stream_frames):
    lib = FakeVideoLib(stream_frames)
    outcome = open_link(lib).toss_with_photos()

    assert outcome.result == "acknowledged"
    assert outcome.photo_error is None
    assert set(outcome.clips) == {1.0, 2.0}
    # Video started before the toss and stopped after.
    assert lib.sent.index(IPCAM_START) < lib.sent.index(131079) < lib.sent.index(IPCAM_STOP)

    # The toss went out after the 2nd keyframe (0.4 s into the stream), so the
    # photos are from about 1.4 s (green) and 2.4 s (blue).
    seen = [_closest(_dominant_rgb(photos.last_picture_jpeg(outcome.clips[o]))) for o in (1.0, 2.0)]
    assert seen == ["green", "blue"]


def test_camera_with_no_picture_still_tosses():
    lib = FakeVideoLib([], video_ok=True)
    outcome = open_link(lib).toss_with_photos(settle=0.3)
    assert outcome.result == "acknowledged"
    assert outcome.clips == {}
    assert "no picture" in outcome.photo_error
    assert IPCAM_STOP in lib.sent


def test_rejected_toss_raises_and_stops_video():
    lib = FakeVideoLib([], reject=True)
    with pytest.raises(P2PError, match="rejected"):
        open_link(lib).toss_with_photos(settle=0.2)
    assert IPCAM_STOP in lib.sent


# --- regressions from the bug check ---------------------------------------------------


def _without_headers(frames, keep: set[int]):
    """The stream as a camera that sends SPS/PPS only on some keyframes would."""
    from furbo_p2p import PPS, SPS, _nal_type, _nal_units

    out = []
    for i, (key, data) in enumerate(frames):
        kept = data
        if key and i not in keep:
            kept = b"".join(u for u in _nal_units(data) if _nal_type(u) not in (SPS, PPS))
        out.append((key, kept))
    return out


@needs_ffmpeg
def test_photos_decode_when_keyframes_lack_headers(stream_frames):
    # Headers only on the first full-size keyframe (frame 10).
    frames = _without_headers(stream_frames, keep={0, 10})
    bare = b"".join(d for _, d in frames[30:40])
    assert photos.last_picture_jpeg(bare) is None  # proof the stream really lacks them
    outcome = open_link(FakeVideoLib(frames)).toss_with_photos()
    seen = [_closest(_dominant_rgb(photos.last_picture_jpeg(outcome.clips[o]))) for o in (1.0, 2.0)]
    assert seen == ["green", "blue"]


def test_clip_prefixes_latest_headers_before_its_keyframe():
    sps_a, pps_a = b"\x00\x00\x00\x01\x67A", b"\x00\x00\x00\x01\x68A"
    idr = b"\x00\x00\x00\x01\x65I"
    frames = [
        Frame(0.0, True, sps_a + pps_a + idr),
        Frame(0.1, False, b"\x00\x00\x00\x01\x41p"),
        Frame(0.2, True, idr),  # no headers of its own
        Frame(0.3, False, b"\x00\x00\x00\x01\x41q"),
    ]
    assert clip_ending_at(frames, 0.35) == sps_a + pps_a + idr + b"\x00\x00\x00\x01\x41q"
    assert clip_ending_at(frames, 0.05) == sps_a + pps_a + idr  # has its own: nothing added


class StaleThenLive(FakeVideoLib):
    """Frames left over from the last toss sit in the buffer before video starts."""

    def __init__(self, frames, stale):
        super().__init__(frames)
        self.stale = list(stale)

    def avRecvFrameData2(self, chan, buf, size, out_size, expected, info, *rest):
        if self.started_at is None and self.stale:
            key, data = self.stale.pop(0)
            ctypes.memmove(buf, data, len(data))
            out_size._obj.value = len(data)
            info._obj.is_keyframe = 1 if key else 0
            return 0
        return super().avRecvFrameData2(chan, buf, size, out_size, expected, info, *rest)


@needs_ffmpeg
def test_leftover_frames_from_the_last_toss_are_not_used(stream_frames):
    # 40 frames of white left over from the previous toss's video.
    lib = StaleThenLive(stream_frames, stale=stream_frames[75:100] * 2)
    outcome = open_link(lib).toss_with_photos()
    assert lib.stale == []
    seen = [_closest(_dominant_rgb(photos.last_picture_jpeg(outcome.clips[o]))) for o in (1.0, 2.0)]
    assert seen == ["green", "blue"]


def test_slow_full_size_keyframe_never_mixes_in_the_preview():
    # A preview keyframe, then P-frames, and the full-size keyframe only 2 s in.
    # The treat goes after the 0.6 s settle (about 0.6 s in), so at +1 s there's
    # nothing valid to use, and the +2 s photo starts at the full-size keyframe.
    frames = [(True, b"PREVIEW")] + [(False, b"p")] * 49 + [(True, b"FULLKEY")] + [(False, b"q")] * 60
    outcome = open_link(FakeVideoLib(frames)).toss_with_photos(settle=0.6)
    assert all(b"PREVIEW" not in clip for clip in outcome.clips.values())
    assert 1.0 not in outcome.clips
    assert outcome.clips[2.0].startswith(b"FULLKEY")


@needs_ffmpeg
def test_three_photos_including_plus_three_seconds(stream_frames):
    outcome = open_link(FakeVideoLib(stream_frames)).toss_with_photos((1.0, 2.0, 3.0))
    seen = [_closest(_dominant_rgb(photos.last_picture_jpeg(outcome.clips[o]))) for o in (1.0, 2.0, 3.0)]
    assert seen == ["green", "blue", "white"]


@needs_ffmpeg
def test_only_the_three_second_photo(stream_frames):
    outcome = open_link(FakeVideoLib(stream_frames)).toss_with_photos((3.0,))
    assert set(outcome.clips) == {3.0}
    assert _closest(_dominant_rgb(photos.last_picture_jpeg(outcome.clips[3.0]))) == "white"
