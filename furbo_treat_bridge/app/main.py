"""Entry point: read the add-on options, sign in, serve the HTTP API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import signal

import aiohttp
from aiohttp import web

from bridge import Bridge, Options, StateStore
from furbo_p2p import shutdown_sdk
from server import VERSION, build_app

DATA = Path(os.environ.get("FURBO_DATA", "/data"))
PORT = int(os.environ.get("FURBO_PORT", "8792"))
LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_LOGGER = logging.getLogger("furbo_treat_bridge")


def _api_token(configured: str, store: StateStore) -> str:
    """The configured token, or one generated once and kept."""
    if configured.strip():
        return configured.strip()
    token = store.get("generated_api_token")
    if not token:
        token = secrets.token_urlsafe(32)
        store.update(generated_api_token=token)
    _LOGGER.warning(
        "No api_token set in the add-on configuration; using a generated one. "
        "Enter this in the Home Assistant integration: %s",
        token,
    )
    return token


def _log_start_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception():
        _LOGGER.error("Startup sign-in crashed", exc_info=task.exception())


async def run() -> None:
    raw = json.loads((DATA / "options.json").read_text())
    logging.basicConfig(
        level=LOG_LEVELS.get(str(raw.get("log_level", "info")), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # The TUTK library is chatty and the cloud client is quiet; keep ours readable.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    _LOGGER.info("Furbo Treat Bridge %s starting", VERSION)

    store = StateStore(DATA / "state.json")
    if raw.get("reset_session"):
        _LOGGER.warning("reset_session is on: forgetting the stored Furbo sign-in")
        store.update(cognito_token=None, mfa_auth_code=None, pending_candidate=None)
    options = Options(
        email=str(raw.get("email") or ""),
        password=str(raw.get("password") or ""),
        min_interval=float(raw.get("min_interval", 5)),
        daily_limit=int(raw.get("daily_limit", 0)),
    )
    token = _api_token(str(raw.get("api_token") or ""), store)

    async with aiohttp.ClientSession() as http:
        bridge = Bridge(options, store, http)
        runner = web.AppRunner(build_app(bridge, token), access_log=None)
        await runner.setup()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        _LOGGER.info("Listening on port %d", PORT)
        # Signing in runs alongside, so a stop request is honoured even mid-sign-in.
        start = asyncio.create_task(bridge.start())
        start.add_done_callback(_log_start_failure)
        await stop.wait()
        _LOGGER.info("Shutting down")
        start.cancel()
        bridge.stop()
        await runner.cleanup()
        await asyncio.to_thread(bridge.close)
    shutdown_sdk()


if __name__ == "__main__":
    asyncio.run(run())
