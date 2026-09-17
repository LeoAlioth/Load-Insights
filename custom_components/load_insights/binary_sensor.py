"""One binary sensor per named load: is it running right now."""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .detection import DetectionRunner
from .sensor import _child_device


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    runner: DetectionRunner = hass.data[DOMAIN].get(f"{entry.entry_id}_detection")
    if runner is None:
        return
    add([NamedLoadRunning(runner, entry, name) for name in sorted(runner.detector.names())])


class NamedLoadRunning(BinarySensorEntity):
    """On while any signature filed under this name is running. Signatures
    that share a name are one device, so the entity is per NAME."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        self._runner = runner
        self._name = name
        self._attr_translation_key = "named_load_running"
        self._attr_unique_id = f"{entry.entry_id}_load_{name.lower().replace(' ', '_')}"
        self._attr_device_info = _child_device(runner.hass, entry, f"load_{name}", name, "Detected load")

    async def async_added_to_hass(self) -> None:
        self._runner.add_listener(self.async_write_ha_state)

    @property
    def is_on(self) -> bool:
        return self._name in self._runner.detector.active_by_name(dt_util.utcnow().timestamp())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        det = self._runner.detector
        ids = det.names().get(self._name, [])
        sigs = [s for s in det.signatures if s.id in ids]
        return {
            "signatures": ids,
            "watts": round(det.active_by_name(dt_util.utcnow().timestamp()).get(self._name, 0.0)),
            "sessions_seen": sum(s.count for s in sigs),
            "phases": "".join(sorted({p for s in sigs for p in s.phases})).upper(),
        }
