"""Schedule window start and end."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from homeassistant.components.time import TimeEntity

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
        WindowTime(data.coordinator, scheduler, key)
        for scheduler in data.schedules.schedulers.values()
        for key in ("window_start", "window_end")
    )


class WindowTime(FurboScheduleEntity, TimeEntity):
    """One end of the daily window treats are scheduled in."""

    @property
    def _field(self) -> str:
        return "start" if self._attr_translation_key == "window_start" else "end"

    @property
    def native_value(self) -> dt.time:
        return getattr(self.scheduler.settings, self._field)

    async def async_set_value(self, value: dt.time) -> None:
        self.scheduler.update_settings(**{self._field: value.replace(microsecond=0)})
