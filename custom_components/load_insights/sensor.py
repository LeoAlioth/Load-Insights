"""The forecast, published the way the solar forecasts are."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN
from homeassistant.util import dt as dt_util

from .coordinator import REMAINDER_KEY, SITE_KEY, InsightsCoordinator, InsightsData
from .detection import DetectionRunner
from .insights.detect import describe_location, location_confidence, most_specific, suggest_levels
from .insights.profile import Forecast
from .insights.scoring import BAND_LEAD_H, LEADS, LEADS_H, Ledger


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: InsightsCoordinator = hass.data[DOMAIN][entry.entry_id]
    # One sensor per thing forecast: the state is the prediction for the
    # coming hour, the attributes carry the rest - the horizon, the spread,
    # the recent actuals, the score. Daily totals, import and export figures
    # and the individual score numbers were sensors of their own once; they
    # are all attributes already, and a template sensor makes one where a
    # dashboard wants it (Anze, 2026-09-17).
    entities = [
        ForecastPowerSensor(coordinator, entry, "consumption_forecast", "consumption"),
        ForecastPowerSensor(coordinator, entry, "remainder_forecast", "remainder"),
        GridForecastSensor(coordinator, entry, "grid_forecast"),
    ]
    # One per device the Energy dashboard lists, from the site model of the
    # first refresh. A device added to the dashboard later appears after a
    # reload of the integration; a removed one keeps its entity, unavailable.
    data: InsightsData = coordinator.data
    if data is not None:
        # The state of charge is only forecastable where the Energy dashboard
        # gives both a battery SOC and a capacity. A site with no battery was
        # getting the sensor anyway, permanently unavailable (Anze,
        # 2026-09-17), so it is created only where it can have a value.
        if data.site.battery_soc and data.site.battery_capacity_kwh:
            entities.append(BatterySocForecastSensor(coordinator, entry, "battery_soc_forecast"))
        else:
            _forget(hass, entry, "battery_soc_forecast")
        entities += [DeviceForecastSensor(coordinator, entry, d) for d in data.site.devices]
    detection: DetectionRunner = hass.data[DOMAIN].get(f"{entry.entry_id}_detection")
    if detection is not None:
        entities += [DetectedLoadsSensor(detection, entry), UnknownLoadPowerSensor(detection, entry)]
        entities += [BaseLoadSensor(detection, entry)]
        for n in sorted(detection.detector.names()):
            entities += [NamedLoadPower(detection, entry, n), NamedLoadEnergy(detection, entry, n)]
    add(entities)


def _forget(hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
    """Drop a sensor this site cannot have, so an install that once created
    it is not left with an unavailable leftover in the registry."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    if entity_id:
        registry.async_remove(entity_id)


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
            model="Site",
        )

    def _forecast(self) -> Optional[Forecast]:
        data: Optional[InsightsData] = self.coordinator.data
        if data is None:
            return None
        return getattr(data, self._which)

    def _ledger_key(self) -> str:
        """Which scoring ledger belongs to this sensor."""
        return SITE_KEY if self._which == "consumption" else REMAINDER_KEY

    def _predictions(self) -> dict:
        data: Optional[InsightsData] = self.coordinator.data
        if data is None or not data.ledgers:
            return {}
        led = data.ledgers.get(self._ledger_key())
        return led.predictions() if led else {}

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
            # The last two days as they actually happened, each row also
            # carrying what the forecast said for that hour a DAY BEFORE it -
            # from the scoring ledger, so it is what was really predicted at
            # the time, not this morning's fit re-run over its own history.
            # null until scoring has settled that hour (about two days in).
            "history": [
                {"period_start": t.isoformat(), "kwh": round(v, 3),
                 "predicted": (lambda x: None if x is None else round(x, 3))(self._predictions().get(t.timestamp()))}
                for t, v in fc.history
            ],
            "today_kwh": round(fc.today_kwh, 2),
            "tomorrow_kwh": round(fc.tomorrow_kwh, 2),
            "level_correction": round(fc.level, 3),
            "weeks_of_data": round(fc.span_weeks, 1),
            "hours_fitted": fc.sample_count,
            "computed_at": data.computed_at.isoformat(),
            # Public holidays are scored as Sundays; None here means the
            # holidays library is not installed (no Workday integration).
            "score": _score_attrs((data.ledgers or {}).get(self._ledger_key()), data.computed_at),
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
            # Every explanatory signal's fitted role for THIS series - linked
            # calendars and attached sensors alike. A sensor's "titles" are
            # its states, or its quantile bands when it is numeric with many
            # values. Not engaged = no effect on the forecast.
            "signals": [
                {
                    "entity": m.entity,
                    "kind": next((g.kind for g in (data.calendar_signals or ()) if g.entity == m.entity), "calendar"),
                    "reading": (data.input_kinds or {}).get(m.entity),
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


class DeviceForecastSensor(ForecastPowerSensor):
    """The same forecast, for one individually metered device.

    Enabled, like every other sensor here (Anze, 2026-09-17). They were
    disabled by default on the theory that a busy dashboard would flood the
    registry - but a device is on the Energy dashboard because its owner
    cares about it, the dashboard template builds a card per device, and an
    entity nobody looks at costs a few hundred bytes. Hiding them only made
    the first thing anyone wants to do take twenty clicks first.

    The unique id is built from the device's statistic id, which is what the
    dashboard itself keys the device by.
    """

    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, device) -> None:
        super().__init__(coordinator, entry, "device_forecast", "device")
        self._energy = device.energy
        self._device = device
        self._attr_unique_id = f"{entry.entry_id}_device_{device.energy.replace('.', '_')}"
        # Its own device, named after the dashboard's device and hanging off
        # the site's. Putting the forecast ON the real device (by reusing that
        # device's identifiers) is nicer and was tried on 2026-09-17: it
        # produced nine unnamed devices instead of merging, so it is out until
        # it can be tested against a live registry rather than reasoned about.
        self._attr_device_info = _child_device(
            coordinator.hass, entry, f"device_{device.energy}", device.label, "Device forecast"
        )

    def _forecast(self) -> Optional[Forecast]:
        data: Optional[InsightsData] = self.coordinator.data
        if data is None or not data.devices:
            return None
        return data.devices.get(self._energy)

    def _ledger_key(self) -> str:
        return self._energy

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


def _child_device(hass, entry: ConfigEntry, key: str, name: str, model: str) -> DeviceInfo:
    """A device of its own, pointed at the site device by the registry id.

    ``via_device_id`` rather than the ``via_device`` tuple: the tuple form is
    deprecated and stops working in Home Assistant 2027.8."""
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{key}")},
        name=name,
        manufacturer="Load Insights",
        model=model,
    )
    parent = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_site_device")
    if parent:
        info["via_device_id"] = parent
    return info


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
                 "pf": None if s.pf is None else round(s.pf, 2), "last_seen": iso(s.last_seen),
                 # watt-hours per hour of day, and per weekday from Monday
                 "hour_wh": [round(x) for x in s.hour_wh], "day_wh": [round(x) for x in s.day_wh],
                 # the downstream meter that also saw it, or "main" (upstream of every submeter)
                 "location": most_specific(s.locations, s.count, self._runner.parents),
                 "where": describe_location(s.locations, s.count, self._runner.parents, s.phases),
                 "seen_by": dict(s.locations),
                 # three separate scores: whether it is a real repeating load,
                 # what kind of thing it might be, and where it lives. They
                 # answer different questions, so they are not blended.
                 "evidence": s.evidence, "regular": s.regular,
                 "guess": s.guess().to_dict(),
                 "where_confidence": location_confidence(s.locations, s.count, self._runner.parents)}
                # same order as the naming page: best evidence first
                for s in sorted(det.signatures, key=lambda x: (-x.evidence, -x.count))
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


class _GridBase(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    def _grid(self):
        data: Optional[InsightsData] = self.coordinator.data
        return None if data is None else data.grid

    @property
    def available(self) -> bool:
        g = self._grid()
        return CoordinatorEntity.available.fget(self) and g is not None and bool(g.hours)


class GridForecastSensor(_GridBase):
    """State: the meter's expected average power over the coming hour, in W -
    positive importing, negative exporting. Attributes: the hourly detail."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _unrecorded_attributes = frozenset({"detailedForecast"})

    @property
    def native_value(self) -> Optional[float]:
        g = self._grid()
        return None if not g or not g.hours else round(1000.0 * g.hours[0].net_kwh)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        g = self._grid()
        data: Optional[InsightsData] = self.coordinator.data
        if not g or data is None:
            return {}
        return {
            "detailedForecast": [
                {"period_start": h.when.isoformat(),
                 "kwh": round(h.net_kwh, 3),
                 "consumption_kwh": round(h.consumption_kwh, 3),
                 "pv_kwh": round(h.pv_kwh, 3),
                 "battery_kwh": round(h.battery_kwh, 3),
                 "soc": None if h.soc is None else round(h.soc, 1)}
                for h in g.hours
            ],
            "import_kwh": round(g.import_kwh, 2),
            "export_kwh": round(g.export_kwh, 2),
            "battery_modelled": g.battery_modelled,
            "hours_with_pv_forecast": g.pv_hours,
            "pv_forecast_entries": list(data.site.solar_forecast_entries),
        }


class BatterySocForecastSensor(_GridBase):
    """The pack's expected state of charge at the end of the coming hour."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _unrecorded_attributes = frozenset({"detailedForecast"})

    @property
    def available(self) -> bool:
        g = self._grid()
        return super().available and g.battery_modelled

    @property
    def native_value(self) -> Optional[float]:
        g = self._grid()
        return None if not g or not g.hours or g.hours[0].soc is None else round(g.hours[0].soc)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        g = self._grid()
        if not g or not g.battery_modelled:
            return {}
        socs = [h.soc for h in g.hours if h.soc is not None]
        return {
            "detailedForecast": [
                {"period_start": h.when.isoformat(), "soc": round(h.soc, 1)}
                for h in g.hours if h.soc is not None
            ],
            "minimum": round(min(socs), 1) if socs else None,
            "maximum": round(max(socs), 1) if socs else None,
        }


class BaseLoadSensor(_DetectionBase):
    """What the site draws with nothing switched on: the sum of each phase's
    idle baseline, which the detector tracks anyway to know when a load
    starts. Creeping upward is the thing to watch."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, runner, entry) -> None:
        super().__init__(runner, entry, "base_load")

    @property
    def available(self) -> bool:
        return super().available and any(st.baseline is not None for st in self._runner.detector.phases.values())

    @property
    def native_value(self) -> Optional[float]:
        vals = [st.baseline for st in self._runner.detector.phases.values() if st.baseline is not None]
        return round(sum(vals)) if vals else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        det = self._runner.detector
        return {
            "by_phase": {p.upper(): round(st.baseline) for p, st in det.phases.items() if st.baseline is not None},
            "noise_floor_w": {p.upper(): round(st.noise) for p, st in det.phases.items() if st.baseline is not None},
            "by_meter": {
                name: round(sum(st.baseline for st in d.phases.values() if st.baseline is not None))
                for name, d in self._runner.fleet.subs.items()
            },
        }


class NamedLoadPower(_DetectionBase):
    """Mean watts a named load drew over the stretch of data last processed.

    Not the instantaneous power, which was the first design and was three
    ways unreliable (Anze, 2026-09-18): it spoke on a step UP without waiting
    for the matching step down; detection reads the recorder every five
    minutes, so a load that started AND finished inside one window was
    already closed when we looked and never showed at all - the kiln, 44
    seconds every two minutes, was essentially never caught; and a step up
    whose partner never arrived sat high for as much as a day.

    An average over the interval has none of that. It is the energy that
    arrived divided by the time it covers, so it counts only sessions that
    CLOSED - short loads included, at their true share of the window - and a
    load that did nothing reads zero rather than whatever was last left open.
    It also integrates back to the energy meter beside it, which the
    instantaneous reading never did.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        super().__init__(runner, entry, "named_load_power")
        self._name = name
        self._attr_unique_id = f"{entry.entry_id}_load_power_{name.lower().replace(' ', '_')}"
        self._attr_device_info = _child_device(runner.hass, entry, f"load_{name}", name, "Detected load")

    @property
    def native_value(self) -> Optional[float]:
        # None until two passes have run: one pass gives a total, not a rate
        value = self._runner.average_power.get(self._name)
        return None if value is None else round(value)


class NamedLoadEnergy(_DetectionBase):
    """What a named load has used, all told.

    Naming a load already gave it a device and a power reading; without an
    ENERGY reading beside it the device cannot appear on the Energy
    dashboard, which is where anyone would go to ask what the thing costs
    (Anze, 2026-09-18).

    The figure is sound as a meter rather than merely plausible: a
    signature's hour_wh only ever accumulates, a merge sums both sides, and
    a named signature is never evicted - so it cannot go backwards except
    when detection is reset, which is a real meter reset and is exactly what
    TOTAL_INCREASING means.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        super().__init__(runner, entry, "named_load_energy")
        self._name = name
        self._attr_unique_id = f"{entry.entry_id}_load_energy_{name.lower().replace(' ', '_')}"
        self._attr_device_info = _child_device(runner.hass, entry, f"load_{name}", name, "Detected load")

    @property
    def native_value(self) -> Optional[float]:
        return self._runner.detector.energy_by_name().get(self._name, 0.0) / 1000.0
