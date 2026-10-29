"""Schedule mode: fixed or random."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity

from .planner import MODES
from .schedule_entity import FurboScheduleEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import FurboConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: FurboConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    async_add_entities(
        ScheduleMode(data.coordinator, scheduler, "schedule_mode")
        for scheduler in data.schedules.schedulers.values()
    )


class ScheduleMode(FurboScheduleEntity, SelectEntity):
    """Fixed spreads treats evenly; random picks new times each day."""

    _attr_options = list(MODES)

    @property
    def current_option(self) -> str:
        return self.scheduler.settings.mode

    async def async_select_option(self, option: str) -> None:
        self.scheduler.update_settings(mode=option)
