"""The latest treat photos: 1 second and 2 seconds after the last toss."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .entity import FurboEntity
from .photo_store import OFFSETS, PhotoStore

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import FurboConfigEntry
    from .coordinator import FurboStatusCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: FurboConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    store = data.schedules.photos
    if store is None:
        return
    tz = dt_util.get_default_time_zone()
    for scheduler in data.schedules.schedulers.values():
        await hass.async_add_executor_job(store.load_latest, scheduler.camera_id, scheduler.camera_name, tz)
    async_add_entities(
        TreatPhoto(hass, data.coordinator, scheduler.camera_id, store, offset)
        for scheduler in data.schedules.schedulers.values()
        for offset in OFFSETS
    )


class TreatPhoto(FurboEntity, ImageEntity):
    """The photo taken ``offset`` seconds after the most recent toss."""

    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: FurboStatusCoordinator,
        camera_id: str,
        store: PhotoStore,
        offset: float,
    ) -> None:
        key = "treat_photo_1s" if offset == 1.0 else "treat_photo_2s"
        FurboEntity.__init__(self, coordinator, camera_id, key)
        ImageEntity.__init__(self, hass)
        self._store = store
        self._offset = offset
        latest = store.latest.get(camera_id, {}).get(offset)
        self._attr_image_last_updated = latest[1] if latest else None

    @property
    def available(self) -> bool:
        # The last photo stays viewable while the bridge is unreachable.
        return True

    async def async_image(self) -> bytes | None:
        latest = self._store.latest.get(self.camera_id, {}).get(self._offset)
        return latest[0] if latest else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._store.add_listener(self._photo_taken))

    @callback
    def _photo_taken(self, camera_id: str) -> None:
        latest = self._store.latest.get(self.camera_id, {}).get(self._offset)
        if camera_id == self.camera_id and latest:
            self._attr_image_last_updated = latest[1]
            self.async_write_ha_state()
