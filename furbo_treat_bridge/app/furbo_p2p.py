"""The encrypted peer-to-peer link to a Furbo camera, reduced to treat commands.

Furbo cameras are controlled over ThroughTek's TUTK/Kalay P2P protocol. The
library that speaks it is closed source; it is loaded here with ctypes. The
connection sequence, structure layouts, licence key and command numbers were
taken from the Furbo Android app by the ha-furbo project
(https://github.com/willbeeching/ha-furbo, MIT licensed), which verified a treat
toss on a Furbo 360 (FB0030, firmware 108). Furbo's cloud "TossTreat" endpoint
answers success without tossing anything, so this link is the only proven path.

Sequence for one command:
  1. Once per process: set the app's licence key, IOTC_Initialize2, avInitialize.
  2. IOTC_Connect_ByUIDEx with the camera's P2PUuid and 8-character AuthKey.
  3. avClientStartEx with P2PAccountId / P2PAccountKey over DTLS.
  4. avSendIOCtrl with the command number, then read replies with avRecvIOCtrl.

Every call into the library blocks, so callers run these methods in a thread.
"""

from __future__ import annotations

from ctypes import (
    CDLL,
    RTLD_GLOBAL,
    Structure,
    byref,
    c_char,
    c_char_p,
    c_int,
    c_int32,
    c_uint,
    c_uint8,
    c_uint16,
    c_uint32,
    create_string_buffer,
    sizeof,
)
from dataclasses import dataclass
import logging
import random
import threading
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# The Furbo app's TUTK licence key (TUTKGlobalAPIs.TUTK_SDK_Set_License_Key).
FURBO_LICENSE_KEY = b"AQAAAImKg/eLaZN7zGTUrAl9TqgL27EnRkTZm6iu5GvTVNWh7OW4gmiNUfkGqX9ooTOoSogLX7Cnp+bovyklByEGrbr3QS9f+XeoMknVSXd3p1ZxNDQIqXfE4CtzDVEdrFWQ7akXnuUHcySIy2KGhXjQdITBl2y3hJITKVi30pPAbxGuaiYCI/o9KFwtfgQS+8lhtkJGY4MmO9CXoFsUwpZZifAb"

# Command numbers. Replies come back on (command + 1) with a status byte first,
# 0 meaning success. Newer cameras (V3) use a different numbering from older ones.
COMMANDS = {
    "v3": {"toss": 131079, "treat_sound": 131085},
    "v2": {"toss": 2321, "treat_sound": 2373},
}
# V3 cameras also announce a toss on their own with this notification.
NOTIFY_TREAT_TOSSING_V3 = 196613
# A camera refusing a command answers this with [status, opcode_lo, opcode_hi, ...].
CMD_REJECTED = 0x40001

# Which command set each Furbo model uses (from the app's FurboType mapping).
PROTO_BY_PRODUCT = {
    "FB0030": "v3",  # Furbo 360 Dog Camera (verified by ha-furbo)
    "FB0035": "v3",
    "FBC0030": "v3",  # Furbo 360 Cat Camera
    "FBC0035": "v3",
    "MC0020": "v3",  # Furbo Mini 360
    "MC0030": "v3",
    "FB001": "v2",
    "FB002": "v2",
    "FB0025": "v2",
    "MC0010": "v2",
}

ERRORS = {
    -10: "IOTC_ER_UNLICENSE",
    -13: "IOTC_ER_TIMEOUT",
    -19: "IOTC_ER_CAN_NOT_FIND_DEVICE",
    -22: "IOTC_ER_SESSION_CLOSE_BY_REMOTE",
    -23: "IOTC_ER_REMOTE_TIMEOUT_DISCONNECT",
    -24: "IOTC_ER_DEVICE_NOT_LISTENING (camera offline)",
    -40: "IOTC_ER_NO_PERMISSION",
    -41: "IOTC_ER_NETWORK_UNREACHABLE",
    -42: "IOTC_ER_FAIL_SETUP_RELAY",
    -90: "IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY",
    -1004: "TUTK_ER_INVALID_LICENSE_KEY",
    -20009: "AV_ER_WRONG_VIEWACCorPWD",
    -20011: "AV_ER_TIMEOUT",
    -20015: "AV_ER_SESSION_CLOSE_BY_REMOTE",
    -20016: "AV_ER_REMOTE_TIMEOUT_DISCONNECT",
    -20040: "AV_ER_DTLS_WRONG_PWD",
    -20041: "AV_ER_DTLS_AUTH_FAIL",
}

MODERN_KEYS = ("AuthKey", "P2PAccountId", "P2PAccountKey")


class P2PError(Exception):
    """The link to the camera could not be opened or used."""


def describe(code: int) -> str:
    """A TUTK return code with its name, when known."""
    return f"{code} {ERRORS.get(code, '')}".strip()


@dataclass
class P2PCredentials:
    """Everything needed to open the link to one camera."""

    name: str
    uid: str
    auth_key: str
    account: str
    password: str
    legacy: bool
    proto: str


def build_credentials(device: dict[str, Any], p2p: dict[str, Any], account_id: str) -> P2PCredentials:
    """Combine the camera record and its fresh P2P keys into link credentials.

    A modern camera gets all three of AuthKey, P2PAccountId and P2PAccountKey.
    An older one gets none and authenticates with the account id and the
    camera's P2PAccessToken instead. Anything in between is refused rather than
    guessed at, since guessing would drop a camera that expects DTLS onto the
    unencrypted path.
    """
    name = str(device.get("DeviceName") or device.get("Id") or "camera")
    uid = device.get("P2PUuid")
    if not isinstance(uid, str) or not uid:
        raise P2PError(f"{name} has no P2PUuid in its cloud record")
    proto = PROTO_BY_PRODUCT.get(str(device.get("ProductId") or ""), "v3")
    usable = {k: p2p.get(k) for k in MODERN_KEYS if isinstance(p2p.get(k), str) and p2p.get(k)}

    if len(usable) == len(MODERN_KEYS):
        return P2PCredentials(
            name, uid, usable["AuthKey"], usable["P2PAccountId"], usable["P2PAccountKey"], False, proto
        )
    if usable:
        missing = ", ".join(k for k in MODERN_KEYS if k not in usable)
        raise P2PError(f"Furbo sent {name} only part of its P2P credentials (no {missing})")
    token = device.get("P2PAccessToken")
    if not isinstance(token, str) or not token:
        raise P2PError(f"Furbo sent {name} no P2P credentials and it has no access token")
    return P2PCredentials(name, uid, "", account_id, token, True, proto)


# --- TUTK SDK 4.x structures -----------------------------------------------


class _ConnectInput(Structure):
    _fields_ = [
        ("cb", c_uint32),
        ("authentication_type", c_uint),
        ("auth_key", c_char * 8),
        ("timeout", c_uint32),
    ]


class _AVClientStartIn(Structure):
    _fields_ = [
        ("cb", c_uint32),
        ("iotc_session_id", c_uint32),
        ("iotc_channel_id", c_uint8),
        ("timeout_sec", c_uint32),
        ("account_or_identity", c_char_p),
        ("password_or_token", c_char_p),
        ("resend", c_int32),
        ("security_mode", c_uint32),
        ("auth_type", c_uint32),
        ("sync_recv_data", c_int32),
    ]


class _AVClientStartOut(Structure):
    _fields_ = [
        ("cb", c_uint32),
        ("server_type", c_uint32),
        ("resend", c_int32),
        ("two_way_streaming", c_int32),
        ("sync_recv_data", c_int32),
        ("security_mode", c_uint32),
    ]


# The SDK is initialised once per process and shared by every camera link.
_SDK_LOCK = threading.Lock()
_sdk_lib: Any = None


def load_sdk(lib_path: str) -> Any:
    """Load and initialise the TUTK library once; later calls reuse it."""
    global _sdk_lib
    with _SDK_LOCK:
        if _sdk_lib is not None:
            return _sdk_lib
        lib = CDLL(lib_path, mode=RTLD_GLOBAL)
        lib.IOTC_Get_Version_String.restype = c_char_p
        _LOGGER.info("TUTK library %s", lib.IOTC_Get_Version_String().decode())
        ret = lib.TUTK_SDK_Set_License_Key(c_char_p(FURBO_LICENSE_KEY))
        if ret < 0:
            raise P2PError(f"TUTK licence key rejected: {describe(ret)}")
        ret = lib.IOTC_Initialize2(c_uint16(random.randint(10000, 19999)))
        if ret < 0:
            raise P2PError(f"IOTC_Initialize2 failed: {describe(ret)}")
        ret = lib.avInitialize(c_int(10))
        if ret < 0:
            raise P2PError(f"avInitialize failed: {describe(ret)}")
        _sdk_lib = lib
        return lib


def shutdown_sdk() -> None:
    """Release the library at process exit, after every link is closed."""
    global _sdk_lib
    with _SDK_LOCK:
        if _sdk_lib is not None:
            _sdk_lib.avDeInitialize()
            _sdk_lib.IOTC_DeInitialize()
            _sdk_lib = None


class CameraLink:
    """One open link to one camera. Not thread-safe: callers serialise use."""

    def __init__(self, lib: Any, creds: P2PCredentials) -> None:
        self._lib = lib
        self.creds = creds
        self._session = -1
        self._channel = -1

    @property
    def is_open(self) -> bool:
        return self._channel >= 0

    def open(self, timeout: int = 20) -> None:
        """Connect to the camera and open the encrypted command channel."""
        lib, creds = self._lib, self.creds
        sid = lib.IOTC_Get_SessionID()
        if sid < 0:
            raise P2PError(f"IOTC_Get_SessionID failed: {describe(sid)}")
        if creds.legacy:
            ret = lib.IOTC_Connect_ByUID_Parallel(c_char_p(creds.uid.encode()), c_int(sid))
        else:
            cin = _ConnectInput()
            cin.cb = sizeof(cin)
            cin.authentication_type = 0
            cin.auth_key = creds.auth_key.encode()
            cin.timeout = timeout
            ret = lib.IOTC_Connect_ByUIDEx(c_char_p(creds.uid.encode()), c_int(sid), byref(cin))
        if ret < 0:
            lib.IOTC_Session_Close(c_int(sid))
            raise P2PError(f"Could not reach {creds.name}: {describe(ret)}")
        self._session = ret

        av_in = _AVClientStartIn()
        av_in.cb = sizeof(av_in)
        av_in.iotc_session_id = self._session
        av_in.iotc_channel_id = 0
        av_in.timeout_sec = timeout
        av_in.account_or_identity = creds.account.encode()
        av_in.password_or_token = creds.password.encode()
        av_in.resend = 1
        av_in.security_mode = 0 if creds.legacy else 1  # 1 = DTLS, as the app uses
        av_in.auth_type = 0  # the app never sets it
        av_in.sync_recv_data = 0
        av_out = _AVClientStartOut()
        av_out.cb = sizeof(av_out)
        ret = lib.avClientStartEx(byref(av_in), byref(av_out))
        if ret < 0:
            self.close()
            raise P2PError(f"{creds.name} refused the command channel: {describe(ret)}")
        self._channel = ret
        _LOGGER.info("Link to %s open", creds.name)

    def _poll(self, timeout_ms: int) -> tuple[int, bytes] | None:
        opcode = c_uint()
        buf = create_string_buffer(4096)
        ret = self._lib.avRecvIOCtrl(
            c_int(self._channel), byref(opcode), buf, c_int(4096), c_uint(timeout_ms)
        )
        if ret < 0:
            return None
        return opcode.value, buf.raw[:ret]

    def command(self, name: str, wait: float = 3.0) -> str:
        """Send a treat command and report what the camera said.

        Returns "acknowledged" when the camera confirmed it, "sent" when it
        accepted the command without a reply in time. Raises P2PError when the
        send fails or the camera rejects it.
        """
        opcode = COMMANDS[self.creds.proto][name]
        # Drop anything already queued, so a stale reply is not taken for ours.
        while self._poll(0) is not None:
            pass
        ret = self._lib.avSendIOCtrl(c_int(self._channel), c_uint(opcode), b"\0\0\0\0", c_int(4))
        if ret < 0:
            raise P2PError(f"Sending {name} to {self.creds.name} failed: {describe(ret)}")

        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            reply = self._poll(200)
            if reply is None:
                # A dead channel returns at once instead of waiting; don't spin.
                time.sleep(0.05)
                continue
            code, data = reply
            if code == opcode + 1:
                if data and data[0] != 0:
                    raise P2PError(f"{self.creds.name} reported error {data[0]} for {name}")
                return "acknowledged"
            if code == NOTIFY_TREAT_TOSSING_V3 and name == "toss":
                return "acknowledged"
            if code == CMD_REJECTED and len(data) >= 3 and (data[1] | data[2] << 8) == opcode & 0xFFFF:
                raise P2PError(f"{self.creds.name} rejected {name} (status {data[0]})")
        return "sent"

    def close(self) -> None:
        """Close this link. The shared SDK stays up for other cameras."""
        lib = self._lib
        if self._channel >= 0:
            lib.avSendIOCtrlExit(c_int(self._channel))
            lib.avClientStop(c_int(self._channel))
            self._channel = -1
        if self._session >= 0:
            lib.IOTC_Connect_Stop_BySID(c_int(self._session))
            lib.IOTC_Session_Close(c_int(self._session))
            self._session = -1
