"""Treat schedule and double launch on/off."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity

from .schedule_entity import FurboScheduleEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import FurboConfigEntry

# Entity key -> the schedule setting it turns on and off.
SWITCHES = {
    "schedule_enabled": "enabled",
    "double_launch": "double_enabled",
    "triple_launch": "triple_enabled",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: FurboConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    async_add_entities(
        ScheduleSwitch(data.coordinator, scheduler, key)
        for scheduler in data.schedules.schedulers.values()
        for key in SWITCHES
    )


class ScheduleSwitch(FurboScheduleEntity, SwitchEntity):
    """Turns a part of the treat schedule on or off."""

    @property
    def _field(self) -> str:
        return SWITCHES[str(self._attr_translation_key)]

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.scheduler.settings, self._field))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.scheduler.update_settings(**{self._field: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.scheduler.update_settings(**{self._field: False})
