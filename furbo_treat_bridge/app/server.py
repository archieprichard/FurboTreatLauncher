"""HTTP API the Home Assistant integration talks to.

Every route needs ``Authorization: Bearer <api_token>``.

    GET  /api/status                       sign-in state, cameras, today's count
    POST /api/login/code   {"code": "1234"} finish sign-in with the emailed code
    POST /api/login/resend {}               ask Furbo to email a new code
    POST /api/toss         {"device_id"?: "...", "count"?: 1..5}
    POST /api/treat-sound  {"device_id"?: "..."}
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from aiohttp import web

from bridge import Bridge, BridgeError

_LOGGER = logging.getLogger(__name__)

VERSION = "0.1.0"
BRIDGE_KEY: web.AppKey[Bridge] = web.AppKey("bridge", Bridge)
TOKEN_KEY: web.AppKey[str] = web.AppKey("token", str)

STATUS_BY_KIND = {
    "bad_request": 400,
    "bad_code": 400,
    "not_found": 404,
    "conflict": 409,
    "code_required": 409,
    "rate_limited": 429,
    "camera": 502,
    "upstream": 502,
    "not_signed_in": 503,
}


@web.middleware
async def _auth(request: web.Request, handler: Any) -> web.StreamResponse:
    expected = f"Bearer {request.app[TOKEN_KEY]}"
    given = request.headers.get("Authorization", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        return web.json_response({"error": "unauthorized", "message": "Bad or missing token."}, status=401)
    try:
        return await handler(request)
    except BridgeError as err:
        body = {"error": err.kind, "message": str(err), **err.extra}
        return web.json_response(body, status=STATUS_BY_KIND.get(err.kind, 500))


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except ValueError as err:
        raise BridgeError("bad_request", "Body must be JSON.") from err
    if not isinstance(data, dict):
        raise BridgeError("bad_request", "Body must be a JSON object.")
    return data


def _device(data: dict[str, Any]) -> str | None:
    value = data.get("device_id")
    if value is None or value == "":
        return None
    if not isinstance(value, (str, int)):
        raise BridgeError("bad_request", "device_id must be a string.")
    return str(value)


async def get_status(request: web.Request) -> web.Response:
    return web.json_response({"version": VERSION, **request.app[BRIDGE_KEY].status()})


async def post_code(request: web.Request) -> web.Response:
    code = (await _body(request)).get("code")
    if not isinstance(code, (str, int)):
        raise BridgeError("bad_request", 'Send the code as {"code": "1234"}.')
    state = await request.app[BRIDGE_KEY].submit_code(str(code))
    return web.json_response({"login": state})


async def post_resend(request: web.Request) -> web.Response:
    state = await request.app[BRIDGE_KEY].sign_in(force_new_code=True)
    bridge = request.app[BRIDGE_KEY]
    return web.json_response({"login": state, "login_error": bridge.login_error})


async def post_toss(request: web.Request) -> web.Response:
    data = await _body(request)
    count = data.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int):
        raise BridgeError("bad_request", "count must be a whole number.")
    result = await request.app[BRIDGE_KEY].toss(_device(data), count)
    return web.json_response({"ok": True, **result})


async def post_treat_sound(request: web.Request) -> web.Response:
    result = await request.app[BRIDGE_KEY].treat_sound(_device(await _body(request)))
    return web.json_response({"ok": True, **result})


def build_app(bridge: Bridge, token: str) -> web.Application:
    app = web.Application(middlewares=[_auth])
    app[BRIDGE_KEY] = bridge
    app[TOKEN_KEY] = token
    app.add_routes(
        [
            web.get("/api/status", get_status),
            web.post("/api/login/code", post_code),
            web.post("/api/login/resend", post_resend),
            web.post("/api/toss", post_toss),
            web.post("/api/treat-sound", post_treat_sound),
        ]
    )
    return app
