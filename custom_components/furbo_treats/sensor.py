"""Treats today and the next scheduled treat."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass

from .entity import FurboEntity
from .schedule_entity import FurboScheduleEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import FurboConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: FurboConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    entities: list[SensorEntity] = []
    for scheduler in data.schedules.schedulers.values():
        entities.append(TreatsToday(data.coordinator, scheduler.camera_id, "treats_today"))
        entities.append(NextTreat(data.coordinator, scheduler, "next_treat"))
    async_add_entities(entities)


class TreatsToday(FurboEntity, SensorEntity):
    """Every treat tossed today through the bridge: scheduled, buttons and actions.

    The bridge keeps one count across all of the account's cameras, which is
    what its daily safety limit applies to.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "treats"

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("treats_today")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {"daily_limit": data.get("daily_limit") or None, "last_toss": data.get("last_toss")}


class NextTreat(FurboScheduleEntity, SensorEntity):
    """When the schedule tosses its next treat."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> dt.datetime | None:
        return self.scheduler.next_treat

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.scheduler
        limit = (self.coordinator.data or {}).get("daily_limit") or 0
        attrs: dict[str, Any] = {
            "planned_today": [t.isoformat() for t in s.plan],
            "scheduled_given_today": s.given,
            "double_launches_today": s.doubles,
            "triple_launches_today": s.triples,
            "last_result": s.last_result,
        }
        problems = [s.problem] if s.problem else []
        if s.settings.enabled and limit and s.settings.per_day > limit:
            problems.append(
                f"Treats per day ({s.settings.per_day}) is above the bridge's daily limit "
                f"({limit}); treats past the limit will be refused."
            )
        min_interval = (self.coordinator.data or {}).get("min_interval") or 0
        delay = s.settings.bonus_delay_seconds
        if s.settings.double_enabled and min_interval > delay:
            problems.append(
                f"The bridge's min_interval ({min_interval:g}s) is longer than the {delay}-second "
                "bonus launch delay, so double and triple launches will be refused. "
                "Lower min_interval in the add-on, or raise the delay."
            )
        if problems:
            attrs["problem"] = " ".join(problems)
        return attrs
