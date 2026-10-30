"""Toss Treat and Play Treat Sound buttons."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import BridgeError
from .entity import FurboEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import FurboConfigEntry

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: FurboConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        button
        for camera in coordinator.data.get("cameras", [])
        for button in (
            FurboButton(coordinator, camera["id"], "toss_treat"),
            FurboButton(coordinator, camera["id"], "treat_sound"),
        )
    )


class FurboButton(FurboEntity, ButtonEntity):
    """A one-press camera command."""

    async def async_press(self) -> None:
        data = self.coordinator.config_entry.runtime_data
        try:
            if self._attr_translation_key == "toss_treat":
                await data.schedules.async_toss(self.camera_id)  # saves photos too
            else:
                await data.client.treat_sound(self.camera_id)
        except BridgeError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
