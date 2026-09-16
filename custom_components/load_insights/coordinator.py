"""Fetches the site's hourly statistics on the quarter hours and forecasts."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as rec_stats
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_CALENDAR_ENTITIES,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    HISTORY_WEEKS,
    HORIZON_HOURS,
    REFRESH_MINUTES,
    REFRESH_SECOND,
)
from .insights.calendars import CalendarSignals
from .insights.covariates import interpolate_hourly
from .insights.model import SiteModel
from .insights.profile import Forecast, floor_hour, forecast, hour_buckets
from .insights.scoring import BAND_LEAD_H, LEADS_H, Ledger
from .insights.series import combine, coverage, subtract_all

STORAGE_VERSION = 1
SITE_KEY = "consumption"
REMAINDER_KEY = "remainder"

_LOGGER = logging.getLogger(__name__)


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
    holidays_in_horizon: tuple = ()
    weather_entity: Optional[str] = None
    temperature_entity: Optional[str] = None
    temperature_history_hours: int = 0
    temperature_forecast_hours: int = 0
    calendar_entities: tuple = ()
    calendar_on_hours: Dict[str, int] = None  # type: ignore[assignment]
    # series key -> Ledger (site, remainder, each device by statistic id)
    ledgers: Dict[str, Ledger] = None  # type: ignore[assignment]
    # One forecast per individually metered device, keyed by its statistic id.
    # A device with no statistics yet has no entry, and its sensor stays
    # unavailable rather than showing a forecast of nothing.
    devices: Dict[str, Forecast] = None  # type: ignore[assignment]


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

    async def _load_ledgers(self) -> Dict[str, Ledger]:
        if self._ledgers is None:
            raw = await self._store.async_load() or {}
            self._ledgers = {k: Ledger.from_dict(v) for k, v in (raw.get("ledgers") or {}).items()}
        return self._ledgers

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
            trows = await get_instance(self.hass).async_add_executor_job(
                rec_stats.statistics_during_period,
                self.hass, start, None, {temp_entity}, "hour", {"temperature": UnitOfTemperature.CELSIUS}, {"mean"},
            )
            for r in trows.get(temp_entity, []):
                if r.get("mean") is not None and isinstance(r.get("start"), (int, float)):
                    temps_hist[float(r["start"])] = float(r["mean"])
            temps_fc = await self._forecast_temperatures(weather_entity, now)

        # --- calendars: every linked one, over history and horizon ---
        cal_entities = tuple(opts.get(CONF_CALENDAR_ENTITIES) or ())
        hour_keys = [b.timestamp() for b in hour_buckets(floor_hour(start), int((end_h - floor_hour(start)).total_seconds() // 3600) + 1)]
        cal_signals: List[CalendarSignals] = []
        for cal in cal_entities:
            events = await self._calendar_events(cal, start, end_h)
            cal_signals.append(CalendarSignals.from_events(cal, events, hour_keys))

        consumption = combine(series, site.consumption_terms())
        if not consumption:
            raise UpdateFailed("no hourly consumption statistics yet")
        remainder_ids = [d.energy for d in site.remainder_devices()]
        remainder = subtract_all(consumption, series, remainder_ids) if remainder_ids else []
        complete_since, missing = coverage(series, remainder_ids) if remainder_ids else (None, [])
        labels = {d.energy: d.label for d in site.devices}

        # The fit is pure Python over a few thousand rows - still, never on
        # the event loop.
        fit = lambda rows: self.hass.async_add_executor_job(  # noqa: E731
            forecast, rows, now, HORIZON_HOURS, 3.0, hols, temps_hist or None, temps_fc or None, cal_signals or None
        )
        cons_fc = await fit(consumption)
        rem_fc = await fit(remainder) if remainder else None

        # --- scoring: settle what has arrived, record what is now predicted ---
        ledgers = await self._load_ledgers()

        def score(key: str, fc: Forecast, actual) -> None:
            led = ledgers.setdefault(key, Ledger())
            led.settle(now, actual)
            led.record(now, fc.hourly, fc.bands, fc.tomorrow_kwh)

        score(SITE_KEY, cons_fc, consumption)
        if rem_fc is not None:
            score(REMAINDER_KEY, rem_fc, remainder)
        # Every listed device, nested ones included - the series are already
        # in hand for the remainder, so this is only the fits.
        device_fc: Dict[str, Forecast] = {}
        for d in site.devices:
            rows = series.get(d.energy) or []
            if rows:
                device_fc[d.energy] = await fit(rows)
                score(d.energy, device_fc[d.energy], rows)
        await self._save_ledgers()
        return InsightsData(
            site=site, consumption=cons_fc, remainder=rem_fc, computed_at=now,
            remainder_complete_since=complete_since,
            devices_without_statistics=tuple(labels.get(m, m) for m in missing),
            devices=device_fc,
            holidays_known=hols is not None,
            holidays_in_horizon=tuple(sorted(d.isoformat() for d in (hols or ()) if now.date() <= d <= end_h.date())),
            weather_entity=weather_entity,
            temperature_entity=temp_entity,
            temperature_history_hours=len(temps_hist),
            temperature_forecast_hours=len(temps_fc),
            ledgers=dict(ledgers),
            calendar_entities=cal_entities,
            calendar_on_hours={sig.entity: len(sig.existence) for sig in cal_signals},
        )

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


def _to_c(value, unit) -> float:
    return float(TemperatureConverter.convert(float(value), unit, UnitOfTemperature.CELSIUS))


def _rows_to_samples(rows: List[dict], tz) -> List[tuple]:
    """Recorder rows -> (local period start, kWh). ``start`` is an epoch float
    in current cores; a datetime is accepted too."""
    out = []
    for r in rows:
        v = r.get("change")
        if v is None:
            continue
        s = r.get("start")
        if isinstance(s, (int, float)):
            t = datetime.fromtimestamp(s, tz)
        elif isinstance(s, datetime):
            t = s.astimezone(tz)
        else:
            continue
        out.append((t, float(v)))
    return out
