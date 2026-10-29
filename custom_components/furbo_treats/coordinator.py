"""Polls the bridge for its sign-in state, cameras and today's treat count."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BridgeAuthError, BridgeClient, BridgeError
from .const import DOMAIN, STATUS_INTERVAL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import FurboConfigEntry

_LOGGER = logging.getLogger(__name__)


class FurboStatusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Bridge status, refreshed every minute and after every treat."""

    config_entry: FurboConfigEntry

    def __init__(self, hass: HomeAssistant, entry: FurboConfigEntry, client: BridgeClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=STATUS_INTERVAL)
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.client.status()
        except BridgeAuthError as err:
            raise ConfigEntryAuthFailed("The bridge refused the API token") from err
        except BridgeError as err:
            raise UpdateFailed(str(err)) from err
        login = status.get("login")
        if login == "code_required":
            raise ConfigEntryAuthFailed("Furbo emailed a verification code; enter it to continue")
        if login == "error":
            raise UpdateFailed(status.get("login_error") or "The bridge could not sign in to Furbo")
        return status

    def camera(self, camera_id: str) -> dict[str, Any] | None:
        for camera in (self.data or {}).get("cameras", []):
            if camera.get("id") == camera_id:
                return camera
        return None
