"""The forecast, published the way the solar forecasts are."""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN
from .coordinator import InsightsCoordinator, InsightsData
from .insights.profile import Forecast


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: InsightsCoordinator = hass.data[DOMAIN][entry.entry_id]
    add([
        ForecastPowerSensor(coordinator, entry, "consumption_forecast", "consumption"),
        ForecastEnergySensor(coordinator, entry, "consumption_today", "consumption", "today_kwh"),
        ForecastEnergySensor(coordinator, entry, "consumption_tomorrow", "consumption", "tomorrow_kwh"),
        ForecastPowerSensor(coordinator, entry, "remainder_forecast", "remainder"),
    ])


class _Base(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, key: str, which: str) -> None:
        super().__init__(coordinator)
        self._which = which
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_NAME, DEFAULT_NAME),
            manufacturer="Load Insights",
            model="Consumption forecast",
        )

    def _forecast(self) -> Optional[Forecast]:
        data: Optional[InsightsData] = self.coordinator.data
        if data is None:
            return None
        return getattr(data, self._which)

    @property
    def available(self) -> bool:
        return super().available and self._forecast() is not None


class ForecastPowerSensor(_Base):
    """State: expected average power over the coming hour, in W.
    Attributes: the 7-day hourly forecast in the shape the solar forecast
    integrations use, so a card built for one plots the other."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> Optional[float]:
        fc = self._forecast()
        return None if fc is None or fc.next_hour_w is None else round(fc.next_hour_w)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fc = self._forecast()
        data: Optional[InsightsData] = self.coordinator.data
        if fc is None or data is None:
            return {}
        attrs: dict[str, Any] = {
            "detailedForecast": [{"period_start": t.isoformat(), "kwh": round(v, 3)} for t, v in fc.hourly],
            "today_kwh": round(fc.today_kwh, 2),
            "tomorrow_kwh": round(fc.tomorrow_kwh, 2),
            "level_correction": round(fc.level, 3),
            "weeks_of_data": round(fc.span_weeks, 1),
            "hours_fitted": fc.sample_count,
            "computed_at": data.computed_at.isoformat(),
        }
        if self._which == "remainder":
            attrs["subtracted_devices"] = [d.label for d in data.site.remainder_devices()]
            # Before this instant at least one device was not yet metered, so
            # its energy is inside the remainder for those hours - by design.
            attrs["remainder_complete_since"] = (
                data.remainder_complete_since.isoformat() if data.remainder_complete_since else None
            )
            if data.devices_without_statistics:
                attrs["devices_without_statistics"] = list(data.devices_without_statistics)
        return attrs


class ForecastEnergySensor(_Base):
    """Today's or tomorrow's total: actual for the completed hours, forecast
    for the rest. No state_class on purpose - it is a forecast, not a meter."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry, key, which, field) -> None:
        super().__init__(coordinator, entry, key, which)
        self._field = field

    @property
    def native_value(self) -> Optional[float]:
        fc = self._forecast()
        return None if fc is None else round(getattr(fc, self._field), 2)
