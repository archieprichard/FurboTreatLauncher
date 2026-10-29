"""Link credentials and the treat command, against a fake TUTK library."""

from __future__ import annotations

import ctypes

import pytest

from furbo_p2p import CMD_REJECTED, CameraLink, P2PCredentials, P2PError, build_credentials

DEVICE = {"Id": "d", "DeviceName": "Cam", "ProductId": "FB0030", "P2PUuid": "UID1"}


def test_modern_credentials():
    creds = build_credentials(DEVICE, {"AuthKey": "k" * 8, "P2PAccountId": "a", "P2PAccountKey": "p"}, "acct")
    assert (creds.auth_key, creds.account, creds.password, creds.legacy, creds.proto) == (
        "k" * 8,
        "a",
        "p",
        False,
        "v3",
    )


def test_legacy_credentials():
    device = {**DEVICE, "ProductId": "FB002", "P2PAccessToken": "tok"}
    creds = build_credentials(device, {"AuthKey": None}, "acct")
    assert (creds.account, creds.password, creds.legacy, creds.proto) == ("acct", "tok", True, "v2")


def test_partial_credentials_refused():
    with pytest.raises(P2PError, match="only part"):
        build_credentials(DEVICE, {"AuthKey": "k" * 8}, "acct")
    with pytest.raises(P2PError, match="no P2PUuid"):
        build_credentials({"DeviceName": "x"}, {}, "acct")


class FakeLib:
    """Answers avSendIOCtrl/avRecvIOCtrl like a camera would."""

    def __init__(self, replies):
        self.replies = list(replies)  # (opcode, bytes) delivered after the send
        self.queued = [(1, b"stale")]  # something left over from before
        self.sent = []
        self.closed = False

    def IOTC_Get_SessionID(self):
        return 7

    def IOTC_Connect_ByUIDEx(self, uid, sid, cin):
        return 7

    def avClientStartEx(self, cin, cout):
        return 3

    def avSendIOCtrl(self, chan, opcode, data, size):
        self.sent.append(opcode.value)
        self.queued.extend(self.replies)
        return 0

    def avRecvIOCtrl(self, chan, opcode_ref, buf, size, timeout):
        if not self.queued:
            return -20012
        code, data = self.queued.pop(0)
        opcode_ref._obj.value = code
        ctypes.memmove(buf, data, len(data))
        return len(data)

    def avSendIOCtrlExit(self, chan):
        pass

    def avClientStop(self, chan):
        self.closed = True

    def IOTC_Connect_Stop_BySID(self, sid):
        pass

    def IOTC_Session_Close(self, sid):
        pass


CREDS = P2PCredentials("Cam", "UID1", "k" * 8, "a", "p", False, "v3")


def link(replies):
    lib = FakeLib(replies)
    lnk = CameraLink(lib, CREDS)
    lnk.open()
    return lnk, lib


def test_toss_acknowledged_by_reply():
    lnk, lib = link([(131080, b"\x00")])
    assert lnk.command("toss") == "acknowledged"
    assert lib.sent == [131079]


def test_toss_acknowledged_by_notification():
    lnk, _ = link([(196613, b"{}")])
    assert lnk.command("toss") == "acknowledged"


def test_error_status_raises():
    lnk, _ = link([(131080, b"\x05")])
    with pytest.raises(P2PError, match="error 5"):
        lnk.command("toss")


def test_rejection_raises():
    lnk, _ = link([(CMD_REJECTED, bytes([2, 0x07, 0x00, 0, 0]))])
    with pytest.raises(P2PError, match="rejected"):
        lnk.command("toss")


def test_no_reply_is_sent_not_failure():
    lnk, _ = link([])
    assert lnk.command("treat_sound", wait=0.2) == "sent"


def test_close():
    lnk, lib = link([])
    lnk.close()
    assert lib.closed and not lnk.is_open
