"""Battery SOC planning from the house forecast and the sun forecast. Pure.

One hourly balance, three readings of it (ported from Kozolec's three
template helpers, 2026-10-17):

    net_i = w_p(i) x production_scale x production_i - w_c(i) x consumption_scale x use_i
    S_i   = net_0 + ... + net_i          (kWh; i = 0 is the hour under way)
    w(i)  = max(1 - drop x max(i - full_h, 0), 0)     per side, with its own full_h and drop

    target  = full - max(0, max S) / capacity x 100     room for the strongest stretch
    minimum = floor + max(0, -min S) / capacity x 100   cover for the weakest
    excess  = each calendar day's peak of S, less the energy the pack still takes to be full

The values are RAW: negative is allowed, the consumer clamps. A reading is
None when any hour it needs is missing, and the caller keeps its last value
(the helpers did the same after 2026-10-16: a half-rolled forecast must not
move the target).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

HOUR = 3600.0


@dataclass(frozen=True)
class Side:
    """How a side's hours lose weight with distance: full for ``full_h``
    hours, then ``drop`` percent less per hour."""
    full_h: float
    drop: float


@dataclass(frozen=True)
class Spec:
    """One sensor's options: the two sides' weights."""
    consumption: Side
    production: Side


def weight(i: int, side: Side) -> float:
    return max(1.0 - side.drop / 100.0 * max(i - side.full_h, 0.0), 0.0)


def hour_keys(now: datetime, hours: int) -> List[float]:
    """``hours`` consecutive real hours from the one under way (epoch seconds)."""
    first = now.replace(minute=0, second=0, microsecond=0).timestamp()
    return [first + i * HOUR for i in range(hours)]


def hours_until_day(now: datetime, days: int) -> int:
    """Hours from the one under way to local midnight ``days`` calendar days
    after today's start - the span the daily excess runs over."""
    first = now.replace(minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return int(round((end.timestamp() - first.timestamp()) / HOUR))


def spread_evenly(use: Dict[float, float], parts: Sequence[Dict[float, float]]) -> Dict[float, float]:
    """``use`` with each part's energy spread evenly over its own horizon
    instead of where it is forecast: a tank's boost moves to midday, a dull
    spell needs its energy round the clock. Never below zero."""
    out = dict(use)
    for hourly in parts:
        if not hourly:
            continue
        even = sum(hourly.values()) / len(hourly)
        for k, v in hourly.items():
            if k in out:
                out[k] = max(out[k] - v + even, 0.0)
    return out


def production_hour(production: Dict[float, float], k: float) -> Optional[float]:
    """The forecast sun in the hour starting ``k``. A forecast integration
    may leave out its zero (night) hours: up to the last hour it covers a
    missing hour is zero (the night before its first day too), beyond its end
    it is unknown."""
    if k in production:
        return production[k]
    if production and k <= max(production):
        return 0.0
    return None


@dataclass(frozen=True)
class Balance:
    keys: tuple              # hour starts, epoch seconds
    net: tuple               # weighted kWh per hour
    running: tuple           # S_i


def balance(now: datetime, hours: int, production: Dict[float, float], use: Dict[float, float],
            spec: Spec, production_scale: float = 1.0, consumption_scale: float = 1.0,
            under_way: bool = False) -> Optional[Balance]:
    """The weighted hourly balance over ``hours`` hours from the one under
    way; None while any of those hours has no forecast (production or use).
    ``under_way``: the hour in progress counts only for the part still ahead
    (the daily excess; its whole forecast put today's excess above what was
    left to produce, 14 Oct 13:55)."""
    keys = hour_keys(now, hours)
    net: List[float] = []
    run: List[float] = []
    total = 0.0
    for i, k in enumerate(keys):
        p = production_hour(production, k)
        u = use.get(k)
        if p is None or u is None:
            return None
        v = (weight(i, spec.production) * production_scale * p
             - weight(i, spec.consumption) * consumption_scale * u)
        if under_way and i == 0:
            v *= min(1.0, (k + HOUR - now.timestamp()) / HOUR)
        total += v
        net.append(v)
        run.append(total)
    return Balance(tuple(keys), tuple(net), tuple(run))


def _at(k: float, tz) -> str:
    return datetime.fromtimestamp(k, tz).isoformat()


def _days(bal: Balance, capacity: float, tz) -> List[dict]:
    """Net energy per calendar day of the window, in kWh and in points of the pack."""
    out: List[dict] = []
    for k, v in zip(bal.keys, bal.net):
        d = datetime.fromtimestamp(k, tz).date().isoformat()
        if not out or out[-1]["date"] != d:
            out.append({"date": d, "net_kwh": 0.0})
        out[-1]["net_kwh"] += v
    return [{"date": r["date"], "net_kwh": round(r["net_kwh"], 2),
             "net_points": round(r["net_kwh"] / capacity * 100.0, 1)} for r in out]


def _extremes(bal: Balance, tz) -> dict:
    hi = max(range(len(bal.running)), key=lambda i: (bal.running[i], -i))
    lo = min(range(len(bal.running)), key=lambda i: (bal.running[i], i))
    peak, dip = max(0.0, bal.running[hi]), max(0.0, -bal.running[lo])
    return {"peak_surplus_kwh": round(peak, 2), "peak_at": _at(bal.keys[hi], tz) if peak > 0 else None,
            "dip_kwh": round(dip, 2), "dip_at": _at(bal.keys[lo], tz) if dip > 0 else None}


def target(bal: Balance, full_soc: float, capacity: float, tz) -> Tuple[int, dict]:
    """Full less the highest point of S, as points of the pack."""
    value = round(full_soc - max(0.0, max(bal.running)) / capacity * 100.0)
    return value, {**_extremes(bal, tz), "full_soc": full_soc, "days": _days(bal, capacity, tz)}


def minimum(bal: Balance, floor: float, capacity: float, tz) -> Tuple[int, dict]:
    """The floor plus the deepest dip of S, as points of the pack."""
    value = round(floor + max(0.0, -min(bal.running)) / capacity * 100.0)
    return value, {**_extremes(bal, tz), "floor": floor, "days": _days(bal, capacity, tz)}


def full_at(bal: Balance, until_full_kwh: float, tz) -> Optional[str]:
    """When the pack is next full by the forecast: the START of the first hour
    whose running balance covers what the pack still takes (the early side - it
    is full somewhere inside that hour). An hour already past, or the hour under
    way, means full now. None when no hour of the window gets there."""
    for k, s in zip(bal.keys, bal.running):
        if s >= until_full_kwh:
            return _at(k, tz)
    return None


def excess(bal: Balance, days: int, until_full_kwh: float, tz) -> Tuple[Optional[float], dict]:
    """For each of ``days`` calendar days from today: the day's peak of S
    less the energy the pack still takes to be full, in kWh. Negative: the
    pack is not full by then. State: today's."""
    today = datetime.fromtimestamp(bal.keys[0], tz).date()
    peaks: Dict[str, float] = {}
    for k, s in zip(bal.keys, bal.running):
        d = datetime.fromtimestamp(k, tz).date().isoformat()
        peaks[d] = max(peaks[d], s) if d in peaks else s
    rows = []
    for n in range(days):
        d = (today + timedelta(days=n)).isoformat()
        rows.append({"date": d, "excess_kwh": round(peaks[d] - until_full_kwh, 1) if d in peaks else None})
    return rows[0]["excess_kwh"], {"days": rows, "excess_kwh": [r["excess_kwh"] for r in rows],
                                   "until_full_kwh": round(until_full_kwh, 2),
                                   "full_at": full_at(bal, until_full_kwh, tz)}


def sun_window(now: datetime, production: Dict[float, float], scale: float, tz) -> tuple:
    """When the forecast sun starts and stops on the first day that still has
    any from now on (today by day, tomorrow once today is over): the start of
    that whole day's first and the end of its last hour above 1 % of the day's
    peak hour, so a few watts at dawn or dusk do not count. -> (starts_at,
    ends_at) ISO strings, (None, None) with no forecast sun. A load paced off
    the day's excess is paced until the end."""
    first = now.replace(minute=0, second=0, microsecond=0).timestamp()
    live = sorted(k for k, v in production.items() if k >= first and v * scale > 0.001)
    if not live:
        return None, None
    day = datetime.fromtimestamp(live[0], tz).date()
    hours = sorted(k for k, v in production.items()
                   if datetime.fromtimestamp(k, tz).date() == day and v * scale > 0.001)
    bar = max(production[k] for k in hours) * 0.01
    lit = [k for k in hours if production[k] >= bar]
    return _at(lit[0], tz), _at(lit[-1] + HOUR, tz)


ROOM_PERCENTILE = 90
ROOM_MIN_DAYS = 3


def room_to_absorb(hourly: Dict[float, float], now: datetime, tz=None) -> Optional[float]:
    """kWh a named load can still take today: what it has used on its heavier
    days (the 90th percentile of the complete days before today in ``hourly``,
    hour start epoch s -> Wh) less what it has used today so far, never below 0.
    A ceiling from the past, not a measurement now - the owner's, 2026-10-18, for
    Load Juggler's early start: a tank that has already heated has little room
    left, whatever its element could draw. None with fewer than ROOM_MIN_DAYS
    complete days (a new load has no ceiling yet)."""
    tz = tz or now.tzinfo
    today = now.astimezone(tz).date()
    per_day: Dict = {}
    for k, wh in hourly.items():
        d = datetime.fromtimestamp(k, tz).date()
        per_day[d] = per_day.get(d, 0.0) + wh
    # a day counts once its first hour is inside the data: the oldest, part-covered day is left out
    first = min(per_day) if per_day else None
    past = sorted(v / 1000.0 for d, v in per_day.items() if d < today and d != first)
    if len(past) < ROOM_MIN_DAYS:
        return None
    pos = (len(past) - 1) * ROOM_PERCENTILE / 100.0
    lo = int(pos)
    p90 = past[lo] + (past[min(lo + 1, len(past) - 1)] - past[lo]) * (pos - lo)
    return round(max(0.0, p90 - per_day.get(today, 0.0) / 1000.0), 2)


RATE_PERCENTILE = 95        # measured on Kozolec: a boiler rarely runs a whole hour, so P90 of its active hours read 1.1 kW of a 1.9 kW element, P95 1.5, P98 1.85 - low reads only start the early start sooner
RATE_MIN_HOURS = 5          # active hours before a rate is believed
RATE_ACTIVE_SHARE = 0.1     # an hour is active at this share of the device's own heaviest hour


def absorption_rate(samples) -> Optional[float]:
    """kW a device really pulls when it runs: the 95th percentile of its active hours' energy
    (an hour's kWh is its mean kW). ``samples`` are the hourly (hour start, kWh) of the
    forecast's own series. Active is a share of the device's own heaviest hour, so standby
    and the part-hour ends of a run do not drag it down; a high percentile keeps the full
    hours of a thermostat that cycles. None with fewer than RATE_MIN_HOURS active hours.
    What Load Juggler's early start counts as the load's power instead of its rating
    (INS-3)."""
    values = [v for _, v in samples if v is not None and v > 0]
    if not values:
        return None
    floor = RATE_ACTIVE_SHARE * max(values)
    active = sorted(v for v in values if v >= floor)
    if len(active) < RATE_MIN_HOURS:
        return None
    pos = (len(active) - 1) * RATE_PERCENTILE / 100.0
    lo = int(pos)
    return round(active[lo] + (active[min(lo + 1, len(active) - 1)] - active[lo]) * (pos - lo), 3)


def rate_from_wh(hourly: Dict[float, float]) -> Optional[float]:
    """``absorption_rate`` for a named load, from the hourly energy the
    detector keeps for it (``Fleet.hourly_by_name``: hour start epoch s -> Wh,
    the series ``room_to_absorb`` reads): an hour's Wh over 1000 is its mean kW."""
    return absorption_rate([(k, wh / 1000.0) for k, wh in hourly.items()])


def daylight(production: Dict[float, float], day, tz) -> Optional[Tuple[float, float]]:
    """(first, last) lit hour start, epoch s, of local date ``day`` in the sun
    forecast: lit is above 1 % of that day's peak hour, as ``sun_window``
    reads it, so a few watts at dawn or dusk do not count. None with no sun
    forecast that day."""
    hours = {k: v for k, v in production.items() if v > 0.001 and datetime.fromtimestamp(k, tz).date() == day}
    if not hours:
        return None
    bar = max(hours.values()) * 0.01
    lit = sorted(k for k, v in hours.items() if v >= bar)
    return lit[0], lit[-1]


BASE_PERCENTILE = 0.10      # a judgement (the owner, 2026-10-19): one odd hour must not be the base


def _percentile(values: Sequence[float], q: float) -> float:
    """The ``q`` quantile of ``values``, interpolating between the two nearest."""
    v = sorted(values)
    pos = q * (len(v) - 1)
    lo = int(pos)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (pos - lo)


def base_consumption(now: datetime, use: Dict[float, float], production: Dict[float, float],
                     tz=None) -> Optional[dict]:
    """The 10th percentile (``BASE_PERCENTILE``) of the whole-house consumption
    forecast's hours between the sun forecast's first and last lit hour
    (``daylight``), for today and for tomorrow, and the LOWER of the two, in W:
    what the house draws by day with nothing optional on, the figure a daytime
    load is judged against. The minimum itself was one odd hour (Kozolec,
    2026-10-19: 8 W for tomorrow 11:00 against a day's 173 W). ``use`` is kWh an
    hour, so W = kWh x 1000. A day counts only the daytime hours the horizon
    still holds (today's start is behind it by afternoon); one with none, or
    with no sun forecast, is unknown and the other day stands alone. None when
    neither is. -> {"value", "attributes": {"today": {...}|None,
    "tomorrow": {...}|None}} - per day the percentile in ``watts``, the lowest
    hour's ``minimum_watts`` and its time ``at``, sunrise, sunset and the hours counted."""
    tz = tz or now.tzinfo
    today = now.astimezone(tz).date()
    days = {}
    for name, day in (("today", today), ("tomorrow", today + timedelta(days=1))):
        window = daylight(production, day, tz)
        hours = [] if window is None else [(use[k], k) for k in sorted(use) if window[0] <= k <= window[1]]
        if not hours:
            days[name] = None
            continue
        low, at = min(hours)          # the earliest hour of a tie
        days[name] = {"watts": round(_percentile([w for w, _ in hours], BASE_PERCENTILE) * 1000.0),
                      "minimum_watts": round(low * 1000.0), "at": _at(at, tz),
                      "sunrise": _at(window[0], tz), "sunset": _at(window[1] + HOUR, tz), "hours": len(hours)}
    known = [d["watts"] for d in days.values() if d]
    return {"value": min(known), "attributes": days} if known else None


def room_from_series(samples, now: datetime) -> Optional[float]:
    """``room_to_absorb`` for a dashboard device, from the hourly (hour start, kWh) series
    the forecast already reads for it; None values are skipped."""
    return room_to_absorb({t.timestamp(): v * 1000.0 for t, v in samples if v is not None}, now)


def plan_all(now: datetime, use: Dict[float, float], production: Dict[float, float], capacity: Optional[float],
             soc: Optional[float], full_soc: float, cfg: dict, tz=None) -> Dict[str, Optional[dict]]:
    """The three sensors from one set of inputs - what the coordinator and a
    replay both call. ``cfg``: ``days`` (1-7), ``production_scale``,
    ``consumption_scale``, ``floor``, and ``weights[sensor] = (consumption
    (full_h, drop), production (full_h, drop))``. -> {sensor: {"value",
    "attributes"}} with None for a sensor whose hours are not all there (the
    caller keeps its last value)."""
    tz = tz or now.tzinfo
    days = max(1, min(7, int(cfg["days"])))
    if not capacity or capacity <= 0:
        return {n: None for n in ("target", "minimum", "excess")}
    ps, cs = cfg["production_scale"], cfg["consumption_scale"]
    spec = {n: Spec(Side(*cfg["weights"][n][0]), Side(*cfg["weights"][n][1])) for n in cfg["weights"]}
    common = {
        "inputs": {"horizon_days": days, "production_scale": ps, "consumption_scale": cs,
                   "capacity_kwh": round(capacity, 2), "production_hours": len(production), "use_hours": len(use)},
    }

    def weights_of(n):
        return {"consumption": {"full_hours": spec[n].consumption.full_h, "derate_per_hour": spec[n].consumption.drop},
                "production": {"full_hours": spec[n].production.full_h, "derate_per_hour": spec[n].production.drop}}

    def one(n, hours, under_way, read, extra=None):
        bal = balance(now, hours, production, use, spec[n], ps, cs, under_way=under_way)
        if bal is None or (n == "excess" and soc is None):
            return None
        v, a = read(bal)
        return {"value": v, "attributes": {**a, **common, "weights": weights_of(n), **(extra or {})}}

    return {
        "target": one("target", days * 24, False, lambda b: target(b, full_soc, capacity, tz)),
        "minimum": one("minimum", days * 24, False, lambda b: minimum(b, cfg["floor"], capacity, tz)),
        "excess": one("excess", hours_until_day(now, days), True,
                      lambda b: excess(b, days, max(0.0, (100.0 - soc) / 100.0 * capacity), tz), {"soc": soc}),
    }
