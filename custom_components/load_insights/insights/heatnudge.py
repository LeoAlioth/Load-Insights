"""Heat nudge, SHADOW MODE. Pure, stdlib only, replayable (no Home Assistant).

A heat pump (or an air conditioner or heat pump used as heating) makes a unit of
heat for less electricity when the outdoor air is warmer and the flow / tank /
room target is lower. The idea: use the forecast to put the device's heat in the
best hours. Nothing here writes to a device: it only COMPUTES, so the owner can
judge from real data whether a nudge would pay. The objective is electrical
ENERGY (kWh) only - never a tariff block (the owner, 2026-10-19: the 15-minute
blocks are charged on peak POWER, so a nudge also must never raise a block's
average; the scorecard reports the extra peak a nudge would have caused).

Three parts:

  1. ``cop`` - a COP PROXY from outdoor and target temperature. A Carnot
     estimate with a stated efficiency factor. It is NOT a measured COP.
  2. ``forecast_day`` - for a day: the COP of every hour, the device's expected
     energy by hour (its own history by hour of day and by temperature), the
     SHADOW schedule (the same heat made in better hours, within the device's
     shift limit and its own rate), the best hours and the expected gain.
  3. ``scorecard_day`` - a finished day: the outdoor temperature weighted by when
     the electricity was really used, against the same figure the shadow
     schedule would have produced from the same energy, and the gap. This needs
     no heat-output meter and is the number the owner judges by.

Hourly electrical energy comes from three kinds of reading (``classify_energy``,
``hourly_from_power``, ``daily_from_lumps`` + ``spread_daily``): an hourly
statistic, a power sensor's hourly mean, and a counter that moves once a day
(MELCloud at Andrej's: yesterday's total, replaced after midnight). The last
cannot give hours: its day is spread over hours by an activity entity if there is
one, else by a heating-demand shape, and the scorecard says so (``basis``).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

HOUR = 3600.0
HEAT_GENERATION = 1          # what the stored scorecard means; migrate() below

# --- the COP proxy -----------------------------------------------------------
# COP = ETA x T_hot / (T_hot - T_cold), temperatures in kelvin, where
# T_hot = target + the kind's SINK_OFFSET and T_cold = outdoor - SOURCE_OFFSET.
# ETA 0.45 is a JUDGEMENT (a typical air-source unit reaches 40-50 % of Carnot),
# not a measurement; at a 35 C target it gives about 2.2 % per degree of outdoor
# temperature, in the 2-3 % the owner quoted. It scales every COP alike, so it
# changes no ranking and no gap in degrees - only the size of the percentages.
ETA = 0.45
SOURCE_OFFSET = 5.0          # evaporator below the outdoor air (K)
COP_MIN, COP_MAX = 1.0, 8.0
FROST_BAND_C = (0.0, 5.0)    # humidity matters only here (frost, defrost) ...
FROST_RH = 80.0              # ... and only when this damp: a flag, never in the COP

# per kind: (default target C, SINK_OFFSET K, shift limit h)
#   shift limit = how far an hour of heat may move: a tank stores a lot, a floor or
#   a house some, a room almost none. Judgements (the owner decides, plan section 9).
KINDS = {
    "space_heating": (35.0, 5.0, 4),
    "hot_water": (50.0, 5.0, 8),
    "room": (21.0, 12.0, 2),      # air to air: the indoor coil runs ~12 K above the room air
}
DEFAULT_KIND = "space_heating"

DAILY_LAG_H = 6              # a counter that moves once a day reports the day before when it moves before 06:00
LUMP_SHARE = 0.9             # a day whose change sits this much in one hour ...
LUMP_DAYS = 0.8              # ... on this share of its days (at least LUMP_MIN_DAYS) is a daily counter
LUMP_MIN_DAYS = 3
MIN_DAY_KWH = 0.05           # a day with less is not a heating day: not scored
HEATING_BASE_C = 15.0        # the demand shape for spreading a daily total: max(0, base - T)
FIT_MIN_DAYS = 7
FIT_MIN_SPREAD_C = 2.0
FIT_MIN_R2 = 0.2
SHAPE_WIDTH_C = 4.0          # days within this many degrees of the forecast day's mean shape its hours
KEEP_DAYS = 400              # the stored scorecard's length
KEEP_FORECASTS = 14


def kind_defaults(kind: Optional[str]) -> Tuple[float, float, int]:
    return KINDS.get(kind or DEFAULT_KIND, KINDS[DEFAULT_KIND])


def cop(t_out: float, target: float, kind: Optional[str] = None) -> float:
    """The COP proxy: Carnot x ETA between the target (plus the kind's offset) and
    the outdoor air (less SOURCE_OFFSET), kept to COP_MIN..COP_MAX. Rises with the
    outdoor temperature and falls with the target."""
    hot = target + kind_defaults(kind)[1] + 273.15
    cold = t_out - SOURCE_OFFSET + 273.15
    lift = max(hot - cold, 1.0)
    return min(COP_MAX, max(COP_MIN, ETA * hot / lift))


def frost_flag(t_out: float, humidity: Optional[float]) -> bool:
    """The cold damp band where frost and defrost cost more than the proxy says."""
    return humidity is not None and FROST_BAND_C[0] <= t_out <= FROST_BAND_C[1] and humidity >= FROST_RH


# --- hourly electrical energy from what a device reports ---------------------
def local_day(ts: float, tz) -> date:
    return datetime.fromtimestamp(ts, tz).date()


def day_keys(day: date, tz) -> List[float]:
    """The hour starts (epoch s) of a local day: 24, or 23 / 25 over a clock change."""
    k = datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()
    out = []
    while local_day(k, tz) == day:
        out.append(k)
        k += HOUR
    return out


def classify_energy(rows: Sequence[Tuple[float, float]], tz) -> str:
    """"hourly" or "daily" for a cumulative reading's hourly CHANGES
    ((hour start epoch s, kWh)). Daily: on most days nearly all of the day's
    change lands in one hour (Andrej's MELCloud: the day before's total, replaced
    once after midnight, so its utility meter moves once a day)."""
    days: Dict[date, List[float]] = {}
    for k, v in rows:
        if v and v > 0:
            days.setdefault(local_day(k, tz), []).append(v)
    if len(days) < LUMP_MIN_DAYS:
        return "hourly"
    lumped = sum(1 for v in days.values() if max(v) >= LUMP_SHARE * sum(v))
    return "daily" if lumped >= LUMP_DAYS * len(days) else "hourly"


def daily_from_lumps(rows: Sequence[Tuple[float, float]], tz) -> Dict[date, float]:
    """kWh per local day from a daily counter's changes. A change before DAILY_LAG_H
    is the day BEFORE's total (the reading is yesterday's, published after midnight);
    one later is the same day's. LIMITS: a day equal to the day before leaves no change
    and is missed; the day's own hours are unknown."""
    out: Dict[date, float] = {}
    for k, v in rows:
        if v and v > 0:
            d = local_day(k - DAILY_LAG_H * HOUR, tz)
            out[d] = out.get(d, 0.0) + v
    return out


def hourly_from_power(rows: Sequence[Tuple[float, float]]) -> Dict[float, float]:
    """kWh per hour from a power sensor's hourly MEAN in W (an hour's kWh is its mean kW)."""
    return {k: w / 1000.0 for k, w in rows if w is not None and w > 0}


def demand_weights(temps: Sequence[Optional[float]]) -> List[float]:
    """Heating demand per hour, max(0, HEATING_BASE_C - T): the shape a device that
    only reports a day is spread by when nothing says when it ran. Flat when no hour needs heat."""
    w = [max(0.0, HEATING_BASE_C - t) if t is not None else 0.0 for t in temps]
    return w if sum(w) > 0 else [1.0] * len(temps)


def active_fractions(changes: Sequence[Tuple[float, bool]], keys: Sequence[float], end: float) -> List[float]:
    """Share of each hour (starting at ``keys``) an activity entity was ON, from its
    changes ((epoch s, on)) sorted by time; the state in force before the first change
    is off. ``end`` closes the last interval (now)."""
    out = []
    for k in keys:
        lo, hi = k, min(k + HOUR, end)
        if hi <= lo:
            out.append(0.0)
            continue
        on_s, state, t = 0.0, False, lo
        for ts, on in changes:
            if ts <= lo:
                state = on
                continue
            if ts >= hi:
                break
            if state:
                on_s += ts - t
            t, state = ts, on
        if state:
            on_s += hi - t
        out.append(on_s / HOUR)
    return out


def spread_daily(day_kwh: float, weights: Sequence[float]) -> List[float]:
    """A day's energy over its hours in proportion to ``weights`` (flat if they sum to 0)."""
    s = sum(weights)
    if s <= 0:
        return [day_kwh / len(weights)] * len(weights) if weights else []
    return [day_kwh * w / s for w in weights]


# --- the shadow schedule -----------------------------------------------------
def shadow_schedule(energy: Sequence[float], cops: Sequence[float], shift: int,
                    rate: Optional[float] = None) -> List[float]:
    """The same HEAT made in better hours: electrical kWh per hour.

    Heat is conserved: an hour's heat is ``energy x cop``; moving it to an hour with
    a better COP costs less electricity. An hour's heat may move at most ``shift``
    hours either way and an hour takes at most max(rate, its own energy). Greedy -
    the worst hours first to the best hour in reach - not optimal (ponytail: a
    transport problem solved exactly if the greedy ever looks short)."""
    n = len(energy)
    cap = [max(rate if rate is not None else max(energy, default=0.0), e) for e in energy]
    used = list(energy)                       # everything starts where it was; worst hours move first
    for i in sorted(range(n), key=lambda j: cops[j]):
        heat = energy[i] * cops[i]
        if heat <= 0:
            continue
        used[i] -= energy[i]
        reach = sorted((j for j in range(max(0, i - shift), min(n, i + shift + 1)) if cops[j] > cops[i] + 1e-9),
                       key=lambda j: -cops[j])       # only to a strictly better hour
        for j in reach:
            take = min(heat, max(cap[j] - used[j], 0.0) * cops[j])
            used[j] += take / cops[j]
            heat -= take
            if heat <= 1e-12:
                break
        used[i] += heat / cops[i]             # what found no room stays
    return used


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> Optional[float]:
    s = sum(weights)
    return None if s <= 0 else sum(v * w for v, w in zip(values, weights)) / s


# --- what a day needs, from the device's own history -------------------------
def day_need(history: Sequence[Tuple[float, float]], t_mean: float) -> Tuple[Optional[float], str]:
    """Expected kWh for a day with mean outdoor ``t_mean`` from past days
    ((kWh, mean outdoor C)): a straight line when at least FIT_MIN_DAYS days spread
    FIT_MIN_SPREAD_C and the line explains FIT_MIN_R2 of them, else the median.
    -> (kWh or None, "fit" | "median" | "none")."""
    days = [(e, t) for e, t in history if e is not None and t is not None]
    if not days:
        return None, "none"
    ts = [t for _, t in days]
    n = len(days)
    if n >= FIT_MIN_DAYS and max(ts) - min(ts) >= FIT_MIN_SPREAD_C:
        mt, me = sum(ts) / n, sum(e for e, _ in days) / n
        sxx = sum((t - mt) ** 2 for t in ts)
        sxy = sum((t - mt) * (e - me) for e, t in days)
        syy = sum((e - me) ** 2 for e, _ in days)
        if sxx > 0 and syy > 0 and (sxy * sxy) / (sxx * syy) >= FIT_MIN_R2:
            b = sxy / sxx
            return max(0.0, me + b * (t_mean - mt)), "fit"
    es = sorted(e for e, _ in days)
    mid = len(es) // 2
    return (es[mid] if len(es) % 2 else (es[mid - 1] + es[mid]) / 2), "median"


def hour_shape(days: Sequence[Tuple[Sequence[float], float]], t_mean: float, tz_hours: Sequence[int]) -> Tuple[List[float], str]:
    """The share of a day's energy in each clock hour (``tz_hours``: the forecast
    day's local hours), from past days ((kWh by clock hour 0-23, mean C)), the days
    nearest the forecast day's temperature counting most. Flat with no history."""
    acc = [0.0] * 24
    for hours, tm in days:
        total = sum(hours)
        if total <= 0:
            continue
        w = math.exp(-0.5 * ((tm - t_mean) / SHAPE_WIDTH_C) ** 2) + 1e-3
        for h in range(24):
            acc[h] += w * hours[h] / total
    if sum(acc) <= 0:
        return [1.0 / len(tz_hours)] * len(tz_hours), "flat"
    shape = [acc[h] for h in tz_hours]
    s = sum(shape)
    return [x / s for x in shape], "history"


# --- the forecast side -------------------------------------------------------
def _fill(temps: Sequence[Optional[float]]) -> Optional[List[float]]:
    """Missing hours take the nearest known one; None if none is known."""
    known = [i for i, t in enumerate(temps) if t is not None]
    if not known:
        return None
    return [temps[min(known, key=lambda j: abs(j - i))] for i in range(len(temps))]


def forecast_day(keys: Sequence[float], temps: Sequence[Optional[float]], target: float, kind: Optional[str],
                 need_kwh: Optional[float], shape: Sequence[float], shift: int, rate: Optional[float],
                 humidity: Optional[Sequence[Optional[float]]] = None) -> Optional[dict]:
    """One day: the COP of each hour, the expected energy by hour (``need_kwh`` x
    ``shape``), the shadow schedule from it, the best hours and the gain. None when
    no hour has a temperature. ``nudge`` lists the hours that take MORE than expected."""
    t = _fill(temps)
    if t is None or not keys:
        return None
    cops = [cop(x, target, kind) for x in t]
    rank = {i: r + 1 for r, i in enumerate(sorted(range(len(cops)), key=lambda j: -cops[j]))}
    mean_cop = sum(cops) / len(cops)
    rows = [{"start": k, "temp": round(x, 1), "cop": round(c, 2), "index": round(c / mean_cop, 3), "rank": rank[i],
             "frost": bool(humidity and frost_flag(x, humidity[i]))}
            for i, (k, x, c) in enumerate(zip(keys, t, cops))]
    out = {"hours": rows, "mean_cop": round(mean_cop, 2), "temps_filled": sum(1 for x in temps if x is None)}
    if not need_kwh or need_kwh <= 0:
        return {**out, "need_kwh": need_kwh, "nudge": [], "gain_vs_day_average_pct": None, "saving_pct": None, "extra_peak_kw": None}
    expected = [need_kwh * s for s in shape]
    shadow = shadow_schedule(expected, cops, shift, rate)
    for r, e, s in zip(rows, expected, shadow):
        r["expected_kwh"], r["shadow_kwh"] = round(e, 3), round(s, 3)
    mean_shadow = weighted_mean(cops, shadow)
    saving = 1.0 - sum(shadow) / sum(expected)
    return {**out, "need_kwh": round(need_kwh, 2),
            "nudge": [r["start"] for r in rows if r["shadow_kwh"] > r["expected_kwh"] + 0.005],
            # the shadow's energy-weighted COP against the plain mean of the day's hours
            "gain_vs_day_average_pct": round((mean_shadow / mean_cop - 1.0) * 100.0, 1),
            "saving_pct": round(saving * 100.0, 1),
            "extra_peak_kw": round(max(shadow) - max(expected), 3)}


# --- the scorecard -----------------------------------------------------------
def scorecard_day(energy: Sequence[float], t_actual: Sequence[Optional[float]], target: float, kind: Optional[str],
                  shift: int, rate: Optional[float], basis: str,
                  t_forecast: Optional[Sequence[Optional[float]]] = None) -> Optional[dict]:
    """A finished day. ``actual_t``: the outdoor temperature weighted by the hours'
    electrical energy. ``shadow_t``: the same figure for the shadow schedule built
    from the same energy and the day's REAL temperatures (a perfect forecast);
    ``shadow_t_forecast``: the schedule picked from yesterday evening's FORECAST
    temperatures, judged at the real ones - what a nudge could really have done.
    ``gap`` = shadow - actual in degrees, positive when the nudge would have used warmer
    hours. ``saving_pct``: electricity the shadow would have saved for the same heat
    (proxy). ``extra_peak_kw``: the shadow's highest hour less the actual one, the extra
    peak a nudge would have caused (hourly, so a floor of any 15-minute block's).
    None for a day without usable temperatures or energy."""
    total = sum(energy)
    t = _fill(t_actual)
    if total < MIN_DAY_KWH or t is None:
        return None
    cops = [cop(x, target, kind) for x in t]
    actual_t = weighted_mean(t, energy)
    shadow = shadow_schedule(energy, cops, shift, rate)
    shadow_t = weighted_mean(t, shadow)
    out = {"energy_kwh": round(total, 3), "actual_t": round(actual_t, 2), "shadow_t": round(shadow_t, 2),
           "gap": round(shadow_t - actual_t, 2), "saving_pct": round((1.0 - sum(shadow) / total) * 100.0, 1),
           "extra_peak_kw": round(max(shadow) - max(energy), 3), "basis": basis,
           "shadow_t_forecast": None, "gap_forecast": None}
    f = _fill(t_forecast) if t_forecast is not None else None
    if f is not None and len(f) == len(energy):
        sf = shadow_schedule(energy, [cop(x, target, kind) for x in f], shift, rate)
        st = weighted_mean(t, sf)
        out["shadow_t_forecast"], out["gap_forecast"] = round(st, 2), round(st - actual_t, 2)
    return out


def summarize(days: Dict[str, dict]) -> dict:
    """The scorecard over its days (``{iso date: scorecard_day}``): energy-weighted means of
    the gaps. Days on a modelled basis are counted apart and left out of the headline."""
    measured = [d for d in days.values() if d.get("basis") != "modelled"]
    w = [d["energy_kwh"] for d in measured]
    wf = [d["energy_kwh"] for d in measured if d.get("gap_forecast") is not None]
    mean = lambda k, ws, ds: None if not ds else round(weighted_mean([d[k] for d in ds], ws), 2)  # noqa: E731
    return {"days_measured": len(measured), "days_modelled": len(days) - len(measured),
            "mean_gap": mean("gap", w, measured),
            "mean_gap_forecast": mean("gap_forecast", wf, [d for d in measured if d.get("gap_forecast") is not None]),
            "mean_saving_pct": mean("saving_pct", w, measured),
            "mean_extra_peak_kw": mean("extra_peak_kw", w, measured)}


def prune(days: Dict[str, dict], keep: int = KEEP_DAYS) -> Dict[str, dict]:
    return {k: days[k] for k in sorted(days)[-keep:]}


# --- the stored scorecard ----------------------------------------------------
def migrate(raw: Optional[dict]) -> dict:
    """The stored state as the code reads it: ``{"generation", "days": {device: {iso: card}},
    "forecasts": {device: {iso: [temp by clock hour 0-23]}}}``. Nothing stored, or a
    generation this code does not know, starts empty: the days of an hourly device are
    worked out again from the recorder, only the old FORECAST temperatures are lost.
    Bump HEAT_GENERATION and add a step here when a stored field changes meaning."""
    if not isinstance(raw, dict) or raw.get("generation") != HEAT_GENERATION:
        return {"generation": HEAT_GENERATION, "days": {}, "forecasts": {}}
    return {"generation": HEAT_GENERATION, "days": dict(raw.get("days") or {}), "forecasts": dict(raw.get("forecasts") or {})}


# --- the options -------------------------------------------------------------
def parse_devices(raw) -> List[dict]:
    """The 'Heat devices' option as devices: each with ``id`` (stable, from the name),
    ``name``, ``source`` (a power or energy sensor), ``kind``, ``target`` (number, C),
    ``target_entity`` (None or an entity that holds it), ``activity_entity``. A row
    without a name or source is dropped; an unknown kind is the default; a missing
    target is the kind's default."""
    out, seen = [], set()
    for r in raw or []:
        name, source = str(r.get("name") or "").strip(), r.get("source")
        if not name or not source:
            continue
        base = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_") or "device"
        uid, n = base, 2
        while uid in seen:
            uid, n = f"{base}_{n}", n + 1
        seen.add(uid)
        kind = r.get("kind") if r.get("kind") in KINDS else DEFAULT_KIND
        try:
            target = float(r["target"])
        except (KeyError, TypeError, ValueError):
            target = kind_defaults(kind)[0]
        out.append({"id": uid, "name": name, "source": source, "kind": kind, "target": target,
                    "target_entity": r.get("target_entity") or None, "activity_entity": r.get("activity_entity") or None})
    return out


# --- the answer a device's entities read --------------------------------------
def publish(now: datetime, today: Optional[dict], tomorrow: Optional[dict], card: dict, reading: str,
            target: float, kind: str, why: Optional[str] = None) -> dict:
    """What one heat device's entities show. ``today`` / ``tomorrow``: ``forecast_day``
    results; ``card``: ``summarize`` plus ``last`` (the newest day's card); ``reading``:
    "hourly" | "power" | "daily" | "none". ``would_nudge_now``: the hour under way is one the
    shadow schedule gives more than expected - and never when today has no schedule.
    Shadow mode: nothing is sent anywhere."""
    k = now.replace(minute=0, second=0, microsecond=0).timestamp()
    row = next((r for r in (today or {}).get("hours", []) if r["start"] == k), None)
    return {"index_now": None if row is None else row["index"],
            "cop_now": None if row is None else row["cop"],
            "would_nudge_now": bool(today and k in today["nudge"]),
            "frost_now": bool(row and row["frost"]),
            "today": today, "tomorrow": tomorrow, "scorecard": card, "reading": reading,
            "target_c": target, "kind": kind, "why": why, "computed_at": now.isoformat(),
            "proxy": "Carnot x %.2f, not a measured COP" % ETA}
