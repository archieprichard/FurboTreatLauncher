"""Treats per day, minimum gap and double launch chance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTime

from .schedule_entity import FurboScheduleEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import FurboConfigEntry
    from .coordinator import FurboStatusCoordinator
    from .scheduler import TreatScheduler


@dataclass(frozen=True, kw_only=True)
class ScheduleNumberDescription(NumberEntityDescription):
    """A whole-number schedule setting."""

    field: str


NUMBERS = (
    ScheduleNumberDescription(
        key="treats_per_day",
        field="per_day",
        native_min_value=1,
        native_max_value=50,
        native_step=1,
        native_unit_of_measurement="treats",
        mode=NumberMode.BOX,
    ),
    ScheduleNumberDescription(
        key="min_gap",
        field="min_gap_minutes",
        native_min_value=0,
        native_max_value=720,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
    ),
    ScheduleNumberDescription(
        key="double_chance",
        field="double_chance",
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
    ),
    ScheduleNumberDescription(
        key="triple_chance",
        field="triple_chance",
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: FurboConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    async_add_entities(
        ScheduleNumber(data.coordinator, scheduler, description)
        for scheduler in data.schedules.schedulers.values()
        for description in NUMBERS
    )


class ScheduleNumber(FurboScheduleEntity, NumberEntity):
    """One numeric schedule setting."""

    entity_description: ScheduleNumberDescription

    def __init__(
        self,
        coordinator: FurboStatusCoordinator,
        scheduler: TreatScheduler,
        description: ScheduleNumberDescription,
    ) -> None:
        super().__init__(coordinator, scheduler, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        return getattr(self.scheduler.settings, self.entity_description.field)

    async def async_set_native_value(self, value: float) -> None:
        self.scheduler.update_settings(**{self.entity_description.field: int(value)})
