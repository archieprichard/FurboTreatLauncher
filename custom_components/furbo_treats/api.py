"""Client for the Furbo Treat Bridge add-on's HTTP API."""

from __future__ import annotations

from typing import Any

import aiohttp

# Opening a camera link can take ~40 s; with photos each toss adds up to ~10 s
# (video start, 2 s of frames, decoding), so a 5-treat burst needs ~2 minutes.
# Give up too early and a treat that did go out looks failed, inviting a retry.
TIMEOUT = aiohttp.ClientTimeout(total=180)


class BridgeError(Exception):
    """The bridge could not do what was asked."""

    def __init__(self, message: str, kind: str = "error", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class BridgeConnectionError(BridgeError):
    """The bridge could not be reached, or its answer was lost.

    For a toss this means the outcome is unknown: the treat may have gone out.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, "connection")


class BridgeAuthError(BridgeError):
    """The bridge refused the API token."""


class BridgeCodeRequired(BridgeError):
    """Furbo wants an emailed verification code before the bridge can continue."""


class BridgeClient:
    """Talks to one Furbo Treat Bridge."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}/api"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with self._session.request(
                method, f"{self._base}{path}", json=body, headers=self._headers, timeout=TIMEOUT
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BridgeConnectionError(
                f"Could not reach the Furbo Treat Bridge: {type(err).__name__}"
            ) from err
        if not isinstance(data, dict):
            raise BridgeConnectionError(f"The bridge answered {status} with something unreadable")
        if status == 200:
            return data
        message = str(data.get("message") or f"Bridge error {status}")
        kind = str(data.get("error") or "error")
        if status == 401:
            raise BridgeAuthError(message, kind)
        if kind == "code_required":
            raise BridgeCodeRequired(message, kind)
        retry_after = data.get("retry_after")
        raise BridgeError(
            message, kind, float(retry_after) if isinstance(retry_after, (int, float)) else None
        )

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/status")

    async def submit_code(self, code: str) -> dict[str, Any]:
        return await self._request("POST", "/login/code", {"code": code})

    async def resend_code(self) -> dict[str, Any]:
        return await self._request("POST", "/login/resend", {})

    async def toss(self, device_id: str, count: int = 1, *, photos: bool = False) -> dict[str, Any]:
        return await self._request(
            "POST", "/toss", {"device_id": device_id, "count": count, "photos": photos}
        )

    async def treat_sound(self, device_id: str) -> dict[str, Any]:
        return await self._request("POST", "/treat-sound", {"device_id": device_id})
