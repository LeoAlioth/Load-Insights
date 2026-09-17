"""The forecast, published the way the solar forecasts are."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN
from homeassistant.util import dt as dt_util

from .coordinator import REMAINDER_KEY, SITE_KEY, InsightsCoordinator, InsightsData
from .detection import DetectionRunner
from .insights.detect import most_specific, suggest_levels
from .insights.profile import Forecast
from .insights.scoring import BAND_LEAD_H, LEADS, LEADS_H, Ledger


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: InsightsCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ForecastPowerSensor(coordinator, entry, "consumption_forecast", "consumption"),
        ForecastEnergySensor(coordinator, entry, "consumption_today", "consumption", "today_kwh"),
        ForecastEnergySensor(coordinator, entry, "consumption_tomorrow", "consumption", "tomorrow_kwh"),
        ForecastPowerSensor(coordinator, entry, "remainder_forecast", "remainder"),
    ]
    # Forecast scores: the few numbers worth a card or an alert. Per-device
    # scores are attributes on the device forecast sensors instead.
    entities += [
        ScoreSensor(coordinator, entry, "consumption_error_day_ahead", SITE_KEY, "mae", LEADS["day_ahead"]),
        ScoreSensor(coordinator, entry, "consumption_bias_day_ahead", SITE_KEY, "bias", LEADS["day_ahead"]),
        ScoreSensor(coordinator, entry, "consumption_error_hour_ahead", SITE_KEY, "mae", LEADS["hour_ahead"]),
        DayAheadErrorSensor(coordinator, entry, "consumption_day_ahead_kwh_error", SITE_KEY),
        ScoreSensor(coordinator, entry, "remainder_error_day_ahead", REMAINDER_KEY, "mae", LEADS["day_ahead"]),
        ScoreSensor(coordinator, entry, "remainder_bias_day_ahead", REMAINDER_KEY, "bias", LEADS["day_ahead"]),
    ]
    # One per device the Energy dashboard lists, from the site model of the
    # first refresh. A device added to the dashboard later appears after a
    # reload of the integration; a removed one keeps its entity, unavailable.
    data: InsightsData = coordinator.data
    if data is not None:
        entities += [DeviceForecastSensor(coordinator, entry, d) for d in data.site.devices]
    detection: DetectionRunner = hass.data[DOMAIN].get(f"{entry.entry_id}_detection")
    if detection is not None:
        entities += [DetectedLoadsSensor(detection, entry), UnknownLoadPowerSensor(detection, entry)]
        entities += [NamedLoadPower(detection, entry, n) for n in sorted(detection.detector.names())]
    add(entities)


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

    # ~10 KB of attributes changing every quarter hour on every forecast
    # sensor would go into the recorder with each state - for nothing, since
    # nothing reads them back from history. They stay live and on cards.
    _unrecorded_attributes = frozenset({"detailedForecast", "history"})

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
            # kwh is the weighted MEAN (means add up, so the daily totals are
            # exact); kwh_p10 / kwh_p90 are the slot's weighted percentiles -
            # the spread, in Solcast's naming convention. Percentiles do not
            # add, so the daily sensors carry no band.
            "detailedForecast": [
                {"period_start": t.isoformat(), "kwh": round(v, 3), "kwh_p10": round(b[0], 3), "kwh_p90": round(b[1], 3)}
                for (t, v), b in zip(fc.hourly, fc.bands)
            ],
            # The last two days as they actually happened, same shape, so one
            # entity feeds both halves of an actual-vs-forecast chart.
            "history": [{"period_start": t.isoformat(), "kwh": round(v, 3)} for t, v in fc.history],
            "today_kwh": round(fc.today_kwh, 2),
            "tomorrow_kwh": round(fc.tomorrow_kwh, 2),
            "level_correction": round(fc.level, 3),
            "weeks_of_data": round(fc.span_weeks, 1),
            "hours_fitted": fc.sample_count,
            "computed_at": data.computed_at.isoformat(),
            # Public holidays are scored as Sundays; None here means the
            # holidays library is not installed (no Workday integration).
            "holidays_modelled": data.holidays_known,
            "holidays_in_horizon": list(data.holidays_in_horizon),
            # The temperature response fitted for THIS series, or zeros when
            # it did not pass the guard (or no pair is configured).
            "temperature_response": {
                "engaged": fc.temperature.engaged,
                "heating_w_per_degree": round(fc.temperature.heating_kwh_per_degh * 1000.0, 1),
                "cooling_w_per_degree": round(fc.temperature.cooling_kwh_per_degh * 1000.0, 1),
                "residual_explained": round(fc.temperature.explained, 3),
                "hours_fitted": fc.temperature.hours,
                "horizon_hours_with_forecast": fc.hours_with_forecast_temperature,
                "weather_entity": data.weather_entity,
                "temperature_entity": data.temperature_entity,
            },
            # Each linked calendar's fitted role for THIS series: existence
            # factors per hour of day (or the single factor), then any title
            # that earned a factor of its own. Not engaged = no effect.
            "calendars": [
                {
                    "entity": m.entity,
                    "engaged": m.engaged,
                    "on_hours_in_window": (data.calendar_on_hours or {}).get(m.entity),
                    "existence": {
                        "engaged": m.existence.engaged,
                        # contrast = while on, relative to while off: 0.4 is
                        # "the house runs at 40 % while this calendar is on"
                        "contrast": round(m.existence.contrast, 3),
                        "contrast_by_hour": [round(m.existence.contrast_at(h), 3) for h in range(24)] if m.existence.engaged else None,
                        "factor_on": round(m.existence.on, 3),
                        "factor_off": round(m.existence.off, 3),
                        "residual_explained": round(m.existence.explained, 3),
                        "on_hours": m.existence.on_hours,
                    },
                    "titles": {
                        t: {"factor": round(f.on, 3), "residual_explained": round(f.explained, 3), "on_hours": f.on_hours}
                        for t, f in m.titles.items()
                    },
                }
                for m in fc.calendars
            ],
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


def _score_attrs(led: Optional[Ledger], now) -> dict:
    """The scoring table for one series, as attributes."""
    if led is None:
        return {}
    out: dict = {"leads": {}}
    for name, lead in LEADS.items():
        m = led.metrics(now, lead)
        out["leads"][name] = {
            "lead_hours": lead,
            "n": m["n"],
            "mae_w": None if m["mae_w"] is None else round(m["mae_w"]),
            "bias_w": None if m["bias_w"] is None else round(m["bias_w"]),
        }
    cov = led.coverage(now)
    out["band_coverage_day_ahead"] = None if cov is None else round(cov, 3)
    day = led.last_day()
    if day:
        out["last_day"] = {"date": day[0], "actual_kwh": round(day[1], 2), "predicted_kwh": round(day[2], 2)}
    return out


class ScoreSensor(_Base):
    """A trailing-7-day error figure for one series and one lead, in W."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _unrecorded_attributes = frozenset({"recent"})

    def __init__(self, coordinator, entry, key, series_key, metric, lead) -> None:
        super().__init__(coordinator, entry, key, "consumption")
        self._series_key = series_key
        self._metric = metric
        self._lead = lead

    def _ledger(self) -> Optional[Ledger]:
        data: Optional[InsightsData] = self.coordinator.data
        return None if data is None or not data.ledgers else data.ledgers.get(self._series_key)

    @property
    def available(self) -> bool:
        led = self._ledger()
        return CoordinatorEntity.available.fget(self) and led is not None and led.metrics(self.coordinator.data.computed_at, self._lead)["n"] > 0

    @property
    def native_value(self) -> Optional[float]:
        led = self._ledger()
        if led is None:
            return None
        m = led.metrics(self.coordinator.data.computed_at, self._lead)
        v = m["mae_w"] if self._metric == "mae" else m["bias_w"]
        return None if v is None else round(v)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        led = self._ledger()
        data: Optional[InsightsData] = self.coordinator.data
        if led is None or data is None:
            return {}
        attrs = _score_attrs(led, data.computed_at)
        attrs["recent"] = [
            {"period_start": datetime.fromtimestamp(r["hour_key"], data.computed_at.tzinfo).isoformat(),
             "actual_kwh": round(r["actual"], 3), "predicted_kwh": round(r["predicted"], 3)}
            for r in led.recent(data.computed_at, self._lead if self._lead in LEADS_H else BAND_LEAD_H)
        ]
        return attrs


class DayAheadErrorSensor(ScoreSensor):
    """Yesterday's headline: actual minus the total the forecast showed for
    it at noon the day before, in kWh, signed. One number a day."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = None
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry, key, series_key) -> None:
        super().__init__(coordinator, entry, key, series_key, "day", LEADS["day_ahead"])

    @property
    def available(self) -> bool:
        led = self._ledger()
        return CoordinatorEntity.available.fget(self) and led is not None and led.last_day() is not None

    @property
    def native_value(self) -> Optional[float]:
        led = self._ledger()
        day = led.last_day() if led else None
        return None if day is None else round(day[1] - day[2], 2)


class DeviceForecastSensor(ForecastPowerSensor):
    """The same forecast, for one individually metered device.

    Disabled by default: a busy dashboard lists twenty devices and nobody
    wants a forecast of the bug lamp on a card - enable the two or three that
    matter from the device page. The unique id is built from the device's
    statistic id, which is what the dashboard itself keys the device by.
    """

    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, device) -> None:
        super().__init__(coordinator, entry, "device_forecast", "device")
        self._energy = device.energy
        self._device = device
        self._attr_unique_id = f"{entry.entry_id}_device_{device.energy.replace('.', '_')}"
        self._attr_translation_placeholders = {"device": device.label}

    def _forecast(self) -> Optional[Forecast]:
        data: Optional[InsightsData] = self.coordinator.data
        if data is None or not data.devices:
            return None
        return data.devices.get(self._energy)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        if attrs:
            attrs["statistic_id"] = self._energy
            if self._device.included_in:
                attrs["included_in"] = self._device.included_in
            data: Optional[InsightsData] = self.coordinator.data
            if data is not None and data.ledgers and self._energy in data.ledgers:
                attrs["score"] = _score_attrs(data.ledgers[self._energy], data.computed_at)
            fc = self._forecast()
            st_entity = (data.device_state_sensors or {}).get(self._energy) if data else None
            if st_entity and fc is not None:
                # The device's own state and what it says about the next hours.
                attrs["nowcast"] = {
                    "state_entity": st_entity,
                    "current_value": (data.device_state_now or {}).get(self._energy),
                    "engaged": fc.nowcast.engaged,
                    "kwh_per_unit_by_lead": [round(c, 4) for c in fc.nowcast.coefficients],
                    "residual_explained_by_lead": [round(e, 3) for e in fc.nowcast.explained],
                    "state_mean": round(fc.nowcast.state_mean, 2) if fc.nowcast.engaged else None,
                    "hours_fitted": fc.nowcast.hours,
                    "deltas_kwh": [round(x, 3) for x in fc.nowcast_deltas],
                }
        return attrs


class _DetectionBase(SensorEntity):
    """Fed by the detection runner rather than the coordinator: its cadence is
    the meter's, not the statistics'."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, key: str) -> None:
        self._runner = runner
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        self._runner.add_listener(self.async_write_ha_state)

    @property
    def available(self) -> bool:
        return self._runner.enabled and self._runner.last_run is not None


class DetectedLoadsSensor(_DetectionBase):
    """State: how many unexplained loads are on right now. Attributes: which,
    the signature library in words, the last sessions, and where the backfill
    stands."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _unrecorded_attributes = frozenset({"active", "signatures", "recent_sessions"})

    def __init__(self, runner, entry) -> None:
        super().__init__(runner, entry, "detected_loads")

    @property
    def native_value(self) -> Optional[int]:
        return len(self._runner.detector.active(dt_util.utcnow().timestamp()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        det = self._runner.detector
        now = dt_util.utcnow().timestamp()
        tz = dt_util.DEFAULT_TIME_ZONE
        def iso(ts):
            return datetime.fromtimestamp(ts, tz).isoformat()
        return {
            "active": [
                {"phases": a["phases"].upper(), "watts": a["watts"], "since": iso(a["since"]),
                 "signature": a["signature"], "name": a.get("name")}
                for a in det.active(now)
            ],
            "signatures": [
                {"id": s.id, "name": s.name, "description": s.describe(tz), "phases": s.phases.upper(),
                 "watts_by_phase": {p.upper(): round(w) for p, w in s.power.items()}, "count": s.count,
                 "typical_duration_s": round(s.duration_s), "typical_interval_s": None if s.interval_s is None else round(s.interval_s),
                 "pf": None if s.pf is None else round(s.pf, 2), "last_seen": iso(s.last_seen), "hours": s.hours,
                 # the downstream meter that also saw it, or "main" (upstream of every submeter)
                 "location": most_specific(s.locations, s.count, self._runner.parents),
                 "seen_by": dict(s.locations)}
                for s in sorted(det.signatures, key=lambda x: -x.count)
            ],
            "meters": {
                name: {
                    "signatures": [{"id": x.id, "description": x.describe(tz), "count": x.count} for x in sorted(d.signatures, key=lambda y: -y.count)],
                    "baseline_w": {p.upper(): round(st.baseline) for p, st in d.phases.items() if st.baseline is not None},
                    "noise_floor_w": {p.upper(): round(st.noise) for p, st in d.phases.items() if st.baseline is not None},
                    "active": [{"phases": a["phases"].upper(), "watts": a["watts"], "since": iso(a["since"])} for a in d.active(now)],
                }
                for name, d in self._runner.fleet.subs.items()
            },
            "meter_hierarchy": self._runner.parents,
            "named_loads": self._runner.detector.names(),
            "looks_like_one_device": suggest_levels(det.signatures, det.recent),
            "recent_sessions": [
                {**r, "start": iso(r["start"]), "end": iso(r["end"]), "phases": r["phases"].upper()} for r in det.recent[-40:]
            ],
            "noise_floor_w": {p.upper(): round(st.noise) for p, st in det.phases.items() if st.baseline is not None},
            "baseline_w": {p.upper(): round(st.baseline) for p, st in det.phases.items() if st.baseline is not None},
            "processed_until": self._runner.last_processed.isoformat() if self._runner.last_processed else None,
            "caught_up": self._runner.caught_up,
        }


class UnknownLoadPowerSensor(_DetectionBase):
    """Power of everything detected as on right now, in W."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, runner, entry) -> None:
        super().__init__(runner, entry, "unknown_load_power")

    @property
    def native_value(self) -> Optional[float]:
        return round(self._runner.detector.unknown_power(dt_util.utcnow().timestamp()))


class NamedLoadPower(_DetectionBase):
    """Watts a named load is drawing right now, 0 when it is off."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        super().__init__(runner, entry, "named_load_power")
        self._name = name
        self._attr_name = f"{name} power"
        self._attr_unique_id = f"{entry.entry_id}_load_power_{name.lower().replace(' ', '_')}"

    @property
    def native_value(self) -> Optional[float]:
        return round(self._runner.detector.active_by_name(dt_util.utcnow().timestamp()).get(self._name, 0.0))
