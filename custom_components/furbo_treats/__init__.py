"""Furbo Treat Launcher: toss Furbo treats from Home Assistant, on demand or on a schedule."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_DEVICE_ID, CONF_HOST, CONF_PORT, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .api import BridgeClient, BridgeError
from .const import ATTR_COUNT, CONF_API_TOKEN, DOMAIN, SERVICE_TOSS_TREAT
from .coordinator import FurboStatusCoordinator
from .photo_store import PhotoStore
from .scheduler import ScheduleManager

PLATFORMS = [
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

TOSS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Optional(ATTR_COUNT, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
    }
)


@dataclass
class FurboData:
    """What a loaded entry holds."""

    client: BridgeClient
    coordinator: FurboStatusCoordinator
    schedules: ScheduleManager


type FurboConfigEntry = ConfigEntry[FurboData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the toss_treat action."""

    async def toss_treat(call: ServiceCall) -> ServiceResponse:
        device = dr.async_get(hass).async_get(call.data[CONF_DEVICE_ID])
        if device is None:
            raise ServiceValidationError("That device does not exist.")
        camera_id = next((i[1] for i in device.identifiers if i[0] == DOMAIN), None)
        entry: FurboConfigEntry | None = next(
            (
                e
                for e in hass.config_entries.async_entries(DOMAIN)
                if e.state is ConfigEntryState.LOADED and e.entry_id in device.config_entries
            ),
            None,
        )
        if camera_id is None or entry is None:
            raise ServiceValidationError("Pick a Furbo camera set up by Furbo Treat Launcher.")
        try:
            result = await entry.runtime_data.schedules.async_toss(camera_id, call.data[ATTR_COUNT])
        except BridgeError as err:
            raise HomeAssistantError(f"Could not toss a treat: {err}") from err
        await entry.runtime_data.coordinator.async_request_refresh()
        return {
            "results": result.get("results", []),
            "treats_today": result.get("treats_today"),
            "photos": result.get("photo_urls", []),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_TOSS_TREAT,
        toss_treat,
        schema=TOSS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _photo_store(hass: HomeAssistant) -> PhotoStore:
    """Photos go in Home Assistant's local media folder (/media on Home Assistant OS)."""
    media_dirs = hass.config.media_dirs
    name = "local" if "local" in media_dirs else next(iter(media_dirs), "local")
    root = media_dirs.get(name) or hass.config.path("media")
    return PhotoStore(Path(root), name)


async def async_setup_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Connect to the bridge and start the schedules."""
    client = BridgeClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_API_TOKEN],
    )
    coordinator = FurboStatusCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    cameras = coordinator.data.get("cameras", [])
    if not cameras:
        # The bridge is still signing in after a restart; try again shortly.
        raise ConfigEntryNotReady("The Furbo Treat Bridge has not listed any cameras yet")

    schedules = ScheduleManager(hass, entry.entry_id, client, coordinator, _photo_store(hass))
    await schedules.async_load(cameras)
    entry.runtime_data = FurboData(client, coordinator, schedules)
    entry.async_on_unload(schedules.async_unload)

    known = {str(c["id"]) for c in cameras}

    @callback
    def _cameras_changed() -> None:
        """Reload when a camera is added to or removed from the Furbo account."""
        current = {str(c["id"]) for c in (coordinator.data or {}).get("cameras", [])}
        if current and current != known:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(coordinator.async_add_listener(_cameras_changed))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FurboConfigEntry) -> bool:
    """Unload the entry; the schedules stop via async_on_unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
