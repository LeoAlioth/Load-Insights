"""One binary sensor per named load: is it running right now."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_HEAT_DEVICES
from .detection import DetectionRunner
from .insights.detect import ref_label
from .insights.heatnudge import parse_devices
from .sensor import _child_device, _forget, heat_uid


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    runner: DetectionRunner = entry.runtime_data.runner
    entities = []
    for name in runner.fleet.entity_names():
        if runner.metered_device(name):          # that device: see the sensor platform
            _forget(hass, "binary_sensor", _running_uid(entry.entry_id, name))
        else:
            entities.append(NamedLoadRunning(runner, entry, name))
    entities += [HeatNudgeNow(entry.runtime_data.coordinator, entry, d)
                 for d in parse_devices(entry.options.get(CONF_HEAT_DEVICES))]
    add(entities)


def _running_uid(entry_id: str, name: str) -> str:
    return f"{entry_id}_load_{name.lower().replace(' ', '_')}"


class NamedLoadRunning(CoordinatorEntity, BinarySensorEntity):
    """On while a running load is filed under this name, read one load at a
    time from the grid meter's runs (Fleet.running) - the same answer as the
    dashboard's list, whatever else shares its phase. Signatures that share a
    name are one device, so the entity is per NAME."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        super().__init__(runner)
        self._runner = runner
        self._name = name
        self._attr_translation_key = "named_load_running"
        self._attr_unique_id = _running_uid(entry.entry_id, name)
        self._attr_device_info = _child_device(entry, f"load_{name}", name, "Detected load")

    @property
    def is_on(self) -> bool:
        return self._name in self._runner.fleet.active_by_name(dt_util.utcnow().timestamp())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fleet = self._runner.fleet
        refs = fleet.names().get(self._name, [])
        sigs = [s for s in (fleet.signature(r) for r in refs) if s is not None]
        return {
            # the grid's by id, a meter's as "Mansarda#11"; its phases are its own meter's
            "signatures": [ref_label(r) for r in refs],
            "watts": round(fleet.active_by_name(dt_util.utcnow().timestamp()).get(self._name, 0.0)),
            "sessions_seen": sum(s.count for s in sigs),
            "phases": "".join(sorted({p for s in sigs for p in s.phases})).upper(),
        }


class HeatNudgeNow(CoordinatorEntity, BinarySensorEntity):
    """SHADOW MODE: on while the shadow schedule would nudge this heat device now (the hour under
    way is one that takes more heat than expected, insights/heatnudge.py). Nothing is sent to the
    device; Load Juggler reads this to decide, later, whether to act."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"reason"})

    def __init__(self, coordinator, entry: ConfigEntry, device: dict) -> None:
        super().__init__(coordinator)
        self._heat_id = device["id"]
        self._attr_translation_key = "heat_nudge_now"
        self._attr_unique_id = heat_uid(entry.entry_id, device["id"], "nudge")
        self._attr_device_info = _child_device(entry, f"heat_{device['id']}", device["name"], "Heat device")

    def _reading(self) -> Any:
        data = self.coordinator.data
        return ((data.heat or {}).get(self._heat_id)) if data is not None else None

    @property
    def available(self) -> bool:
        r = self._reading()
        return super().available and r is not None and r["today"] is not None

    @property
    def is_on(self) -> bool:
        r = self._reading()
        return bool(r and r["would_nudge_now"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self._reading() or {}
        today = r.get("today") or {}
        return {"reason": r.get("why"), "expected_saving_pct_today": today.get("saving_pct")}
