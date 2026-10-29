"""The treat bridge: Furbo sign-in, camera links and treat limits.

The bridge holds the Furbo password (from the add-on options) so it can sign in
again on its own, which Furbo needs roughly daily. Furbo emails a verification
code on the first sign-in; once that code is entered the bridge keeps the proof
Furbo hands back, so later sign-ins go through without another code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import datetime as dt
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any

import aiohttp

from furbo_cloud import (
    FurboAuthError,
    FurboCloud,
    FurboCodeError,
    FurboCodeRequired,
    FurboConnectionError,
    FurboError,
    FurboLoginError,
    encrypt_password,
    new_mobile_id,
)
from furbo_p2p import CameraLink, P2PCredentials, P2PError, build_credentials, load_sdk

_LOGGER = logging.getLogger(__name__)

# Login states reported to the integration.
READY = "ready"
CODE_REQUIRED = "code_required"
SIGNING_IN = "signing_in"
ERROR = "error"

# Fields kept from each camera's cloud record; the rest is not needed.
DEVICE_FIELDS = ("Id", "DeviceName", "ProductId", "P2PUuid", "P2PAccessToken")
MAX_BURST = 5  # treats per request
BURST_SPACING = 2.0  # seconds between treats in one request


class BridgeError(Exception):
    """A request the bridge could not carry out. ``kind`` maps to an HTTP status."""

    def __init__(self, kind: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.extra = extra


@dataclass
class Options:
    email: str
    password: str
    min_interval: float = 5.0
    daily_limit: int = 0  # 0 = no limit
    link_idle: float = 60.0  # seconds a camera link stays open after the last command
    lib_path: str = "/usr/local/lib/libIOTCAPIs_ALL.so"


@dataclass
class _Camera:
    record: dict[str, Any]
    lock: threading.Lock = field(default_factory=threading.Lock)
    link: CameraLink | None = None
    close_timer: threading.Timer | None = None
    last_toss_at: float | None = None  # for the minimum interval, per camera


class StateStore:
    """The bridge's persistent state, as one JSON file in the add-on's /data."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update(self, **values: Any) -> None:
        for key, value in values.items():
            if value is None:
                self.data.pop(key, None)
            else:
                self.data[key] = value
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.path)


def _today() -> str:
    return dt.datetime.now().astimezone().date().isoformat()


class Bridge:
    """Signs in to Furbo and sends treat commands to the account's cameras."""

    def __init__(
        self,
        options: Options,
        store: StateStore,
        http: aiohttp.ClientSession,
        *,
        cloud: FurboCloud | None = None,
        link_factory: Callable[[P2PCredentials], CameraLink] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.options = options
        self.store = store
        self.cloud = cloud or FurboCloud(http)
        self._link_factory = link_factory or self._real_link
        self._clock = clock
        self.login_state = READY if store.get("cognito_token") else SIGNING_IN
        self.login_error: str | None = None
        self._login_lock = asyncio.Lock()
        self._toss_lock = asyncio.Lock()
        # Whether the last sign-in failure is worth retrying on its own (Furbo
        # unreachable) rather than needing a person (wrong password).
        self._retryable_failure = False
        self._retry_task: asyncio.Task[None] | None = None
        self._cameras: dict[str, _Camera] = {}
        self._sync_cameras()

    # --- helpers -------------------------------------------------------------

    def _real_link(self, creds: P2PCredentials) -> CameraLink:
        return CameraLink(load_sdk(self.options.lib_path), creds)

    def _sync_cameras(self) -> None:
        for record in self.store.get("devices", []):
            device_id = str(record["Id"])
            if device_id in self._cameras:
                self._cameras[device_id].record = record
            else:
                self._cameras[device_id] = _Camera(record)
        known = {str(r["Id"]) for r in self.store.get("devices", [])}
        for device_id in list(self._cameras):
            if device_id not in known:
                self._close_camera(self._cameras.pop(device_id))

    def _mobile_id(self) -> str:
        mobile_id = self.store.get("mobile_id")
        if not mobile_id:
            mobile_id = new_mobile_id()
            self.store.update(mobile_id=mobile_id)
        return mobile_id

    def treats_today(self) -> int:
        counts = self.store.get("treat_counts", {})
        return int(counts.get(_today(), 0))

    def status(self) -> dict[str, Any]:
        return {
            "login": self.login_state,
            "login_error": self.login_error,
            "cameras": [
                {
                    "id": str(c.record["Id"]),
                    "name": c.record.get("DeviceName") or str(c.record["Id"]),
                    "model": c.record.get("ProductId") or "",
                    "link_open": bool(c.link and c.link.is_open),
                }
                for c in self._cameras.values()
            ],
            "treats_today": self.treats_today(),
            "daily_limit": self.options.daily_limit,
            "min_interval": self.options.min_interval,
            "last_toss": self.store.get("last_toss"),
        }

    # --- sign-in -------------------------------------------------------------

    async def start(self) -> None:
        """Sign in at startup if there is no stored session."""
        if self.store.get("cognito_token"):
            return
        if self.store.get("pending_candidate"):
            # Restarted while waiting for an emailed code: keep waiting for that
            # one rather than making Furbo email another and void it.
            self.login_state = CODE_REQUIRED
            _LOGGER.info("Still waiting for the verification code Furbo emailed")
            return
        await self.sign_in()
        if self.login_state == ERROR and self._retryable_failure:
            self._retry_task = asyncio.create_task(self._retry_sign_in())

    async def _retry_sign_in(self) -> None:
        """Keep trying while Furbo is unreachable (e.g. the network was not up at boot)."""
        delay = 30.0
        while self.login_state == ERROR and self._retryable_failure:
            await asyncio.sleep(delay)
            _LOGGER.info("Retrying Furbo sign-in")
            await self.sign_in()
            delay = min(delay * 2, 600.0)

    def stop(self) -> None:
        """Stop background work. Call on the event loop."""
        if self._retry_task:
            self._retry_task.cancel()

    async def sign_in(self, *, force_new_code: bool = False) -> str:
        """Sign in with the stored password. Returns the resulting login state.

        When Furbo wants a verification code, this asks Furbo to email one and
        parks the login until :meth:`submit_code` is called with it.
        """
        async with self._login_lock:
            return await self._sign_in(force_new_code)

    async def _sign_in(self, force_new_code: bool) -> str:
        opts = self.options
        if not opts.email or not opts.password:
            return self._fail("Set your Furbo email and password in the add-on configuration.")
        self.login_state, self.login_error = SIGNING_IN, None
        enc = encrypt_password(opts.password)
        mobile_id = self._mobile_id()
        proof = None if force_new_code else self.store.get("mfa_auth_code")
        try:
            result = await self.cloud.login(opts.email, enc, mobile_id, proof)
        except FurboCodeRequired as needed:
            try:
                candidate = await self.cloud.send_code(needed.candidate)
            except FurboError as err:
                return self._fail(f"Furbo would not send a verification code: {err}")
            self.store.update(pending_candidate=candidate, cognito_token=None)
            self.login_state = CODE_REQUIRED
            _LOGGER.info("Furbo emailed a verification code to %s", opts.email)
            return self.login_state
        except FurboLoginError:
            return self._fail("Furbo refused the email or password in the add-on configuration.")
        except FurboConnectionError as err:
            return self._fail(f"Could not reach Furbo to sign in: {err}", retryable=True)
        except FurboError as err:
            return self._fail(f"Could not sign in to Furbo: {err}")
        return await self._finish_login(result.account_id, result.cognito_token, result.mfa_auth_code)

    async def submit_code(self, code: str) -> str:
        """Finish a parked sign-in with the emailed verification code."""
        code = code.strip()
        if not code.isdigit():
            raise BridgeError("bad_request", "The verification code should be the digits Furbo emailed.")
        async with self._login_lock:
            candidate = self.store.get("pending_candidate")
            if self.login_state != CODE_REQUIRED or not candidate:
                raise BridgeError("conflict", "No sign-in is waiting for a code. Ask for a new one first.")
            enc = encrypt_password(self.options.password)
            try:
                result = await self.cloud.verify_code(
                    self.options.email, enc, self._mobile_id(), candidate, code
                )
            except FurboCodeError as err:
                raise BridgeError(
                    "bad_code", "Furbo did not accept that code. Check it, or ask for a new one."
                ) from err
            except FurboError as err:
                raise BridgeError("upstream", f"Could not reach Furbo: {err}") from err
            self.store.update(pending_candidate=None)
            return await self._finish_login(result.account_id, result.cognito_token, result.mfa_auth_code)

    async def _finish_login(self, account_id: str, token: str, proof: str | None) -> str:
        try:
            devices = await self.cloud.get_devices(account_id, token)
        except FurboError as err:
            return self._fail(
                f"Signed in, but could not list cameras: {err}",
                retryable=isinstance(err, FurboConnectionError),
            )
        records = [{k: d.get(k) for k in DEVICE_FIELDS if d.get(k) is not None} for d in devices]
        self.store.update(
            account_id=account_id,
            cognito_token=token,
            mfa_auth_code=proof or self.store.get("mfa_auth_code"),
            devices=records,
            pending_candidate=None,  # whatever code was outstanding is moot now
        )
        self._sync_cameras()
        self.login_state, self.login_error = READY, None
        _LOGGER.info("Signed in to Furbo; %d camera(s) on the account", len(records))
        return self.login_state

    def _fail(self, message: str, *, retryable: bool = False) -> str:
        self.login_state, self.login_error = ERROR, message
        self._retryable_failure = retryable
        _LOGGER.error(message)
        return self.login_state

    async def _ensure_signed_in(self) -> tuple[str, str]:
        """The account id and token, signing in first if there is no live session.

        Waits for any sign-in already running and uses its outcome, so a toss
        that arrives mid-sign-in never starts a second one (which, when Furbo
        wants a code, would email another and void the first).
        """
        async with self._login_lock:
            account_id = self.store.get("account_id")
            token = self.store.get("cognito_token")
            if self.login_state == READY and account_id and token:
                return account_id, token
            if self.login_state == CODE_REQUIRED:
                raise BridgeError("code_required", "Furbo is waiting for the emailed verification code.")
            state = await self._sign_in(False)
            if state == CODE_REQUIRED:
                raise BridgeError(
                    "code_required", "Furbo emailed a new verification code. Enter it in Home Assistant."
                )
            if state != READY:
                raise BridgeError("not_signed_in", self.login_error or "Not signed in to Furbo.")
            return self.store.get("account_id"), self.store.get("cognito_token")

    async def _p2p_credentials(self, camera: _Camera) -> P2PCredentials:
        """Fresh link credentials for a camera, signing in again if the token died."""
        for attempt in (1, 2):
            account_id, token = await self._ensure_signed_in()
            try:
                p2p = await self.cloud.get_p2p_connection(account_id, token, str(camera.record["Id"]))
                return build_credentials(camera.record, p2p, account_id)
            except FurboAuthError as err:
                if attempt == 2:
                    raise BridgeError("not_signed_in", "Furbo refused a fresh session.") from err
                _LOGGER.info("Furbo session expired; signing in again")
                # Only if no one has replaced it meanwhile.
                if self.store.get("cognito_token") == token:
                    self.store.update(cognito_token=None)
                    self.login_state = SIGNING_IN
            except FurboConnectionError as err:
                raise BridgeError("upstream", f"Could not reach Furbo: {err}") from err
            except FurboError as err:
                raise BridgeError("upstream", str(err)) from err
            except P2PError as err:
                raise BridgeError("camera", str(err)) from err
        raise AssertionError("unreachable")  # pragma: no cover

    # --- treats --------------------------------------------------------------

    def _pick_camera(self, device_id: str | None) -> _Camera:
        if device_id:
            camera = self._cameras.get(str(device_id))
            if camera is None:
                raise BridgeError("not_found", f"No camera {device_id} on this Furbo account.")
            return camera
        if len(self._cameras) == 1:
            return next(iter(self._cameras.values()))
        if not self._cameras:
            raise BridgeError("not_found", "No cameras on this Furbo account.")
        raise BridgeError("bad_request", "This account has several cameras; say which one.")

    def _check_limits(self, camera: _Camera, count: int) -> None:
        if camera.last_toss_at is not None:
            wait = self.options.min_interval - (self._clock() - camera.last_toss_at)
            if wait > 0:
                raise BridgeError(
                    "rate_limited",
                    f"Treats are limited to one request every {self.options.min_interval:g}s.",
                    retry_after=round(wait, 1),
                )
        limit = self.options.daily_limit
        if limit and self.treats_today() + count > limit:
            raise BridgeError(
                "rate_limited",
                f"Daily limit of {limit} treats reached ({self.treats_today()} today).",
                retry_after=None,
            )

    def _count_treat(self) -> None:
        today = _today()
        counts = self.store.get("treat_counts", {})
        counts = {today: int(counts.get(today, 0)) + 1}  # keep only today's count
        self.store.update(
            treat_counts=counts, last_toss=dt.datetime.now().astimezone().isoformat(timespec="seconds")
        )

    async def toss(self, device_id: str | None = None, count: int = 1) -> dict[str, Any]:
        """Toss ``count`` treats from one camera."""
        if not 1 <= count <= MAX_BURST:
            raise BridgeError("bad_request", f"count must be between 1 and {MAX_BURST}.")
        async with self._toss_lock:
            camera = self._pick_camera(device_id)
            self._check_limits(camera, count)
            results = []
            for i in range(count):
                if i:
                    await asyncio.sleep(BURST_SPACING)
                results.append(await self._send(camera, "toss"))
                # Only a treat that went out holds off the next request.
                camera.last_toss_at = self._clock()
                self._count_treat()
            return {
                "camera": camera.record.get("DeviceName"),
                "results": results,
                "treats_today": self.treats_today(),
            }

    async def treat_sound(self, device_id: str | None = None) -> dict[str, Any]:
        """Play the treat-toss sound without tossing anything."""
        camera = self._pick_camera(device_id)
        return {
            "camera": camera.record.get("DeviceName"),
            "results": [await self._send(camera, "treat_sound")],
        }

    async def _send(self, camera: _Camera, command: str) -> str:
        if camera.link and camera.link.is_open:
            try:
                return await asyncio.to_thread(self._send_blocking, camera, command, None)
            except BridgeError as err:
                # A link left open can die without notice (the camera rebooted,
                # Wi-Fi dropped). Nothing was dispensed, so reconnect once.
                if err.kind != "camera":
                    raise
                _LOGGER.info("Reconnecting to %s: %s", camera.record.get("DeviceName"), err)
        creds = await self._p2p_credentials(camera)
        return await asyncio.to_thread(self._send_blocking, camera, command, creds)

    def _send_blocking(self, camera: _Camera, command: str, creds: P2PCredentials | None) -> str:
        with camera.lock:
            if camera.close_timer:
                camera.close_timer.cancel()
            try:
                if camera.link is None or not camera.link.is_open:
                    if creds is None:  # the link closed between the check and here
                        raise BridgeError("camera", "The camera link closed.")
                    camera.link = self._link_factory(creds)
                    camera.link.open()
                return camera.link.command(command)
            except P2PError as err:
                self._close_link(camera)
                raise BridgeError("camera", str(err)) from err
            except BridgeError:
                self._close_link(camera)
                raise
            finally:
                if camera.link and camera.link.is_open:
                    camera.close_timer = threading.Timer(
                        self.options.link_idle, self._close_camera, args=(camera,)
                    )
                    camera.close_timer.daemon = True
                    camera.close_timer.start()

    def _close_link(self, camera: _Camera) -> None:
        if camera.link:
            try:
                camera.link.close()
            except Exception:
                _LOGGER.debug("Error closing link", exc_info=True)
            camera.link = None

    def _close_camera(self, camera: _Camera) -> None:
        with camera.lock:
            if camera.close_timer:
                camera.close_timer.cancel()
                camera.close_timer = None
            self._close_link(camera)

    def close(self) -> None:
        """Close every camera link. Blocking: call from a worker thread, after stop()."""
        for camera in self._cameras.values():
            self._close_camera(camera)
