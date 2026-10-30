"""The forecast, published the way the solar forecasts are."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import EVENT_RECORDER_5MIN_STATISTICS_GENERATED, get_instance
from homeassistant.components.recorder import statistics as rec_stats
from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import MATCH_ALL, PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (BASE_LOAD_GRID, BASE_LOAD_MIN_W, CONF_BASE_LOAD, CONF_BASE_LOAD_PHASES,
                    CONF_BATTERY_CAPACITY_ENTITY, CONF_HEAT_DEVICES, DOMAIN)
from homeassistant.util import dt as dt_util

from .insights.heatnudge import parse_devices
from .insights.planning import rate_from_wh, room_to_absorb

from .coordinator import REMAINDER_KEY, SITE_KEY, InsightsCoordinator, InsightsData
from .detection import DetectionRunner, device_uid, load_uid, loaded
from .insights.detect import (PF_MIN_QUANTA, PHASES, SYSTEM_PHASES, display_phase, ref_label, running_markdown,
                              signature_rows, suggest_levels)
from .insights.model import SiteModel
from .insights.named import carry_reading
from .insights.profile import PART_MIN_WEEKS, Forecast
from .insights.scoring import LEADS, Ledger


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: InsightsCoordinator = entry.runtime_data.coordinator
    # One sensor per thing forecast: the state is the prediction for the
    # coming hour, the attributes carry the rest - the horizon, the spread,
    # the recent actuals, the score. Daily totals, import and export figures
    # and the individual score numbers were sensors of their own once; they
    # are all attributes already, and a template sensor makes one where a
    # dashboard wants it (2026-09-17).
    entities = [
        ForecastPowerSensor(coordinator, entry, "consumption_forecast", "consumption"),
        ForecastPowerSensor(coordinator, entry, "remainder_forecast", "remainder"),
        GridForecastSensor(coordinator, entry, "grid_forecast"),
    ]
    # One per device the Energy dashboard lists. A device added to the
    # dashboard later appears after a reload of the integration; a removed
    # one keeps its entity, unavailable. Read from the dashboard itself, as
    # the first refresh does, rather than from that refresh: setup no longer
    # waits for it (2026-09-29), and each shows unavailable until it lands.
    site = SiteModel.from_prefs((await async_get_manager(hass)).data)
    # The state of charge is only forecastable where the Energy dashboard
    # gives a battery SOC and a capacity (or a capacity sensor is set). A site with no battery was
    # getting the sensor anyway, permanently unavailable (the owner,
    # 2026-09-17), so it is created only where it can have a value.
    if site.battery_soc and (site.battery_capacity_kwh or entry.options.get(CONF_BATTERY_CAPACITY_ENTITY)):
        entities.append(BatterySocForecastSensor(coordinator, entry, "battery_soc_forecast"))
    else:
        _forget(hass, "sensor", f"{entry.entry_id}_battery_soc_forecast")
    # SOC planning needs the sun forecast the Energy dashboard links and the
    # pack's size; without both there is nothing to plan from.
    if site.solar_forecast_entries and (site.battery_capacity_kwh or entry.options.get(CONF_BATTERY_CAPACITY_ENTITY)):
        entities += [PlanSensor(coordinator, entry, key, which) for key, which in PLAN_ENTITIES
                     if which != "excess" or site.battery_soc]      # the excess needs the SOC
        entities += [SunWindowSensor(coordinator, entry, *e) for e in SUN_ENTITIES]
        if site.battery_soc:
            entities.append(SunWindowSensor(coordinator, entry, *FULL_ENTITY))
        else:
            for key in ("daily_excess", FULL_ENTITY[0]):
                _forget(hass, "sensor", f"{entry.entry_id}_{key}")      # made before a SOC was linked
    else:
        for key in [e[0] for e in PLAN_ENTITIES + SUN_ENTITIES] + [FULL_ENTITY[0]]:
            _forget(hass, "sensor", f"{entry.entry_id}_{key}")
    entities += [DeviceForecastSensor(coordinator, entry, d) for d in site.devices]
    entities += [DeviceRoomSensor(coordinator, entry, d) for d in site.devices]
    entities += [DeviceRateSensor(coordinator, entry, d) for d in site.devices]
    # Heat nudge, shadow mode: per configured heat device the COP index and the scorecard (the
    # would-nudge-now binary sensor is on the binary_sensor platform). A device taken off the
    # options page loses its entities here, so none is left unavailable in the registry.
    heat = parse_devices(entry.options.get(CONF_HEAT_DEVICES))
    live = {heat_uid(entry.entry_id, d["id"], w) for d in heat for w in ("index", "scorecard", "nudge")}
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (ent.unique_id or "").startswith(f"{entry.entry_id}_heatnudge_") and ent.unique_id not in live:
            registry.async_remove(ent.entity_id)
    entities += [e for d in heat for e in (HeatIndexSensor(coordinator, entry, d), HeatScorecardSensor(coordinator, entry, d))]
    detection: DetectionRunner = entry.runtime_data.runner
    entities += [DetectedLoadsSensor(detection, entry), SignatureLibrarySensor(detection, entry), UnknownLoadPowerSensor(detection, entry)]
    entities += [BaseLoadSensor(detection, entry)]
    entities += _base_loads(hass, entry, detection)
    device_names = {d.label.casefold() for d in site.devices}
    for n in detection.fleet.entity_names():
        if n.casefold() in device_names:
            # the dashboard device of that name has its own room sensor, from the meter's statistics
            _forget(hass, "sensor", load_uid(entry.entry_id, "room", n))
            _forget(hass, "sensor", load_uid(entry.entry_id, "rate", n))
        if detection.metered_device(n):
            # named after a meter holding one device, it IS that device:
            # the meter's readings and its forecast stand for it, and
            # detected ones beside them only duplicate it (the owner,
            # 2026-09-29: "i would expect that we only add forecasting")
            _forget(hass, "sensor", load_uid(entry.entry_id, "power", n))
            _forget(hass, "sensor", load_uid(entry.entry_id, "energy", n))
            _forget(hass, "sensor", load_uid(entry.entry_id, "room", n))
            _forget(hass, "sensor", load_uid(entry.entry_id, "rate", n))
            continue
        entities += [NamedLoadPower(detection, entry, n), NamedLoadEnergy(detection, entry, n)]
        if n.casefold() not in device_names:
            entities += [NamedLoadRoom(detection, entry, n), NamedLoadRate(detection, entry, n)]
    add(entities)
    known = set(detection.fleet.entity_names())

    @callback
    def _follow_names() -> None:
        """A name that lands after setup gets its entities at once. After a
        reset the library is empty at setup and the carried names find
        their loads only during the re-read; until 2026-09-30 every named
        load then stayed unavailable until the next reload.

        Not while the entry unloads: a pass still running across a reload
        that re-attached a carried name would add its entities to the platform
        going away, and leave a stale twin the next one refuses."""
        if entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS:
            return
        new = [n for n in detection.fleet.entity_names() if n not in known]
        if not new:
            return
        known.update(new)
        add([e for n in new if not detection.metered_device(n)
             for e in (NamedLoadPower(detection, entry, n), NamedLoadEnergy(detection, entry, n),
                       *([] if n.casefold() in device_names else
                         [NamedLoadRoom(detection, entry, n), NamedLoadRate(detection, entry, n)]))])

    entry.async_on_unload(detection.async_add_listener(_follow_names))


def _forget(hass: HomeAssistant, domain: str, unique_id: str) -> None:
    """Drop an entity this site cannot have, so an install that once created
    it is not left with an unavailable leftover in the registry."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    if entity_id:
        registry.async_remove(entity_id)


def on_site(entity: Entity, entry: ConfigEntry, key: str, unique_id: Optional[str] = None) -> None:
    """Make ``entity`` one of the site device's, named by its translation
    ``key``. The device itself is made by async_setup_entry before any
    platform, so the identifiers are all an entity needs to join it."""
    entity._attr_has_entity_name = True
    entity._attr_translation_key = key
    entity._attr_unique_id = unique_id or f"{entry.entry_id}_{key}"
    entity._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})


class _PowerW(SensorEntity):
    """A power reading in watts, shown whole."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0


class _Base(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, key: str, which: str) -> None:
        super().__init__(coordinator)
        self._which = which
        on_site(self, entry, key)

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


class ForecastPowerSensor(_PowerW, _Base):
    """State: expected average power over the coming hour, in W.
    Attributes: the 7-day hourly forecast in the shape the solar forecast
    integrations use, so a card built for one plots the other.

    The house forecast and a meter's with parts inside it are BUILT: the
    meter's own part plus its mature parts' forecasts (``parts``). Their
    level, temperature and signals attributes describe the fit of the whole,
    which supplies only the band."""

    # ~10 KB of attributes changing every quarter hour on every forecast
    # sensor would go into the recorder with each state - for nothing, since
    # nothing reads them back from history. They stay live and on cards. All of
    # them: signals, score, the temperature response and the rest were ~6 KiB
    # a state on ~40 sensors at Home, ~20 MB of rows a day on a box short of
    # memory and flagged for I/O pressure (2026-10-30)
    _unrecorded_attributes = frozenset({MATCH_ALL})


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
                # the evidence behind it (INS-15): each coefficient's t over day blocks,
                # the t it needed at the days the fit had
                "t_heating": round(fc.temperature.t_heating, 2),
                "t_cooling": round(fc.temperature.t_cooling, 2),
                "t_needed": round(fc.temperature.t_needed, 2),
                "days_fitted": fc.temperature.days,
                "reason": fc.temperature.reason,
                "hours_fitted": fc.temperature.hours,
                "horizon_hours_with_forecast": fc.hours_with_forecast_temperature,
                "weather_entity": data.weather_entity,
                "temperature_entity": data.temperature_entity,
                "temperature_basis": data.temperature_basis,
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
        if fc.parts:
            attrs["parts"] = list(fc.parts)       # what the (built) forecast is the sum of
        if self._which == "consumption" and data.forecast_left_out:
            attrs["left_out_devices"] = list(data.forecast_left_out)
        if self._which == "remainder":
            # a young top-level device (own fit under PART_MIN_WEEKS) stays in the remainder
            attrs["subtracted_devices"] = [
                d.label for d in data.site.remainder_devices()
                if d.label in data.forecast_left_out
                or (d.energy in data.devices and data.devices[d.energy].span_weeks >= PART_MIN_WEEKS)]
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

    Enabled, like every other sensor here (2026-09-17). They were
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
        self._attr_unique_id = device_uid(entry.entry_id, device.energy)
        # Its own device, named after the dashboard's device and hanging off
        # the site's. Putting the forecast ON the real device (by reusing that
        # device's identifiers) is nicer and was tried on 2026-09-17: it
        # produced nine unnamed devices instead of merging, so it is out until
        # it can be tested against a live registry rather than reasoned about.
        self._attr_device_info = _child_device(
            entry, f"device_{device.energy}", device.label, "Device forecast"
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
                    "t": [round(x, 2) for x in fc.nowcast.t],
                    "days_fitted": fc.nowcast.days,
                    "deltas_kwh": [round(x, 3) for x in fc.nowcast_deltas],
                }
                if len(fc.nowcast.used) > 1:
                    # several states fitted together: one row per state kept
                    attrs["nowcast"]["kwh_per_unit_by_lead_by_state"] = [
                        [round(row[j], 4) for row in fc.nowcast.matrix] for j in range(len(fc.nowcast.used))]
                    attrs["nowcast"]["state_means"] = [round(m, 2) for m in fc.nowcast.means]
            # a thermostat load with a room model: today's and tomorrow's energy at each mode's setpoint
            # (away / normal / boost) and the expected figure between normal and boost (INS-24)
            modes = ((data.device_thermal or {}).get(self._energy) or {}).get("modes") if data else None
            if modes:
                attrs["modes"] = modes
        return attrs


class DeviceRoomSensor(CoordinatorEntity, SensorEntity):
    """How much more a metered device can take today (planning.room_from_series):
    its heavier days' energy less today's so far - or, for a tank or heater that
    Load Juggler runs, the degrees to its boost setpoint times its learned kWh per
    degree (insights/thermal.py). What Load Juggler's early start
    reads for a tank or heater on a metered plug, where the detector names no load
    to carry the sensor (NamedLoadRoom is for those it does)."""

    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, device) -> None:
        super().__init__(coordinator)
        self._energy = device.energy
        self._attr_has_entity_name = True
        self._attr_translation_key = "named_load_room"          # the same name and icon
        self._attr_unique_id = device_uid(entry.entry_id, device.energy) + "_room"
        self._attr_device_info = _child_device(
            entry, f"device_{device.energy}", device.label, "Device forecast"
        )

    @property
    def native_value(self) -> Optional[float]:
        data: Optional[InsightsData] = self.coordinator.data
        return None if data is None or not data.device_room else data.device_room.get(self._energy)

    @property
    def extra_state_attributes(self) -> dict:
        """Where the figure came from: a room's heat-loss model (INS-24; its C, UA and G), the
        thermostat's kWh per degree (a tank or heater that Load Juggler runs) or the history ceiling."""
        data: Optional[InsightsData] = self.coordinator.data
        thermal = (data.device_thermal or {}).get(self._energy) if data else None
        return {"source": "thermal" if thermal else "history",
                **{k: v for k, v in (thermal or {}).items() if k != "modes"}}      # the modes sit on the forecast sensor

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class DeviceRateSensor(CoordinatorEntity, SensorEntity):
    """What a metered device really pulls when it runs (planning.absorption_rate): the
    95th percentile of its active hours' mean power. What Load Juggler's early start
    counts as its power, instead of its rating."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, device) -> None:
        super().__init__(coordinator)
        self._energy = device.energy
        self._attr_has_entity_name = True
        self._attr_translation_key = "device_rate"
        self._attr_unique_id = device_uid(entry.entry_id, device.energy) + "_rate"
        self._attr_device_info = _child_device(
            entry, f"device_{device.energy}", device.label, "Device forecast"
        )

    @property
    def native_value(self) -> Optional[float]:
        data: Optional[InsightsData] = self.coordinator.data
        return None if data is None or not data.device_rate else data.device_rate.get(self._energy)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


def heat_uid(entry_id: str, device_id: str, what: str) -> str:
    return f"{entry_id}_heatnudge_{device_id}_{what}"


def _child_device(entry: ConfigEntry, key: str, name: str, model: str) -> DeviceInfo:
    """A device of its own, pointed at the site device by the registry id.

    ``via_device_id`` rather than the ``via_device`` tuple: the tuple form is
    deprecated and stops working in Home Assistant 2027.8."""
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{key}")},
        name=name,
        manufacturer="Load Insights",
        model=model,
    )
    rt = loaded(entry)
    if rt is not None:
        info["via_device_id"] = rt.site_device_id
    return info


class _DetectionBase(CoordinatorEntity, SensorEntity):
    """Fed by the detection runner rather than the forecast's coordinator: its
    cadence is the meter's, not the statistics'."""

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, key: str) -> None:
        super().__init__(runner)
        self._runner = runner
        on_site(self, entry, key)

    @property
    def available(self) -> bool:
        return self._runner.enabled and self._runner.last_run is not None


class DetectedLoadsSensor(_DetectionBase):
    """State: how many unexplained loads are on right now. Attributes: which,
    the last sessions, and where the backfill stands. The signature library is
    SignatureLibrarySensor's: moved off this sensor, which is rewritten every
    pass, because its 0.5 MB went to every connected dashboard each time."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    # EVERY attribute, not a chosen few. These are a live inspection surface -
    # the library, what each submeter saw, what is on right now - and none of
    # it is a measurement with a history worth keeping; the state, how many
    # unexplained loads are on, is. Naming three of them and letting the rest
    # through put the recorder over its 16 KB ceiling on a real site, which
    # logs a warning, refuses to store the attributes and says the database
    # will suffer - 989 times at the owner's house (2026-09-22). The list drifted
    # because it was a list, so it is the recorder's own "all of them".
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, runner, entry) -> None:
        super().__init__(runner, entry, "detected_loads")
        # HA opens an integration's entry from this, highlighted; it has no link into an options-flow step
        self._link = f"/config/integrations/integration/{DOMAIN}#config_entry={entry.entry_id}"

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
        rows = self._runner.fleet.running(now)
        running = {i for r in rows for m, i in r["refs"] if m == ""}
        return {
            # every load on now, once, as a list for a Markdown card (Fleet.running)
            "running_markdown": running_markdown(
                rows, self._link, {ref for _, offered, _ in self._runner.naming_groups() for ref, _ in offered}, now=now),
            "active": [
                {"phases": a["phases"].upper(), "watts": a["watts"], "since": iso(a["since"]),
                 "signature": a["signature"], "name": a.get("name")}
                for a in det.active(now)
            ],
            # a one-figure inverter's phases on the grid's, from its voltages: "X (L1)" per phase
            "inverter_phases": {n: {c.upper(): display_phase(p) for c, p in m.items() if p in PHASES}
                                for n, m in self._runner.fleet.inverter_phases.items()},
            # which signatures are running now: the library (Signature library sensor) holds nothing that
            # depends on the clock, so a dashboard joins the two
            "running_ids": sorted(running),
            # what each sub-meter has on now; its signatures are in the library sensor's "meters"
            "meters_active": {name: [{"phases": a["phases"].upper(), "watts": a["watts"], "since": iso(a["since"])}
                                     for a in d.active(now)]
                              for name, d in self._runner.fleet.subs.items()},
            "meter_hierarchy": self._runner.parents,
            "named_loads": {n: [ref_label(r) for r in refs] for n, refs in self._runner.fleet.names().items()},
            # names whose signature a reset took, still looking for the load
            # that wore them - visible so they are not silently in limbo
            "awaiting_name": [o.get("name") for o in self._runner.detector.orphan_names],
            # names detection gave by itself (insights/autoname.py), told from the owner's
            "auto_named": {n: m.get("kind") for n, m in sorted(self._runner.autonamer.marks.items())},
            "looks_like_one_device": suggest_levels(det.signatures, det.recent),
            "recent_sessions": [
                {**r, "start": iso(r["start"]), "end": iso(r["end"]), "phases": r["phases"].upper()} for r in det.recent[-40:]
            ],
            "noise_floor_w": {p.upper(): round(st.noise) for p, st in det.phases.items() if st.baseline is not None},
            "baseline_w": {p.upper(): round(st.baseline) for p, st in det.phases.items() if st.baseline is not None},
            # What each reading can RESOLVE, measured from its own history,
            # and the load size below which no power factor is believed
            # because the amps behind it cannot carry one. Both are here so
            # the gates can be checked against a site rather than assumed.
            "resolution_w": {p.upper(): round(st.quantum, 3) for p, st in det.phases.items() if st.quantum},
            "pf_floor_w": {p.upper(): round(PF_MIN_QUANTA * st.q_quantum) for p, st in det.phases.items() if st.q_quantum},
            "processed_until": self._runner.last_processed.isoformat() if self._runner.last_processed else None,
            "caught_up": self._runner.caught_up,
        }


class SignatureLibrarySensor(_DetectionBase):
    """State: how many signatures the detector holds. Attributes: every
    signature in words and figures, and each sub-meter's own.

    Moved off DetectedLoadsSensor (2026-10-25): that sensor is rewritten every
    pass and its signatures were 0.5 MB - every pass, to every connected
    dashboard and phone, and into the state machine. Nothing here depends on
    the clock (``signature_rows``), so a pass that filed nothing writes nothing.
    Not recorded, for the reason DetectedLoadsSensor's attributes are not."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, runner, entry) -> None:
        super().__init__(runner, entry, "signature_library")

    @property
    def native_value(self) -> int:
        return len(self._runner.detector.signatures)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        det = self._runner.detector
        tz = dt_util.DEFAULT_TIME_ZONE
        parents = self._runner.parents
        return {
            "signatures": signature_rows(det.signatures, tz, parents),
            "meters": {
                name: {
                    "signatures": [{"id": x.id, "name": x.name, "description": x.describe(tz), "count": x.count}
                                   for x in sorted(d.signatures, key=lambda y: -y.count)],
                    "baseline_w": {p.upper(): round(st.baseline) for p, st in d.phases.items() if st.baseline is not None},
                    "noise_floor_w": {p.upper(): round(st.noise) for p, st in d.phases.items() if st.baseline is not None},
                }
                for name, d in self._runner.fleet.subs.items()
            },
        }


class UnknownLoadPowerSensor(_PowerW, _DetectionBase):
    """Power of everything detected as on right now, in W."""


    def __init__(self, runner, entry) -> None:
        super().__init__(runner, entry, "unknown_load_power")

    @property
    def native_value(self) -> Optional[float]:
        return round(self._runner.detector.unknown_power(dt_util.utcnow().timestamp()))


class _GridBase(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        on_site(self, entry, key)

    def _grid(self):
        data: Optional[InsightsData] = self.coordinator.data
        return None if data is None else data.grid

    @property
    def available(self) -> bool:
        g = self._grid()
        return CoordinatorEntity.available.fget(self) and g is not None and bool(g.hours)


class GridForecastSensor(_PowerW, _GridBase):
    """State: the meter's expected average power over the coming hour, in W -
    positive importing, negative exporting. Attributes: the hourly detail."""

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


PLAN_ENTITIES = (("soc_target", "target"), ("soc_minimum", "minimum"), ("daily_excess", "excess"),
                 ("base_consumption", "base"))


SUN_ENTITIES = (("production_starts", "sun", "starts"), ("production_ends", "sun", "ends"))
FULL_ENTITY = ("next_full_charge", "full", "at")


class SunWindowSensor(CoordinatorEntity, SensorEntity):
    """When the sun forecast says production starts / stops on the day
    (insights/planning.py ``sun_window``): today's by day, tomorrow's once the
    day is over; and when the forecast has the pack next full (``full_at``),
    which is what an early-Excess load counts back from. Handy on a dashboard."""

    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, key: str, section: str, which: str) -> None:
        super().__init__(coordinator)
        self._section = section
        self._which = which
        on_site(self, entry, key)
        self._attr_device_class = SensorDeviceClass.TIMESTAMP

    def _reading(self) -> Optional[datetime]:
        data: Optional[InsightsData] = self.coordinator.data
        sun = (data.plan or {}).get(self._section) if data is not None else None
        iso = sun.get(self._which) if sun else None
        return dt_util.parse_datetime(iso) if iso else None

    @property
    def available(self) -> bool:
        return super().available and self._reading() is not None

    @property
    def native_value(self) -> Optional[datetime]:
        return self._reading()


class PlanSensor(CoordinatorEntity, SensorEntity):
    """One SOC planning reading (insights/planning.py): the target and the
    minimum in % of the pack, the daily excess in kWh (state: today's, the
    days in the ``days`` attribute). The values are RAW - negative where the
    sun outruns the pack - and the consumer clamps. When an hour of the
    horizon is missing the last reading stands, and ``held_since`` says so.

    The base consumption (``base``, W) rides on the same machinery and the
    same enablement - it needs the sun forecast the planning sensors need - but
    is the 10th percentile of the daylight hours of the house forecast, today or tomorrow
    (``base_consumption``): never held, and unknown while neither day has one."""

    _unrecorded_attributes = frozenset({"days", "inputs", "weights", "excess_kwh"})

    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, key: str, which: str) -> None:
        super().__init__(coordinator)
        self._which = which
        on_site(self, entry, key)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        if which == "excess":
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_suggested_display_precision = 1
        elif which == "base":
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_suggested_display_precision = 0
        else:
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_suggested_display_precision = 0

    def _reading(self) -> Optional[dict]:
        data: Optional[InsightsData] = self.coordinator.data
        return ((data.plan or {}).get(self._which)) if data is not None else None

    @property
    def available(self) -> bool:
        return super().available and self._reading() is not None

    @property
    def native_value(self) -> Optional[float]:
        r = self._reading()
        return None if r is None else r["value"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self._reading()
        if r is None:
            return {}
        # the base consumption is never held: unknown is an answer, so it has no held_since
        return {**r["attributes"], **({"held_since": r["held_since"]} if "held_since" in r else {})}


class _HeatBase(CoordinatorEntity, SensorEntity):
    """One heat device's shadow-mode reading (insights/heatnudge.py), from the coordinator's
    hourly refresh. Nothing here is sent to any device."""

    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, coordinator: InsightsCoordinator, entry: ConfigEntry, device: dict, what: str, key: str) -> None:
        super().__init__(coordinator)
        self._heat_id = device["id"]
        self._attr_has_entity_name = True
        self._attr_translation_key = key
        self._attr_unique_id = heat_uid(entry.entry_id, device["id"], what)
        self._attr_device_info = _child_device(entry, f"heat_{device['id']}", device["name"], "Heat device")

    def _reading(self) -> Optional[dict]:
        data: Optional[InsightsData] = self.coordinator.data
        return ((data.heat or {}).get(self._heat_id)) if data is not None else None


class HeatIndexSensor(_HeatBase):
    """State: the COP index of the hour under way, 1.00 being the day's average hour (above 1: a
    better hour to make heat). A PROXY from the outdoor temperature and the device's target, not a
    measured COP. Attributes: the hourly series for today and tomorrow, whether the shadow schedule
    would nudge now, the expected gain and why it is not computed if it is not."""

    _unrecorded_attributes = frozenset({MATCH_ALL})        # the series and the day summaries are live detail
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry, device: dict) -> None:
        super().__init__(coordinator, entry, device, "index", "heat_index")

    @property
    def available(self) -> bool:
        r = self._reading()
        return super().available and r is not None and r["index_now"] is not None

    @property
    def native_value(self) -> Optional[float]:
        r = self._reading()
        return None if r is None else r["index_now"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self._reading()
        if r is None:
            return {}

        def brief(day):
            return None if day is None else {k: v for k, v in day.items() if k not in ("hours", "nudge")} | {
                "nudge_hours": [datetime.fromtimestamp(k, dt_util.DEFAULT_TIME_ZONE).hour for k in day["nudge"]]}
        return {
            "would_nudge_now": r["would_nudge_now"], "cop_proxy_now": r["cop_now"], "frost_risk_now": r["frost_now"],
            "kind": r["kind"], "target_c": r["target_c"], "reading": r["reading"], "why": r["why"],
            "temperature_basis": r["temperature_basis"],
            "today": brief(r["today"]), "tomorrow": brief(r["tomorrow"]),
            "series": [{**{k: v for k, v in h.items() if k != "start"},
                        "start": datetime.fromtimestamp(h["start"], dt_util.DEFAULT_TIME_ZONE).isoformat(),
                        "nudge": h["start"] in d["nudge"]}
                       for d in (r["today"], r["tomorrow"]) if d for h in d["hours"]],
            "proxy": r["proxy"], "computed_at": r["computed_at"],
        }


class HeatScorecardSensor(_HeatBase):
    """State: the gap of the newest scored day, in degrees: the outdoor temperature the shadow
    schedule would have used, weighted by the electricity, less the one the device really used it
    at. Positive: a nudge would have used warmer hours. Attributes: the day's two figures, the
    means over the days measured, the extra peak a nudge would have caused, and the last 30 days."""

    _unrecorded_attributes = frozenset({MATCH_ALL})        # the 30 recent days are in the stored scorecard
    _attr_native_unit_of_measurement = "K"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry, device: dict) -> None:
        super().__init__(coordinator, entry, device, "scorecard", "heat_scorecard")

    def _last(self) -> Optional[dict]:
        r = self._reading()
        return ((r or {}).get("scorecard") or {}).get("last")

    @property
    def available(self) -> bool:
        return super().available and self._last() is not None

    @property
    def native_value(self) -> Optional[float]:
        last = self._last()
        return None if last is None else last["gap"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self._reading()
        card = (r or {}).get("scorecard") or {}
        last = card.get("last")
        if not last:
            return {"why": (r or {}).get("why"), "temperature_basis": (r or {}).get("temperature_basis")}
        return {"date": last["date"], "actual_weighted_temperature": last["actual_t"],
                "shadow_weighted_temperature": last["shadow_t"], "gap": last["gap"],
                "shadow_weighted_temperature_forecast": last["shadow_t_forecast"], "gap_forecast": last["gap_forecast"],
                "energy_kwh": last["energy_kwh"], "saving_pct": last["saving_pct"], "extra_peak_kw": last["extra_peak_kw"],
                "basis": last["basis"], "temperature_day_basis": last.get("t_basis", "uncorrected"),
                "days_measured": card["days_measured"], "days_modelled": card["days_modelled"],
                "days_uncorrected_temperature": card["days_uncorrected_temperature"],
                "temperature_basis": r["temperature_basis"],
                "mean_gap": card["mean_gap"], "mean_gap_forecast": card["mean_gap_forecast"],
                "mean_saving_pct": card["mean_saving_pct"], "mean_extra_peak_kw": card["mean_extra_peak_kw"],
                "recent": card["recent"], "why": r["why"]}


class BaseLoadSensor(_PowerW, _DetectionBase):
    """What the site draws with nothing switched on: the sum of each phase's
    idle baseline, which the detector tracks anyway to know when a load
    starts. Creeping upward is the thing to watch."""


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


def _base_loads(hass: HomeAssistant, entry: ConfigEntry, runner: DetectionRunner) -> list:
    """Each meter's idle floor as a load of its own (CONF_BASE_LOAD, the owner
    2026-10-30): power and energy on a "Base load" device - the meter's whole,
    or one per phase where it is split. Not a floor known to be under
    BASE_LOAD_MIN_W, and gone when its meter is unticked or split the other
    way; one not learned yet (a rebuild) is kept."""
    chosen, split = runner.config.get(CONF_BASE_LOAD), set(runner.config.get(CONF_BASE_LOAD_PHASES) or [])
    out, keep = [], set()
    for meter, det in runner.fleet.detectors():
        key = (runner.submeters.get(meter) or {}).get("energy") if meter else BASE_LOAD_GRID
        if not key or (chosen is not None and key not in chosen):
            continue
        for p in (sorted(det.phases) if key in split and len(det.phases) > 1 else [None]):
            for kind in ("power", "energy"):
                keep.add(_base_uid(entry, key, p, kind))
            w, _ = runner.fleet.base_load(meter, p)
            if w is None or w >= BASE_LOAD_MIN_W:
                out += [BaseLoadPower(runner, entry, meter, key, p), BaseLoadEnergy(runner, entry, meter, key, p)]
            else:
                keep -= {_base_uid(entry, key, p, kind) for kind in ("power", "energy")}
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (ent.unique_id or "").startswith(f"{entry.entry_id}_baseload_") and ent.unique_id not in keep:
            registry.async_remove(ent.entity_id)
    return out


def _base_uid(entry: ConfigEntry, key: str, phase: Optional[str], kind: str) -> str:
    return f"{entry.entry_id}_baseload_{kind}_{key.replace('.', '_')}_{phase or 'all'}"


class _BaseLoad(_DetectionBase):
    """One meter's base load - its floor - or one phase of it, as a load."""

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, meter: str, key: str,
                 phase: Optional[str], kind: str) -> None:
        super().__init__(runner, entry, f"named_load_{kind}")
        self._meter, self._phase = meter, phase
        self._attr_unique_id = _base_uid(entry, key, phase, kind)
        label = "Base load" if not meter else f"{meter} base load"
        if phase is not None:
            house = runner.fleet.phase_map(meter).get(phase, phase) if meter else phase
            label += f" {SYSTEM_PHASES.get(house, phase.upper())}"
        self._attr_device_info = _child_device(entry, f"baseload_{key}_{phase or 'all'}", label, "Base load")


class BaseLoadPower(_PowerW, _BaseLoad):
    def __init__(self, runner, entry, meter, key, phase) -> None:
        super().__init__(runner, entry, meter, key, phase, "power")

    @property
    def native_value(self) -> Optional[float]:
        w, _ = self._runner.fleet.base_load(self._meter, self._phase)
        return None if w is None else round(w)


class BaseLoadEnergy(_BaseLoad, RestoreSensor):
    """What the base load has used: the floor times the time, counted up as
    a named load's energy is (carry_reading) - never down, not while history
    is re-read."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(self, runner, entry, meter, key, phase) -> None:
        super().__init__(runner, entry, meter, key, phase, "energy")
        self._reading: Optional[float] = None
        self._seen: Optional[float] = None

    @property
    def native_value(self) -> Optional[float]:
        return self._reading

    def _total(self) -> float:
        return self._runner.fleet.base_load(self._meter, self._phase)[1] / 1000.0

    @callback
    def _handle_coordinator_update(self) -> None:
        self._reading, self._seen = carry_reading(self._reading, self._seen, self._total(),
                                                  not self._runner.refiling)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_sensor_data()
        try:
            self._reading = float(last.native_value)
        except (AttributeError, TypeError, ValueError):
            pass
        self._reading, self._seen = carry_reading(self._reading, None, self._total(), False)
        await super().async_added_to_hass()


class NamedLoadPower(_PowerW, _DetectionBase):
    """What a named load draws now (2026-10-13): its open runs from the
    moment their start is declared, plus the runs that began and ended between
    two passes, spread over the pass - see LIVE_NAME_SHARE.

    It was the energy of CLOSED runs divided by the pass (2026-09-18), never
    an open step - a step up with no partner had sat high for a day, and a
    run that began and ended inside one read was missed. With one-minute
    passes that booked a whole run into the minute it closed: a 15-minute
    fridge cycle read 1 kW for a minute and 0 the rest. An open run now
    counts only where its start's kind has been filed under the name, and
    stops counting once it is far longer than any run of the name; the runs
    no pass saw open still come in as their energy.
    """


    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        super().__init__(runner, entry, "named_load_power")
        self._name = name
        self._attr_unique_id = load_uid(entry.entry_id, "power", name)
        self._attr_device_info = _child_device(entry, f"load_{name}", name, "Detected load")

    @property
    def native_value(self) -> Optional[float]:
        value = self._runner.live_power.get(self._name)
        if value is None:
            # a name waiting for its load after a rebuild draws nothing, it is not unknown
            return 0 if self._name not in self._runner.fleet.names() else None
        return round(value)


class NamedLoadRoom(_DetectionBase):
    """How much more a named load can take today (planning.room_to_absorb): its
    heavier days' energy less today's so far. What Load Juggler's early start
    reads to count only the energy a load can still absorb - a tank that has
    already heated, an EVSE that has had its fill."""

    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        super().__init__(runner, entry, "named_load_room")
        self._name = name
        self._attr_unique_id = load_uid(entry.entry_id, "room", name)
        self._attr_device_info = _child_device(entry, f"load_{name}", name, "Detected load")

    @property
    def native_value(self) -> Optional[float]:
        return room_to_absorb(self._runner.fleet.hourly_by_name(self._name), dt_util.now())

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class NamedLoadRate(_DetectionBase):
    """What a named load really pulls when it runs (planning.rate_from_wh): the
    95th percentile of its active hours' mean power, from the same hourly energy
    NamedLoadRoom reads (Fleet.hourly_by_name, the last HOURLY_KEEP_S days of
    its filed runs). The counterpart of DeviceRateSensor for loads the detector
    names; what Load Juggler's early start counts as the load's power instead of
    its rating (INS-3)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        super().__init__(runner, entry, "named_load_rate")
        self._name = name
        self._attr_unique_id = load_uid(entry.entry_id, "rate", name)
        self._attr_device_info = _child_device(entry, f"load_{name}", name, "Detected load")

    @property
    def native_value(self) -> Optional[float]:
        return rate_from_wh(self._runner.fleet.hourly_by_name(self._name))

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class NamedLoadEnergy(_DetectionBase, RestoreSensor):
    """What a named load has used, all told.

    Naming a load already gave it a device and a power reading; without an
    ENERGY reading beside it the device cannot appear on the Energy
    dashboard, which is where anyone would go to ask what the thing costs
    (2026-09-18).

    It counts up by what detection gains and nothing else - never down, and
    not while detection re-reads history after a reset - across restarts
    too; see named.carry_reading. The history is the statistics rewrite's.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(self, runner: DetectionRunner, entry: ConfigEntry, name: str) -> None:
        super().__init__(runner, entry, "named_load_energy")
        self._name = name
        self._attr_unique_id = load_uid(entry.entry_id, "energy", name)
        self._attr_device_info = _child_device(entry, f"load_{name}", name, "Detected load")
        self._reading: Optional[float] = None
        self._seen: Optional[float] = None

    @property
    def native_value(self) -> Optional[float]:
        return self._reading

    def _total(self) -> float:
        return self._runner.fleet.energy_by_name().get(self._name, 0.0) / 1000.0

    @callback
    def _handle_coordinator_update(self) -> None:
        self._reading, self._seen = carry_reading(self._reading, self._seen, self._total(),
                                                  not self._runner.refiling)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_sensor_data()
        try:
            self._reading = float(last.native_value)
        except (AttributeError, TypeError, ValueError):
            pass
        # Never below the reading Home Assistant last compiled: the next
        # compile counts from it, and one restored lower - or not at all, as
        # the first start after 0.4.1-dev.20260929.1455, whose sensor kept
        # nothing to restore - books the difference as a drop.
        compiled = await get_instance(self.hass).async_add_executor_job(
            rec_stats.get_last_short_term_statistics, self.hass, 1, self.entity_id, False, {"state"})
        compiled = ((compiled.get(self.entity_id) or [{}])[0]).get("state")
        if compiled is not None and (self._reading is None or compiled > self._reading):
            self._reading = float(compiled)
        # the library as stored, so what the first pass adds - the time Home
        # Assistant was down - counts
        self._reading, self._seen = carry_reading(self._reading, None, self._total(), False)
        await super().async_added_to_hass()      # each pass's update from here on
        self._runner.entry.async_create_background_task(
            self.hass, self._backfill_when_new(), f"{DOMAIN} backfill {self.entity_id}")

    async def _backfill_when_new(self) -> None:
        """A meter is held against what detection credits its load with as it
        is added - at a start, and at the reload every naming makes - and
        every hour from then on (DetectionRunner._check_named). A new meter's
        statistics start at its first recorded hour, and the Energy dashboard
        with them, though detection saw the load for days before it was
        named. So a meter with no hour of its own yet - one just named - is
        backfilled once it has one: tried each time Home Assistant has
        compiled statistics, every five minutes, until its first hour is in
        (the event fires before the compile commits, so the hour may only
        show five minutes on); as is one added while history is re-read, once
        the re-read is done."""
        if await self._runner.async_backfill_statistics(self._name, force=False) is not None:
            return
        done = asyncio.Event()

        async def compiled(_event) -> None:
            if not done.is_set() and \
                    await self._runner.async_backfill_statistics(self._name, force=False) is not None:
                done.set()

        remove = self.hass.bus.async_listen(EVENT_RECORDER_5MIN_STATISTICS_GENERATED, compiled)
        try:
            await done.wait()
        finally:
            remove()
