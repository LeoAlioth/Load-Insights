"""What Load Insights sees right now, in words, for the options flow.

A read-only page, built fresh each time it is shown. Everything here is
already in the entities and the diagnostics dump; the point is that nobody
should have to open either to answer "is it working, and how far has it
got" (Anze, 2026-09-17 - Load Juggler has had this page for a while).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .insights.detect import most_specific
from .insights.scoring import LEADS_H


def _when(value: Optional[datetime]) -> str:
    if value is None:
        return "never"
    delta = (dt_util.utcnow() - dt_util.as_utc(value)).total_seconds()
    if delta < 90:
        return "just now"
    if delta < 5400:
        return f"{delta / 60:.0f} min ago"
    if delta < 172800:
        return f"{delta / 3600:.0f} h ago"
    return f"{delta / 86400:.0f} days ago"


def _w(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.0f} W" if abs(value) < 1000 else f"{value / 1000:.1f} kW"


def overview_text(hass: HomeAssistant, entry_id: str) -> str:
    """Markdown for the overview page."""
    coordinator = (hass.data.get(DOMAIN) or {}).get(entry_id)
    runner = (hass.data.get(DOMAIN) or {}).get(f"{entry_id}_detection")
    lines: list[str] = []

    data = getattr(coordinator, "data", None)
    if data is None:
        lines.append("The forecast has not computed yet.")
    else:
        site = data.site
        fc = data.consumption
        lines.append(f"**Forecast** - computed {_when(data.computed_at)}, "
                     f"from {fc.span_weeks:.0f} weeks of history.")
        lines.append(f"- Next hour {_w(fc.next_hour_w)}, today {fc.today_kwh:.1f} kWh, "
                     f"tomorrow {fc.tomorrow_kwh:.1f} kWh.")
        if data.grid is not None and data.grid.hours:
            g = data.grid
            lines.append(f"- At the meter: import {g.import_kwh:.1f} kWh and export "
                         f"{g.export_kwh:.1f} kWh over the week"
                         + (", battery simulated." if g.battery_modelled else ", no battery modelled."))
        lines.append(f"- {len(site.devices)} metered devices, "
                     f"{len(site.remainder_devices())} of them inside the remainder.")
        ledger = (data.ledgers or {}).get("consumption")
        hour = ledger.metrics(dt_util.now(), LEADS_H[0]) if ledger is not None else None
        if hour and hour.get("n"):
            lines.append(f"- An hour ahead it has been out by {_w(hour.get('mae_w'))} on average "
                         f"over {hour['n']} settled hours.")
        else:
            lines.append("- Scoring has nothing settled yet; the first figures appear after a day.")
        pair = [x for x in (data.weather_entity, data.temperature_entity) if x]
        lines.append(f"- Weather and temperature: {'both linked' if len(pair) == 2 else 'not linked'}"
                     f", {len(data.calendar_entities or ())} calendars, "
                     f"{len(data.input_entities or ())} other inputs.")

    lines.append("")
    if runner is None or not runner.enabled:
        lines.append("**Load detection** is not set up - give it the meter's per-phase power.")
        return "\n".join(lines)

    det = runner.detector
    sigs = det.signatures
    sessions = sum(s.count for s in sigs)
    named = [s for s in sigs if s.name]
    lines.append(f"**Load detection** - {'caught up' if runner.caught_up else 'still reading history'}, "
                 f"last run {_when(runner.last_run)}.")
    if not runner.caught_up:
        lines.append(f"- Read up to {_when(runner.last_processed)}; it keeps going on its own.")
    lines.append(f"- {len(sigs)} signatures from {sessions} sessions, {len(named)} named.")
    worth = [s for s in sigs if s.count >= 2 and most_specific(s.locations, s.count, runner.parents) == "main"]
    lines.append(f"- {len(worth)} worth naming; {len(sigs) - len(worth)} are either one-offs or "
                 f"already accounted for by a device's own meter.")
    running = det.active(dt_util.utcnow().timestamp())
    if running:
        bits = ", ".join(f"{_w(a['watts'])} on {a['phases'].upper()}" for a in running[:4])
        lines.append(f"- On right now: {bits}.")
    else:
        lines.append("- Nothing unexplained is running right now.")
    idle = [st.baseline for st in det.phases.values() if st.baseline is not None]
    if idle:
        lines.append(f"- Base load {_w(sum(idle))} across {len(idle)} phases.")
    if runner.layout:
        modes = set(runner.layout.values())
        lines.append("- The grid reading is " + ("added to the load one (grid-tied)."
                     if modes == {"parallel"} else "kept out of the load one (it already comes through the inverter)."))
    if runner.solar:
        seen = runner.pv_visible
        verdict = ("it shows in the meter" if any(seen.values())
                   else "it does not show in the meter" if seen else "not measured yet")
        lines.append(f"- {len(runner.solar)} solar array(s) watched for clouds: {verdict}.")
    if runner.submeters:
        lines.append(f"- {len(runner.submeters)} downstream meters used to place loads.")
    return "\n".join(lines)
