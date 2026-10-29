"""Base entity: one device per Furbo camera."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODELS
from .coordinator import FurboStatusCoordinator


class FurboEntity(CoordinatorEntity[FurboStatusCoordinator]):
    """An entity belonging to one camera."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FurboStatusCoordinator, camera_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.camera_id = camera_id
        camera = coordinator.camera(camera_id) or {}
        model = camera.get("model") or ""
        self._attr_unique_id = f"{camera_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, camera_id)},
            name=camera.get("name") or "Furbo",
            manufacturer=MANUFACTURER,
            model=MODELS.get(model, model or None),
            model_id=model or None,
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.camera(self.camera_id) is not None
