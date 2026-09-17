"""Diagnostics: every fit, every signal and the whole library in one file.

Home Assistant puts a **Download diagnostics** button on the config entry and
on each of its devices. Downloading gives the same dump either way.

What is in it, and why:

* ``config`` - the entry's data and options verbatim: which meter, which
  inputs, which calendars, which device state sensors. Reproducing a report
  otherwise means a screenshot of every options page.
* ``site`` - what the Energy dashboard told us: the sources, the devices and
  how they nest. Half of what this integration does follows from it.
* ``forecasts`` - for the site, the remainder and each device: how much
  history the fit saw, the level correction, the temperature response, every
  signal's fitted factors, the nowcast, and the first hours of the horizon.
  This is the part that answers "why is it predicting that".
* ``scoring`` - the settled errors per lead, so an accuracy complaint can be
  checked rather than discussed.
* ``detection`` - each meter's baselines and noise floors, the signature
  library with its locations, and the recent sessions.

Nothing is redacted: this integration stores entity ids, statistic ids and
setpoints, and no credentials or tokens. Entity ids do carry whatever names
the devices were given, which is worth knowing before posting a dump
somewhere public.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import InsightsCoordinator, InsightsData
from .detection import DetectionRunner
from .insights.detect import most_specific

HORIZON_SAMPLE = 24        # hours of the horizon worth dumping; the rest is more of the same
_MAX_DEPTH = 6


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Whatever the diagnostics store can serialise, and nothing that hangs."""
    if depth > _MAX_DEPTH:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value), depth + 1)
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1) for v in value]
    return repr(value)


def _forecast(fc, name: str) -> dict:
    return {
        "series": name,
        "hours_fitted": fc.sample_count,
        "weeks_of_data": round(fc.span_weeks, 2),
        "level_correction": round(fc.level, 4),
        "next_hour_w": None if fc.next_hour_w is None else round(fc.next_hour_w),
        "today_kwh": round(fc.today_kwh, 3),
        "tomorrow_kwh": round(fc.tomorrow_kwh, 3),
        "temperature": _jsonable(fc.temperature),
        "hours_with_forecast_temperature": fc.hours_with_forecast_temperature,
        "signals": [
            {
                "entity": m.entity,
                "engaged": m.engaged,
                "existence": _jsonable(m.existence),
                "titles": {t: _jsonable(f) for t, f in m.titles.items()},
            }
            for m in fc.calendars
        ],
        "nowcast": _jsonable(fc.nowcast),
        "nowcast_deltas": [round(x, 4) for x in fc.nowcast_deltas],
        "horizon": [
            {"period_start": t.isoformat(), "kwh": round(v, 4),
             "p10": round(b[0], 4), "p90": round(b[1], 4)}
            for (t, v), b in list(zip(fc.hourly, fc.bands))[:HORIZON_SAMPLE]
        ],
        "history": [{"period_start": t.isoformat(), "kwh": round(v, 4)} for t, v in fc.history],
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """The whole picture, for a bug report or a question about a number."""
    coordinator: InsightsCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    runner: DetectionRunner | None = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_detection")
    data: InsightsData | None = coordinator.data if coordinator else None

    out: dict[str, Any] = {
        "config": {"data": dict(entry.data), "options": _jsonable(dict(entry.options)),
                   "version": entry.version, "minor_version": getattr(entry, "minor_version", None)},
    }
    if data is None:
        out["state"] = "no data yet - the first refresh has not completed"
        return out

    site = data.site
    out["site"] = {
        "grid_import": list(site.grid_import), "grid_export": list(site.grid_export),
        "solar": list(site.solar), "solar_forecast_entries": list(site.solar_forecast_entries),
        "battery_in": list(site.battery_in), "battery_out": list(site.battery_out),
        "battery_soc": list(site.battery_soc), "battery_capacity_kwh": site.battery_capacity_kwh,
        "devices": [
            {"statistic_id": d.energy, "name": d.name, "power": d.power, "included_in": d.included_in}
            for d in site.devices
        ],
        "remainder_devices": [d.energy for d in site.remainder_devices()],
        "remainder_complete_since": _jsonable(data.remainder_complete_since),
        "devices_without_statistics": list(data.devices_without_statistics),
    }
    out["computed_at"] = data.computed_at.isoformat()
    out["holidays"] = {"modelled": data.holidays_known, "in_horizon": list(data.holidays_in_horizon)}
    out["inputs"] = {
        "weather_entity": data.weather_entity,
        "temperature_entity": data.temperature_entity,
        "temperature_history_hours": data.temperature_history_hours,
        "temperature_forecast_hours": data.temperature_forecast_hours,
        "calendars": list(data.calendar_entities),
        "attached_entities": list(data.input_entities),
        "how_each_was_read": _jsonable(data.input_kinds),
    }
    out["forecasts"] = [_forecast(data.consumption, "consumption")]
    if data.remainder is not None:
        out["forecasts"].append(_forecast(data.remainder, "remainder"))
    out["forecasts"] += [_forecast(fc, sid) for sid, fc in (data.devices or {}).items()]
    out["grid"] = _jsonable(data.grid) if data.grid else None
    out["scoring"] = {
        key: {"errors": {str(lead): len(rows) for lead, rows in led.errors.items()},
              "pending": {str(lead): len(p) for lead, p in led.pending.items()},
              "metrics": {str(lead): led.metrics(data.computed_at, lead) for lead in led.errors},
              "band_coverage": led.coverage(data.computed_at),
              "last_day": led.last_day()}
        for key, led in (data.ledgers or {}).items()
    }

    if runner is not None:
        det = runner.detector
        parents = runner.parents
        out["detection"] = {
            "enabled": runner.enabled,
            "config": _jsonable(runner.config),
            "meters": _jsonable(runner.submeters),
            "solar": _jsonable(runner.solar),     # what a cloud is checked against
            "processed_until": _jsonable(runner.last_processed),
            "caught_up": runner.caught_up,
            "phases": {p: {"baseline": st.baseline, "noise": st.noise, "level": st.level,
                           # the loads believed to be running, and what each
                           # is still drawing: an edge that never pairs off
                           # is the thing to look at when a load goes missing
                           "running": [{"since": _jsonable(o.since), "watts": round(o.watts),
                                        "levels": len(o.levels)} for o in st.open_edges]}
                       for p, st in det.phases.items()},
            "signatures": [
                {**_jsonable(sig.to_dict()), "location": most_specific(sig.locations, sig.count, parents)}
                for sig in sorted(det.signatures, key=lambda x: -x.count)
            ],
            "recent_sessions": det.recent[-60:],
            "submeter_signatures": {
                name: [_jsonable(s.to_dict()) for s in d.signatures]
                for name, d in runner.fleet.subs.items()
            },
        }
    return out
