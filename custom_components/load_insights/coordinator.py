"""Fetches the site's hourly statistics on the quarter hours and forecasts."""
from __future__ import annotations

import functools

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.energy.websocket_api import async_get_energy_platforms
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder import statistics as rec_stats
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import EnergyConverter, TemperatureConverter

from .const import (
    CONF_BATTERY_CAPACITY_ENTITY,
    CONF_CALENDAR_ENTITIES,
    CONF_DETECTION,
    CONF_FORECAST_EXCLUDE,
    CONF_ROOM_THERMOSTATS,
    JUGGLER_BOOST_OPTION,
    JUGGLER_BOOST_SUFFIX,
    JUGGLER_CLIMATE,
    JUGGLER_DEVICE_TYPE,
    JUGGLER_DOMAIN,
    JUGGLER_TANK,
    JUGGLER_TANK_POWER,
    CONF_INPUT_ENTITIES,
    CONF_INPUT_LINKS,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PLAN_CONSUMPTION_SCALE,
    CONF_PLAN_DAYS,
    CONF_PLAN_FULL_SOC,
    CONF_PLAN_FULL_SOC_ENTITY,
    CONF_PLAN_MINIMUM_FLOOR,
    CONF_PLAN_PRODUCTION_SCALE,
    CONF_PLAN_SPREAD,
    PLAN_DEFAULTS,
    PLAN_SENSORS,
    PLAN_WEIGHTS,
    plan_key,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    HISTORY_WEEKS,
    CONF_INVERTERS,
    CONF_LAYOUT,
    HORIZON_HOURS,
    LAYOUT_ALIASES,
    LAYOUT_PARALLEL,
    LAYOUT_SERIES,
    REFRESH_MINUTES,
    REFRESH_SECOND,
)
from .insights.calendars import CalendarSignals
from .insights.inputs import label_history, project, usable
from .insights.covariates import interpolate_hourly
from .insights.detect import site_topology
from .insights.grid import GridForecast, build as build_grid
from .detection import named_load_energy
from .insights.planning import plan_all, room_from_series, spread_evenly, sun_window
from .insights.model import LOAD_PREFIX, SiteModel, own_scoped, scoped
from .insights.profile import Forecast, assemble, floor_hour, forecast, hour_buckets
from .insights.scoring import Ledger
from .insights.thermal import KINDS, heating_runs, kwh_per_degree, room_from_thermal
from .insights.series import combine, coverage, subtract_all
from .repairs import async_check

STORAGE_VERSION = 1
SITE_KEY = "consumption"
REMAINDER_KEY = "remainder"

_LOGGER = logging.getLogger(__name__)

# a planning reading that cannot be refreshed stands for this long, then the sensor goes unavailable
PLAN_HOLD_MAX = timedelta(hours=12)


def holiday_dates(country: Optional[str], years) -> Optional[Set[date]]:
    """Public holidays for ``country`` over ``years``, or None when they cannot
    be known. Uses the ``holidays`` library that Home Assistant's Workday
    integration depends on - so it is present wherever Workday is set up and
    absent otherwise, in which case holidays are simply not modelled. Read
    for the whole history AND the horizon, which a binary sensor's ten days
    of recorder history could never give."""
    if not country:
        return None
    try:
        import holidays as _holidays  # noqa: PLC0415  optional, see above
    except ImportError:
        return None
    try:
        return set(_holidays.country_holidays(country, years=list(years)).keys())
    except Exception as exc:  # noqa: BLE001  an unknown country code, most likely
        _LOGGER.warning("Holidays for %s not available: %s", country, exc)
        return None


@dataclass
class InsightsData:
    site: SiteModel
    consumption: Forecast
    remainder: Optional[Forecast]
    computed_at: datetime
    remainder_complete_since: Optional[datetime] = None
    devices_without_statistics: tuple = ()
    holidays_known: bool = False
    forecast_left_out: tuple = ()      # labels of the devices never added to the house forecast
    consumption_fitted: Optional[Forecast] = None   # the house series fitted on its own: supplies the band, kept for comparison
    own_parts: Dict[str, Forecast] = None  # type: ignore[assignment]   # each meter's own part (its series less its mature parts)
    holidays_in_horizon: tuple = ()
    weather_entity: Optional[str] = None
    temperature_entity: Optional[str] = None
    temperature_history_hours: int = 0
    temperature_forecast_hours: int = 0
    calendar_entities: tuple = ()
    input_entities: tuple = ()
    input_kinds: Dict[str, str] = None  # type: ignore[assignment]
    calendar_signals: tuple = ()
    calendar_on_hours: Dict[str, int] = None  # type: ignore[assignment]
    device_state_sensors: Dict[str, str] = None  # type: ignore[assignment]
    device_state_now: Dict[str, Optional[float]] = None  # type: ignore[assignment]
    # series key -> Ledger (site, remainder, each device by statistic id)
    ledgers: Dict[str, Ledger] = None  # type: ignore[assignment]
    grid: Optional[GridForecast] = None
    # SOC planning: sensor (target / minimum / excess) -> {value, attributes, held_since}; None while it has never read
    plan: Dict[str, Optional[dict]] = None  # type: ignore[assignment]
    # One forecast per individually metered device, keyed by its statistic id.
    # A device with no statistics yet has no entry, and its sensor stays
    # unavailable rather than showing a forecast of nothing.
    devices: Dict[str, Forecast] = None  # type: ignore[assignment]
    # kWh each individually metered device can still take today (planning.room_from_series), by statistic id
    device_room: Dict[str, Optional[float]] = None  # type: ignore[assignment]
    # what the thermal reading of a tank or heater (insights/thermal.py) gave, by statistic id: room, kwh_per_degree, spread, runs, kind
    device_thermal: Dict[str, dict] = None  # type: ignore[assignment]


class InsightsCoordinator(DataUpdateCoordinator):
    """One refresh per quarter hour, aligned to the clock rather than to the
    previous refresh, so every run sees the statistics for a just-closed
    period and the forecast lands on the same grid as the tariff blocks."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self._unsub = async_track_time_change(
            hass, self._on_quarter, minute=list(REFRESH_MINUTES), second=REFRESH_SECOND
        )
        # The scoring ledgers persist in .storage: a forecast recorded today is
        # only scored when its hour arrives, up to a week later.
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.scoring")
        self._ledgers: Optional[Dict[str, Ledger]] = None
        self._capacity_kwh: Optional[float] = None  # the capacity sensor's last good reading
        self._plan_last: Dict[str, dict] = {}       # each planning sensor's last complete reading
        self._thermal_said: Dict[str, str] = {}      # each Load Juggler load's last thermal-room reason, to log changes only

    def _battery_capacity(self) -> Optional[float]:
        """The capacity sensor's kWh, held at its last good reading while it is
        unreadable; None (the dashboard's figure stands) before it has read."""
        eid = self.config_entry.options.get(CONF_BATTERY_CAPACITY_ENTITY)
        st = self.hass.states.get(eid) if eid else None
        try:
            kwh = EnergyConverter.convert(float(st.state), st.attributes.get("unit_of_measurement"),
                                          UnitOfEnergy.KILO_WATT_HOUR)
        except (AttributeError, TypeError, ValueError, HomeAssistantError):
            kwh = None
        if kwh is not None and kwh > 0:
            self._capacity_kwh = kwh
        return self._capacity_kwh

    async def _load_ledgers(self) -> Dict[str, Ledger]:
        if self._ledgers is None:
            raw = await self._store.async_load() or {}
            self._ledgers = {k: Ledger.from_dict(v) for k, v in (raw.get("ledgers") or {}).items()}
        return self._ledgers

    async def async_follow_renames(self, renames: Dict[str, str]) -> None:
        """A device's forecast score follows its statistic when it is renamed."""
        ledgers = await self._load_ledgers()
        for old, new in renames.items():
            if old in ledgers and new not in ledgers:
                ledgers[new] = ledgers.pop(old)
        await self._save_ledgers()

    async def _save_ledgers(self) -> None:
        if self._ledgers is not None:
            await self._store.async_save({"ledgers": {k: v.to_dict() for k, v in self._ledgers.items()}})

    @callback
    def _on_quarter(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    async def async_shutdown(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await super().async_shutdown()

    async def _async_update_data(self) -> InsightsData:
        manager = await async_get_manager(self.hass)
        site = SiteModel.from_prefs(manager.data)
        if not site.has_sources:
            raise UpdateFailed("the Energy dashboard has no grid source configured")
        capacity = self._battery_capacity()
        if capacity:
            site = replace(site, battery_capacity_kwh=capacity)

        now = dt_util.now()
        start = now - timedelta(weeks=HISTORY_WEEKS)
        end_h = now + timedelta(hours=HORIZON_HOURS + 1)
        hols = await self.hass.async_add_executor_job(
            holiday_dates, self.hass.config.country, range(start.year, end_h.year + 1)
        )
        ids = site.all_statistic_ids()
        # Hourly "change" of each energy statistic, normalised to kWh whatever
        # unit the sensor reports in. Recorder work runs on its own executor.
        rows = await get_instance(self.hass).async_add_executor_job(
            rec_stats.statistics_during_period,
            self.hass, start, None, ids, "hour", {"energy": "kWh"}, {"change"},
        )
        series = {sid: _rows_to_samples(rows.get(sid, []), now.tzinfo) for sid in ids}

        # --- the temperature pair: sensor history, weather-entity horizon ---
        opts = self.config_entry.options if self.config_entry else {}
        temp_entity = opts.get(CONF_OUTDOOR_TEMPERATURE_ENTITY)
        weather_entity = opts.get(CONF_WEATHER_ENTITY)
        temps_hist: Dict[float, float] = {}
        temps_fc: Dict[float, float] = {}
        if temp_entity and weather_entity:
            temps_hist = await self._hourly_means(temp_entity, start, {"temperature": UnitOfTemperature.CELSIUS})
            temps_fc = await self._forecast_temperatures(weather_entity, now)

        # --- calendars: every linked one, over history and horizon ---
        cal_entities = tuple(opts.get(CONF_CALENDAR_ENTITIES) or ())
        hour_keys = [b.timestamp() for b in hour_buckets(floor_hour(start), int((end_h - floor_hour(start)).total_seconds() // 3600) + 1)]
        cal_signals: List[CalendarSignals] = []
        for cal in cal_entities:
            events = await self._calendar_events(cal, start, end_h)
            cal_signals.append(CalendarSignals.from_events(cal, events, hour_keys))

        # --- any other attached entity, fitted the same way ---
        input_entities = tuple(opts.get(CONF_INPUT_ENTITIES) or ())
        input_kinds: Dict[str, str] = {}
        horizon_keys = [b.timestamp() for b in hour_buckets(floor_hour(now), HORIZON_HOURS)]
        for eid in input_entities:
            signal, input_kinds[eid] = await self._input_signal(eid, start, now, horizon_keys)
            if signal is not None:
                cal_signals.append(signal)

        consumption = combine(series, site.consumption_terms())
        if not consumption:
            raise UpdateFailed("no hourly consumption statistics yet")
        # Loads the owner leaves out ("Not in the house forecast") are never
        # added to the house forecast; what is scored against is the
        # consumption without them. (The remainder is already without every
        # listed device.)
        left_out = [e for e in (opts.get(CONF_FORECAST_EXCLUDE) or []) if e in series]
        scored = subtract_all(consumption, series, left_out) if left_out else consumption
        remainder_ids = [d.energy for d in site.remainder_devices()]
        complete_since, missing = coverage(series, remainder_ids) if remainder_ids else (None, [])
        labels = {d.energy: d.label for d in site.devices}

        # An input linked to devices acts on those, the meters they sit in
        # and the site (model.scoped); one linked to nothing, on everything.
        links = opts.get(CONF_INPUT_LINKS) or {}
        parent = {d.energy: d.included_in for d in site.devices}

        def within(target: str) -> List[str]:
            """The device a link names, and every device it sits inside."""
            cur = (named_load_energy(self.hass, self.config_entry, target[len(LOAD_PREFIX):])
                   if target.startswith(LOAD_PREFIX) else target)
            out: List[str] = []
            while cur and cur not in out:
                out.append(cur)
                cur = parent.get(cur)
            return out

        # The fit is pure Python over a few thousand rows - still, never on
        # the event loop.
        fit = lambda rows, signals: self.hass.async_add_executor_job(  # noqa: E731
            forecast, rows, now, HORIZON_HOURS, hols, temps_hist or None, temps_fc or None, signals or None
        )
        cons_fit = await fit(consumption, cal_signals)

        # --- scoring: settle what has arrived, record what is now predicted ---
        ledgers = await self._load_ledgers()

        def score(key: str, fc: Forecast, actual) -> None:
            led = ledgers.setdefault(key, Ledger())
            led.settle(now, actual)
            led.record(now, fc.hourly, fc.bands, fc.tomorrow_kwh)

        # Every listed device, nested ones included - the series are already
        # in hand for the remainder, so this is only the fits. A device with a
        # state sensor also gets its nowcast: the sensor's hourly means over
        # the same window, and its live value now.
        # A number linked to a device nudges its next hours, jointly with any
        # other linked to it (fit_joint keeps each only if it adds) - and its
        # parents' too, which the device is part of (2026-09-28: are
        # they "still taken into account by all of the parent meters?"). The
        # site takes every input, linked or not, among its signals above; a
        # device, the unlinked ones and those linked to it or to a device
        # inside it.
        linked: Dict[str, List[str]] = {}
        for eid in input_entities:
            if eid.split(".", 1)[0] in NUMERIC_STATE_DOMAINS:
                for target in links.get(eid) or []:
                    for stat in within(target):
                        if eid not in linked.setdefault(stat, []):
                            linked[stat].append(eid)
        means: Dict[str, Dict[float, float]] = {}
        state_used: Dict[str, str] = {}
        state_now: Dict[str, Optional[float]] = {}
        device_fc: Dict[str, Forecast] = {}
        for d in site.devices:
            rows = series.get(d.energy) or []
            if not rows:
                continue
            numbers: List[Tuple[str, Dict[float, float], Optional[float]]] = []
            for st_entity in linked.get(d.energy, []):
                if st_entity not in means:
                    means[st_entity] = await self._hourly_means(st_entity, start)
                if not means[st_entity]:
                    continue              # no hourly means: a state, among the signals already
                numbers.append((st_entity, means[st_entity], _read_number(self.hass, [st_entity])))
            fc = await self.hass.async_add_executor_job(
                forecast, rows, now, HORIZON_HOURS, hols, temps_hist or None, temps_fc or None,
                scoped(cal_signals, links, within, d.energy) or None,
                [h for _, h, _ in numbers] or None, [v for _, _, v in numbers] or None,
            )
            device_fc[d.energy] = fc
            if numbers:
                kept = [numbers[i] for i in fc.nowcast.used] if fc.nowcast.used else numbers[:1]
                state_used[d.energy] = ", ".join(e for e, _, _ in kept)
                state_now[d.energy] = kept[0][2] if len(kept) == 1 else None

        # --- the house, and every meter with parts, built from them: its own
        # part (its series less its mature parts) plus each mature part's
        # forecast, bottom-up (profile.assemble). The independent fit of the
        # whole supplies only the band.
        kids_of = {k: tuple(d.energy for d in site.parts_of(k)) for k in [None, *labels]}
        rows_of: Dict[Optional[str], list] = {None: consumption, **{k: v for k, v in series.items() if k in labels and v}}
        fitted: Dict[Optional[str], Forecast] = {None: cons_fit, **device_fc}
        fit_part = lambda stat, rows, signed: forecast(  # noqa: E731
            rows, now, HORIZON_HOURS, hols, temps_hist or None, temps_fc or None,
            own_scoped(cal_signals, links, within, stat) or None, signed=signed)
        fit_whole = lambda stat, rows: forecast(  # noqa: E731
            rows, now, HORIZON_HOURS, hols, temps_hist or None, temps_fc or None,
            (cal_signals if stat is None else scoped(cal_signals, links, within, stat)) or None)
        try:
            built, own, house = await self.hass.async_add_executor_job(
                assemble, rows_of, kids_of, left_out, fitted, fit_part, now, labels, fit_whole)
        except Exception:  # noqa: BLE001 - one bad fit must not take every forecast with it
            _LOGGER.exception("House forecast from its parts failed; using the independent fits")
            built, own, house = {}, {}, None
        device_fc.update({k: v for k, v in built.items() if k})
        cons_fc = house or cons_fit
        rem_rows, rem_fc = own.get(None, (None, None))

        score(SITE_KEY, cons_fc, scored)
        if rem_fc is not None:
            score(REMAINDER_KEY, rem_fc, rem_rows)
        for k, fc in device_fc.items():
            score(k, fc, rows_of[k])
        await self._save_ledgers()

        # --- what the meter will do: consumption less the PV the dashboard's
        # own forecast integrations predict, then the battery in between ---
        pv = await self._solar_forecast(site, now)
        soc = _read_number(self.hass, site.battery_soc)
        # Where the pack sits decides which conversions its energy pays for.
        # Only an EXPLICIT setting is used: the layout detection auto-derives
        # from the load reading, which at home is a house-consumption template
        # and says nothing about the battery - and reading it as series there
        # would quietly inflate consumption by the inverter's efficiency.
        opts = self.config_entry.options if self.config_entry else {}
        stored = (opts.get(CONF_DETECTION) or {}).get(CONF_LAYOUT)
        declared = site_topology(opts.get(CONF_INVERTERS) or [], LAYOUT_ALIASES.get(stored, stored))
        topology = declared if declared in (LAYOUT_PARALLEL, LAYOUT_SERIES) else LAYOUT_PARALLEL
        grid = await self.hass.async_add_executor_job(
            functools.partial(build_grid, cons_fc.hourly, pv, soc,
                              site.battery_capacity_kwh, topology=topology),
        )
        try:
            plan = self._plan(site, now, cons_fc, device_fc, left_out, pv, soc, opts)
        except Exception:  # noqa: BLE001 - the last readings stand, the forecasts go on
            _LOGGER.exception("SOC planning failed; keeping its last readings")
            plan = dict(self._plan_last)
        device_room = {d.energy: room_from_series(series.get(d.energy, ()), now) for d in site.devices}
        try:
            device_thermal = await self._thermal_rooms(site, series, now, start, opts)
        except Exception:  # noqa: BLE001 - the history ceilings stand, the forecasts go on
            _LOGGER.exception("Thermal room reading failed; keeping the history ceilings")
            device_thermal = {}
        device_room.update({sid: t["room"] for sid, t in device_thermal.items()})
        data = InsightsData(
            site=site, consumption=cons_fc, remainder=rem_fc, computed_at=now,
            remainder_complete_since=complete_since,
            devices_without_statistics=tuple(labels.get(m, m) for m in missing),
            devices=device_fc,
            device_room=device_room,
            device_thermal=device_thermal,
            forecast_left_out=tuple(labels.get(e, e) for e in left_out),
            consumption_fitted=cons_fit,
            own_parts={k: v[1] for k, v in own.items() if k},
            holidays_known=hols is not None,
            holidays_in_horizon=tuple(sorted(d.isoformat() for d in (hols or ()) if now.date() <= d <= end_h.date())),
            weather_entity=weather_entity,
            temperature_entity=temp_entity,
            temperature_history_hours=len(temps_hist),
            temperature_forecast_hours=len(temps_fc),
            ledgers=dict(ledgers),
            grid=grid,
            plan=plan,
            calendar_entities=cal_entities,
            input_entities=input_entities,
            input_kinds=input_kinds,
            calendar_signals=tuple(cal_signals),
            calendar_on_hours={sig.entity: len(sig.existence) for sig in cal_signals},
            device_state_sensors=state_used,
            device_state_now=state_now,
        )
        # things that are silently half-done get said out loud
        if self.config_entry is not None:
            async_check(self.hass, self.config_entry, data)
        return data

    def _plan(self, site: SiteModel, now: datetime, cons_fc: Forecast, device_fc: Dict[str, Forecast],
              left_out: List[str], pv: Dict[float, float], soc: Optional[float], opts) -> Dict[str, Optional[dict]]:
        """The three SOC planning readings (insights/planning.py) from the
        house forecast and the dashboard's sun forecast. One that cannot be
        read this time - an hour of the horizon is missing - keeps its last
        reading, and says since when (``held_since``)."""
        use = {t.timestamp(): v for t, v in cons_fc.hourly}
        spread = [{t.timestamp(): v for t, v in device_fc[s].hourly}
                  for s in opts.get(CONF_PLAN_SPREAD) or [] if s in device_fc and s not in left_out]
        get = lambda key: opts.get(key, PLAN_DEFAULTS[key])  # noqa: E731
        weights = {}
        for n in PLAN_SENSORS:
            full_h, derate = PLAN_WEIGHTS[n]
            weights[n] = tuple((opts.get(plan_key(n, side, "full_hours"), full_h), opts.get(plan_key(n, side, "derate"), derate))
                               for side in ("consumption", "production"))
        full = _read_number(self.hass, [opts.get(CONF_PLAN_FULL_SOC_ENTITY)]) if opts.get(CONF_PLAN_FULL_SOC_ENTITY) else None
        cfg = {"days": get(CONF_PLAN_DAYS), "production_scale": get(CONF_PLAN_PRODUCTION_SCALE),
               "consumption_scale": get(CONF_PLAN_CONSUMPTION_SCALE), "floor": get(CONF_PLAN_MINIMUM_FLOOR),
               "weights": weights}
        fresh = plan_all(now, spread_evenly(use, spread), pv, site.battery_capacity_kwh, soc,
                         full if full is not None else get(CONF_PLAN_FULL_SOC), cfg)
        names = {s: next((d.label for d in site.devices if d.energy == s), s) for s in opts.get(CONF_PLAN_SPREAD) or []}
        out: Dict[str, Optional[dict]] = {}
        for n, r in fresh.items():
            if r is not None:
                r["attributes"]["inputs"].update({"spread_evenly": [names[s] for s in names if s in device_fc and s not in left_out],
                                                  "left_out": [d.label for d in site.devices if d.energy in left_out]})
                self._plan_last[n] = {**r, "held_since": None}
            elif n in self._plan_last:
                last = self._plan_last[n]
                since = last.get("held_since") or now.isoformat()
                if now - dt_util.parse_datetime(since) > PLAN_HOLD_MAX:
                    del self._plan_last[n]       # an input broken for good must not serve a value for ever
                else:
                    self._plan_last[n] = {**last, "held_since": since}
            out[n] = self._plan_last.get(n)
        # when the forecast sun starts and stops: a function of the sun forecast alone,
        # not of the 120 hours or the pack the three readings above need
        starts, ends = sun_window(now, pv, get(CONF_PLAN_PRODUCTION_SCALE), now.tzinfo)
        out["sun"] = {"starts": starts, "ends": ends} if starts else None
        # when the pack is next full: read off the excess balance (held with it)
        full_at = ((out.get("excess") or {}).get("attributes") or {}).get("full_at")
        out["full"] = {"at": full_at} if full_at else None
        return out

    async def _solar_forecast(self, site: SiteModel, now: datetime) -> Dict[float, float]:
        """Hour key -> forecast PV kWh, from the forecast integrations the
        Energy dashboard already links to the PV source. This is the same
        data the dashboard draws as its dashed line, asked for the same way."""
        if not site.solar_forecast_entries:
            return {}
        try:
            platforms = await async_get_energy_platforms(self.hass)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Solar forecast platforms unavailable: %s", exc)
            return {}
        out: Dict[float, float] = {}
        for entry_id in site.solar_forecast_entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain not in platforms:
                continue
            try:
                fc = await platforms[entry.domain](self.hass, entry_id)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Solar forecast from %s failed: %s", entry.domain, exc)
                continue
            for iso, wh in ((fc or {}).get("wh_hours") or {}).items():
                t = dt_util.parse_datetime(iso)
                if t is None:
                    continue
                k = floor_hour(dt_util.as_local(t)).timestamp()
                # several arrays on one site sum, as the dashboard sums them
                out[k] = out.get(k, 0.0) + float(wh) / 1000.0
        return out

    async def _input_signal(self, eid: str, start: datetime, now: datetime,
                            horizon_keys: List[float]) -> Tuple[Optional[CalendarSignals], str]:
        """An attached entity as the fit takes it - its labelled history and
        the horizon's projected labels - and how it was read. None when it
        cannot take part yet."""
        raw = await self._input_history(eid, start, now)
        labels, kind = await self.hass.async_add_executor_job(label_history, raw)
        if not usable(labels):
            return None, (f"{kind} (too little history)" if kind in ("categorical", "banded") else kind)
        st = self.hass.states.get(eid)
        current = labels.get(max(labels)) if labels else None
        if st is not None and st.state not in ("unknown", "unavailable", ""):
            # the label the CURRENT state falls under, for the hold window
            current = (await self.hass.async_add_executor_job(
                label_history, {max(labels) if labels else 0.0: st.state}
            ))[0].get(max(labels) if labels else 0.0, current) if kind == "categorical" else current
        future = await self.hass.async_add_executor_job(project, labels, horizon_keys, now.tzinfo, current)
        merged = dict(labels)
        merged.update(future)
        return CalendarSignals.from_labels(eid, merged), kind

    async def _input_history(self, entity_id: str, start: datetime, end: datetime) -> Dict[float, object]:
        """hour key -> the entity's value in that hour. Hourly statistics when
        it has them (years of history), otherwise the recorder's raw states,
        taking the state that was in force at the top of each hour - which is
        what a schedule-like input needs and what the recorder can give for a
        sensor with no statistics."""
        out: Dict[float, object] = await self._hourly_means(entity_id, start)
        if out:
            return out
        states = await get_instance(self.hass).async_add_executor_job(
            history.state_changes_during_period, self.hass, start, end, entity_id, True, False, None, True,
        )
        changes = [(st.last_updated.timestamp(), st.state) for st in states.get(entity_id, [])
                   if st.state not in ("unknown", "unavailable", "")]
        changes.sort()
        if not changes:
            return {}
        i = 0
        current = changes[0][1]
        for b in hour_buckets(floor_hour(start), int((end - floor_hour(start)).total_seconds() // 3600) + 1):
            k = b.timestamp()
            while i < len(changes) and changes[i][0] <= k:
                current = changes[i][1]
                i += 1
            if k >= changes[0][0]:
                out[k] = current
        return out

    def _thermal_note(self, entry, text: str) -> None:
        """Say why a Load Juggler load has no thermal room - once per change of reason."""
        if self._thermal_said.get(entry.entry_id) != text:
            self._thermal_said[entry.entry_id] = text
            _LOGGER.info("Thermal room for %s: %s", entry.title, text)

    async def _thermal_rooms(self, site: SiteModel, series: Dict[str, list], now: datetime,
                             start: datetime, opts) -> Dict[str, dict]:
        """The room of each dashboard device that a Load Juggler tank or heater thermostat
        runs, from kWh per degree (insights/thermal.py) and the thermostat's temperature
        now against its boost setpoint. A device gets an entry only when the figure is
        known; the others keep the history ceiling. The device is matched through the
        registry: the tank's power sensor (or its thermostat) and the dashboard's energy sensor sit on one
        device. ponytail: the temperature comes from the recorder's raw states, which keep
        about ten days, so a heater that runs rarely has few runs; persist the runs if that
        proves too few."""
        registry = er.async_get(self.hass)
        by_device = {}
        for d in site.devices:
            ent = registry.async_get(d.energy)
            if ent is not None and ent.device_id:
                by_device.setdefault(ent.device_id, d.energy)
        rooms = set(opts.get(CONF_ROOM_THERMOSTATS) or ())
        out: Dict[str, dict] = {}
        for entry in self.hass.config_entries.async_entries(JUGGLER_DOMAIN):
            def conf(key, entry=entry):
                return entry.options.get(key, entry.data.get(key))
            if conf(JUGGLER_DEVICE_TYPE) != JUGGLER_TANK:
                continue
            climate = conf(JUGGLER_CLIMATE)
            # the power sensor when the load has one, else the thermostat itself: a generic
            # thermostat sits on its heater's device
            sid = None
            for ref in (conf(JUGGLER_TANK_POWER), climate):
                ent = registry.async_get(ref) if ref else None
                sid = by_device.get(ent.device_id) if ent is not None else None
                if sid:
                    break
            state = self.hass.states.get(climate) if climate else None
            if sid is None or state is None:
                self._thermal_note(entry, "no dashboard device on the thermostat's or power sensor's device" if sid is None
                                   else "its thermostat has no state")
                continue
            try:
                current = float(state.attributes["current_temperature"])
            except (KeyError, TypeError, ValueError):
                self._thermal_note(entry, "its thermostat reports no current temperature")
                continue
            boost = None
            for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
                if ent.domain == "number" and (ent.unique_id or "").endswith(JUGGLER_BOOST_SUFFIX):
                    boost = _read_number(self.hass, [ent.entity_id])
            boost = boost if boost is not None else conf(JUGGLER_BOOST_OPTION)
            try:
                boost = float(boost)
            except (TypeError, ValueError):
                self._thermal_note(entry, "no boost setpoint")
                continue
            temps = await self._hourly_attribute(climate, "current_temperature", start, now)
            energy = [(t.timestamp(), v) for t, v in series.get(sid, ()) if v is not None]
            kind = KINDS["room" if sid in rooms else "tank"]
            runs = heating_runs(energy, temps)
            learned = kwh_per_degree(runs, kind)
            if not learned:
                self._thermal_note(entry, f"{len(runs)} clean heating runs in {len(temps)} hours of temperature, "
                                          f"{len(energy)} of energy; not enough or too uneven ({kind.name})")
            room = room_from_thermal(learned[0], current, boost) if learned else None
            if room is not None:
                self._thermal_note(entry, f"{learned[0]:.3f} kWh/K from {learned[2]} runs ({kind.name})")
                out[sid] = {"room": room, "kwh_per_degree": round(learned[0], 3),
                            "spread": round(learned[1], 2), "runs": learned[2], "kind": kind.name,
                            "temperature": current, "boost": boost}
        return out

    async def _hourly_attribute(self, entity_id: str, attribute: str, start: datetime,
                                end: datetime) -> Dict[float, float]:
        """hour key -> a numeric attribute of the entity as it stood at the top of that hour,
        from the recorder's raw states."""
        states = await get_instance(self.hass).async_add_executor_job(
            history.state_changes_during_period, self.hass, start, end, entity_id, False, False, None, True,
        )
        changes = []
        for st in states.get(entity_id, []):
            try:
                changes.append((st.last_updated.timestamp(), float(st.attributes[attribute])))
            except (KeyError, TypeError, ValueError):
                continue
        changes.sort()
        out: Dict[float, float] = {}
        i, current = 0, None
        for b in hour_buckets(floor_hour(start), int((end - floor_hour(start)).total_seconds() // 3600) + 1):
            k = b.timestamp()
            while i < len(changes) and changes[i][0] <= k:
                current = changes[i][1]
                i += 1
            if current is not None:
                out[k] = current
        return out

    async def _hourly_means(self, entity_id: str, start: datetime,
                            units: Optional[Dict[str, str]] = None) -> Dict[float, float]:
        """hour key -> the entity's hourly mean statistic since ``start``,
        in ``units`` where given."""
        rows = await get_instance(self.hass).async_add_executor_job(
            rec_stats.statistics_during_period,
            self.hass, start, None, {entity_id}, "hour", units, {"mean"},
        )
        return {float(r["start"]): float(r["mean"]) for r in rows.get(entity_id, []) if r.get("mean") is not None}

    async def _calendar_events(self, entity_id: str, start: datetime, end: datetime) -> List[tuple]:
        """(start_key, end_key, title) for every event of a calendar between
        ``start`` and ``end`` - PAST included, which is what lets a calendar
        be fitted rather than declared. All-day events span local midnights."""
        try:
            resp = await self.hass.services.async_call(
                "calendar", "get_events",
                {"entity_id": entity_id, "start_date_time": start.isoformat(), "end_date_time": end.isoformat()},
                blocking=True, return_response=True,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Calendar %s could not be read: %s", entity_id, exc)
            return []
        out = []
        for ev in ((resp or {}).get(entity_id) or {}).get("events") or []:
            s_raw, e_raw = ev.get("start"), ev.get("end")
            if not s_raw or not e_raw:
                continue
            s_dt = dt_util.parse_datetime(s_raw) if isinstance(s_raw, str) and "T" in s_raw else None
            e_dt = dt_util.parse_datetime(e_raw) if isinstance(e_raw, str) and "T" in e_raw else None
            if s_dt is None:      # all-day: a date
                d = dt_util.parse_date(s_raw) if isinstance(s_raw, str) else s_raw
                s_dt = dt_util.start_of_local_day(d) if d else None
            if e_dt is None:
                d = dt_util.parse_date(e_raw) if isinstance(e_raw, str) else e_raw
                e_dt = dt_util.start_of_local_day(d) if d else None
            if s_dt is None or e_dt is None:
                continue
            out.append((dt_util.as_utc(s_dt).timestamp(), dt_util.as_utc(e_dt).timestamp(), ev.get("summary") or ""))
        return out

    async def _forecast_temperatures(self, weather_entity: str, now: datetime) -> Dict[float, float]:
        """Hour key -> forecast temperature in C over the horizon, from the
        weather entity's hourly forecast when it has one, else its daily one,
        interpolated onto every hour of the horizon. A provider that answers
        neither, or is unavailable, contributes nothing - the profile stands."""
        points = []
        state = self.hass.states.get(weather_entity)
        unit = (state.attributes.get("temperature_unit") if state else None) or UnitOfTemperature.CELSIUS
        for kind in ("hourly", "daily"):
            try:
                resp = await self.hass.services.async_call(
                    "weather", "get_forecasts", {"type": kind, "entity_id": weather_entity},
                    blocking=True, return_response=True,
                )
            except Exception as exc:  # noqa: BLE001  unsupported type, entity gone
                _LOGGER.debug("%s forecast from %s unavailable: %s", kind, weather_entity, exc)
                continue
            items = ((resp or {}).get(weather_entity) or {}).get("forecast") or []
            for it in items:
                t = it.get("datetime")
                if isinstance(t, str):
                    t = dt_util.parse_datetime(t)
                if t is None:
                    continue
                if kind == "daily":
                    # a day's low around 05:00 and high around 15:00 local
                    lo, hi = it.get("templow"), it.get("temperature")
                    day = dt_util.as_local(t).replace(hour=0, minute=0, second=0, microsecond=0)
                    if lo is not None:
                        points.append((day.replace(hour=5).timestamp(), _to_c(lo, unit)))
                    if hi is not None:
                        points.append((day.replace(hour=15).timestamp(), _to_c(hi, unit)))
                elif it.get("temperature") is not None:
                    points.append((t.timestamp(), _to_c(it["temperature"], unit)))
            if points:
                break
        if not points:
            return {}
        keys = [b.timestamp() for b in hour_buckets(floor_hour(now), HORIZON_HOURS)]
        return interpolate_hourly(points, keys)


def _read_number(hass, entity_ids) -> Optional[float]:
    """The first readable numeric state among ``entity_ids``."""
    for eid in entity_ids or ():
        st = hass.states.get(eid)
        if st is None:
            continue
        try:
            return float(st.state)
        except (TypeError, ValueError):
            continue
    return None


def _to_c(value, unit) -> float:
    return float(TemperatureConverter.convert(float(value), unit, UnitOfTemperature.CELSIUS))


# A device state entity in these domains is a NUMBER and nudges its next hours;
# anything else is a state, fitted like an attached input.
NUMERIC_STATE_DOMAINS = ("sensor", "input_number", "number")


def _rows_to_samples(rows: List[dict], tz) -> List[tuple]:
    """Recorder rows -> (local period start, kWh). ``start`` is an epoch float
    (StatisticsRow, every core this runs on)."""
    return [(datetime.fromtimestamp(r["start"], tz), float(r["change"])) for r in rows if r.get("change") is not None]
