"""Base for the entities that control a camera's treat schedule."""

from __future__ import annotations

from homeassistant.core import callback

from .coordinator import FurboStatusCoordinator
from .entity import FurboEntity
from .scheduler import TreatScheduler


class FurboScheduleEntity(FurboEntity):
    """Reads and writes one camera's schedule; updates when the schedule changes."""

    def __init__(self, coordinator: FurboStatusCoordinator, scheduler: TreatScheduler, key: str) -> None:
        super().__init__(coordinator, scheduler.camera_id, key)
        self.scheduler = scheduler

    @property
    def available(self) -> bool:
        # Settings stay editable while the bridge is briefly unreachable.
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.scheduler.add_listener(self._schedule_changed))

    @callback
    def _schedule_changed(self) -> None:
        self.async_write_ha_state()
