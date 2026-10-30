"""Turns a short H.264 clip into a JPEG of its last picture, with ffmpeg."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
import tempfile

_LOGGER = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
TIMEOUT = 20  # seconds; a 1080p clip of a few seconds decodes in well under one


def last_picture_jpeg(clip: bytes) -> bytes | None:
    """The final decoded picture of an Annex B H.264 clip, as JPEG; None on failure.

    Blocking: run it in a worker thread.
    """
    with tempfile.TemporaryDirectory(prefix="furbo-photo-") as tmp:
        src, out = Path(tmp) / "clip.h264", Path(tmp) / "photo.jpg"
        src.write_bytes(clip)
        try:
            proc = subprocess.run(
                [
                    FFMPEG,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "h264",
                    "-i",
                    str(src),
                    # Write each decoded picture over the last: what remains is the final one.
                    "-update",
                    "1",
                    "-q:v",
                    "3",
                    "-y",
                    str(out),
                ],
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            _LOGGER.warning("Could not run ffmpeg for a treat photo: %s", err)
            return None
        if not out.exists() or out.stat().st_size == 0:
            _LOGGER.warning(
                "ffmpeg made no treat photo (exit %s): %s",
                proc.returncode,
                proc.stderr.decode(errors="replace").strip()[-300:],
            )
            return None
        return out.read_bytes()
