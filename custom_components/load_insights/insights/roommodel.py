"""A heat-loss and heat-capacity model of a room with a thermostat heater. Pure.

    C dT/dt = P - UA (T_in - T_out) + s x sun + G

C kWh/K (what answers in hours), UA W/K (loss to the outside), P the heater's
power (all of it ends up as room heat), ``sun`` an optional series of the site
(measured PV power, or the sun forecast) with its own gain ``s``, G a constant
for everything the model leaves out (sensor height, the ground, small loads).
A constant offset in any sensor lands in G, not in UA or C.

Fitted on hourly rows by least squares and given up (None, with the reason)
when the data cannot carry it. Derived from it: the room to absorb, the day's
energy from a weather forecast, the coasting time. INS-24,
docs/specs/2026-10-21-room-thermal-model.md. Rooms only; a tank stays on
thermal.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

HOUR = 3600.0

# What the data must hold before the fit is believed. Counts, not watts: the
# outdoor spread is in steps of the room thermometer. MIN_* and the limits are
# judgements, benched (tests/roommodel_bench.py).
MIN_HOURS = 72           # rows with temperature, outdoor temperature and power all known
MIN_HEATED = 12          # of them, hours in which the heater drew
SPREAD_QUANTA = 15       # the outdoor difference must range over this many thermometer steps
T_CRIT = 3.0             # |t| of the heater and loss coefficients
HALF_LIMIT = 0.5         # UA of the two halves may differ by this share of their mean
HALF_LIMIT_C = 1.0       # C's: the noisier of the two (0.2 K steps; the daily energy does not use it), so wider
QUANTUM_PERCENTILE = 10
HEAT_W = 20.0            # an hour at or above this mean power counts as heated


@dataclass(frozen=True)
class Row:
    t: float                 # epoch of the hour's start
    dt_k: float              # T_in(h+1) - T_in(h)
    p_w: float               # the heater's mean power in the hour
    d_k: float               # mean T_in - T_out over the hour
    sun: Optional[float]     # the sun series' value for the hour, None when the site has none


@dataclass(frozen=True)
class RoomModel:
    c_kwh_per_k: float
    ua_w_per_k: float
    g_w: float
    sun_gain: Optional[float]    # W per unit of the sun series; None when no sun series was used
    hours: int
    heated: int
    resid_k: float               # residual sd of the hourly temperature change
    t_heater: float
    t_loss: float
    half_diff: float             # the largest relative difference of UA or C between the two halves

    @property
    def coast_hours(self) -> float:
        """The room's time constant C / UA."""
        return self.c_kwh_per_k * 1000.0 / self.ua_w_per_k

    def loss_w(self, t_in: float, t_out: float, sun: Optional[float] = None) -> float:
        """Net heat the room loses at this state with the heater off (negative: it warms by itself)."""
        gain = self.g_w + (self.sun_gain * sun if self.sun_gain is not None and sun is not None else 0.0)
        return self.ua_w_per_k * (t_in - t_out) - gain


def _percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def quantum(temps: Dict[float, float]) -> Optional[float]:
    """The thermometer's own step: a low percentile of its non-zero hourly changes."""
    seq = [temps[t] for t in sorted(temps)]
    steps = [abs(b - a) for a, b in zip(seq, seq[1:]) if b != a]
    return _percentile(steps, QUANTUM_PERCENTILE) if steps else None


def hourly_rows(temps: Dict[float, float], energy: Sequence[Tuple[float, Optional[float]]],
                outdoor: Dict[float, float], sun: Optional[Dict[float, float]] = None) -> List[Row]:
    """One row per hour in which the room's temperature at both ends, the outdoor
    temperature and the heater's energy are known. ``temps`` and ``outdoor``: hour
    start epoch -> degrees at the top of that hour; ``energy``: (hour start, kWh in
    the hour); ``sun``: hour start -> the sun series' mean over the hour."""
    rows = []
    for t, kwh in energy:
        if kwh is None or t not in temps or (t + HOUR) not in temps or t not in outdoor:
            continue
        out_end = outdoor.get(t + HOUR, outdoor[t])
        t_in = (temps[t] + temps[t + HOUR]) / 2.0
        t_out = (outdoor[t] + out_end) / 2.0
        rows.append(Row(t, temps[t + HOUR] - temps[t], kwh * 1000.0, t_in - t_out,
                        sun.get(t) if sun else None))
    return rows


def _solve(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    n = len(b)
    a = [r[:] for r in a]
    b = b[:]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(a[r][i]))
        if abs(a[piv][i]) < 1e-12:
            return None
        a[i], a[piv] = a[piv], a[i]
        b[i], b[piv] = b[piv], b[i]
        for r in range(n):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [x - f * z for x, z in zip(a[r], a[i])]
                b[r] -= f * b[i]
    return [b[i] / a[i][i] for i in range(n)]


def _lsq(x: List[List[float]], y: List[float]):
    """(coefficients, residual sd, standard errors) or None. ``x`` rows are regressors."""
    n = len(x[0])
    xtx = [[sum(r[i] * r[j] for r in x) for j in range(n)] for i in range(n)]
    xty = [sum(r[i] * v for r, v in zip(x, y)) for i in range(n)]
    co = _solve(xtx, xty)
    if co is None or len(y) <= n:
        return None
    res = [v - sum(c * z for c, z in zip(co, r)) for r, v in zip(x, y)]
    var = sum(e * e for e in res) / (len(y) - n)
    se = []
    for k in range(n):
        col = _solve(xtx, [1.0 if i == k else 0.0 for i in range(n)])
        if col is None or col[k] < 0:
            return None
        se.append(math.sqrt(var * col[k]))
    return co, math.sqrt(var), se


def _design(rows: Sequence[Row], use_sun: bool):
    # dT = a P h - b d h + g h [+ s sun h]   with h = 1 hour:  a = 1/C, b = UA/C
    x = []
    for r in rows:
        line = [r.p_w, -r.d_k, 1.0]
        if use_sun:
            line.append(r.sun)
        x.append(line)
    return x, [r.dt_k for r in rows]


def _physical(co: List[float]) -> Optional[Tuple[float, float, float, Optional[float]]]:
    a, b, g = co[0], co[1], co[2]
    if a <= 0 or b <= 0:
        return None
    c = 1.0 / a                                   # Wh per K
    return c, b * c, g * c, (co[3] * c if len(co) > 3 else None)


def fit(rows: Sequence[Row], step: Optional[float], why: Optional[Dict[str, object]] = None) -> Optional[RoomModel]:
    """The room's model from hourly ``rows``; None when the data cannot carry it.
    ``step``: the thermometer's own step (``quantum``). ``why`` (when given) says what was seen."""
    why = why if why is not None else {}
    rows = [r for r in rows if r.p_w >= 0]
    heated = sum(1 for r in rows if r.p_w >= HEAT_W)
    why.update(hours=len(rows), heated=heated)
    if len(rows) < MIN_HOURS:
        why["reason"] = f"{len(rows)} usable hours, {MIN_HOURS} needed"
        return None
    if heated < MIN_HEATED:
        why["reason"] = f"the heater drew in {heated} hours, {MIN_HEATED} needed"
        return None
    spread = max(r.d_k for r in rows) - min(r.d_k for r in rows)
    why["spread_k"] = round(spread, 2)
    if step is None or spread < SPREAD_QUANTA * step:
        why["reason"] = f"the indoor-outdoor difference ranges over {spread:.1f} K, too little to tell the loss from the constant"
        return None
    use_sun = all(r.sun is not None for r in rows) and len({r.sun for r in rows}) > 1
    x, y = _design(rows, use_sun)
    got = _lsq(x, y)
    if got is None:
        why["reason"] = "the fit is singular"
        return None
    co, sd, se = got
    phys = _physical(co)
    if phys is None:
        why["reason"] = "not physical: a non-positive heater or loss coefficient"
        return None
    t_a, t_b = co[0] / se[0], co[1] / se[1]
    why.update(t_heater=round(t_a, 1), t_loss=round(t_b, 1))
    if t_a < T_CRIT or t_b < T_CRIT:
        why["reason"] = f"not significant (t {t_a:.1f} for the heater, {t_b:.1f} for the loss, {T_CRIT} needed)"
        return None
    c, ua, g, s = phys
    half = len(rows) // 2
    diffs = []
    for part in (rows[:half], rows[half:]):
        gx, gy = _design(part, use_sun)
        sub = _lsq(gx, gy)
        sp = _physical(sub[0]) if sub else None
        if sp is None:
            why["reason"] = "one half of the window gives no physical fit"
            return None
        diffs.append(sp)
    for i in (0, 1):
        mean = (diffs[0][i] + diffs[1][i]) / 2.0
        if abs(diffs[0][i] - diffs[1][i]) > (HALF_LIMIT_C if i == 0 else HALF_LIMIT) * mean:
            why["reason"] = f"the two halves of the window disagree on {'C' if i == 0 else 'UA'}"
            return None
    half_diff = max(abs(diffs[0][i] - diffs[1][i]) / ((diffs[0][i] + diffs[1][i]) / 2.0) for i in (0, 1))
    return RoomModel(c / 1000.0, ua, g, s, len(rows), heated, sd, t_a, t_b, half_diff)


def room_to_absorb(m: RoomModel, current: Optional[float], boost: Optional[float], t_out: Optional[float],
                   rate_w: float, sun: Optional[float] = None, horizon_h: float = 12.0) -> Optional[float]:
    """kWh the heater takes to carry the room from ``current`` to ``boost``: the heat
    the room holds, ``C x rise``, plus what it loses on the way. The way takes
    ``C x rise / (rate - loss)`` hours; a heater that cannot out-run the loss has
    ``horizon_h`` hours, at its rate. None when something is unknown; never below zero."""
    if current is None or boost is None or t_out is None or rate_w <= 0:
        return None
    rise = boost - current
    if rise <= 0:
        return 0.0
    loss = m.loss_w((current + boost) / 2.0, t_out, sun)
    held = m.c_kwh_per_k * rise
    net = rate_w - loss
    if net <= 0:
        return round(min(rate_w * horizon_h / 1000.0, max(held + max(loss, 0.0) * horizon_h / 1000.0, 0.0)), 2)
    hours = min(held * 1000.0 / net, horizon_h)
    return round(max(held + loss * hours / 1000.0, 0.0), 2)


def daily_energy(m: RoomModel, setpoint: float, outdoor: Sequence[float], rating_w: float,
                 sun: Optional[Sequence[Optional[float]]] = None) -> float:
    """kWh the heater needs to hold ``setpoint`` through hours of ``outdoor`` temperatures
    (and ``sun``): the loss each hour, less the constant and the sun, within the heater's rating."""
    total = 0.0
    for i, t_out in enumerate(outdoor):
        s = sun[i] if sun is not None and i < len(sun) else None
        total += min(max(m.loss_w(setpoint, t_out, s), 0.0), rating_w)
    return total / 1000.0


def daily_by_mode(m: RoomModel, setpoints: Dict[str, float], outdoor: Dict[float, float], rating_w: float,
                  days: Sequence[Tuple[float, float]], sun: Optional[Dict[float, float]] = None) -> Dict[str, List[Optional[float]]]:
    """The energy of each day (``days``: (start, end) epochs) at each mode's setpoint
    (away, normal, boost: a mode is only a setpoint). A day is None unless every hour of it has an outdoor
    temperature; the sun, when the model has a sun term, is taken as 0 for an hour without it."""
    out: Dict[str, List[Optional[float]]] = {name: [] for name in setpoints}
    for a, b in days:
        keys = [a + i * HOUR for i in range(int(round((b - a) / HOUR)))]
        if any(k not in outdoor for k in keys):
            for name in setpoints:
                out[name].append(None)
            continue
        temps = [outdoor[k] for k in keys]
        suns = [(sun or {}).get(k, 0.0) for k in keys] if m.sun_gain is not None else None
        for name, sp in setpoints.items():
            out[name].append(round(daily_energy(m, sp, temps, rating_w, suns), 2))
    return out


def boost_share(targets: Dict[float, float], normal: float, boost: float, min_hours: int = 24) -> float:
    """The share of the hours spent at boost rather than at the normal setpoint, from the
    thermostat's target temperature at the top of each hour (an hour counts for the nearer of the
    two; away hours - nearer neither - are left out). 0.5 until ``min_hours`` hours say otherwise."""
    at_boost = at_normal = 0
    for tgt in targets.values():
        d_b, d_n = abs(tgt - boost), abs(tgt - normal)
        if min(d_b, d_n) > abs(boost - normal) / 2.0 + 1e-9:     # nearer to away than to either
            continue
        if d_b < d_n:
            at_boost += 1
        else:
            at_normal += 1
    n = at_boost + at_normal
    return 0.5 if n < min_hours else at_boost / n


KEEP_DAYS = 45           # how long a room's hourly rows are kept (the recorder's own states last about ten days)


def merge_hours(stored: Dict[str, list], temps: Dict[float, float], energy: Sequence[Tuple[float, Optional[float]]],
                outdoor: Dict[float, float], sun: Dict[float, float], targets: Dict[float, float],
                now_hour: float, keep_days: int = KEEP_DAYS):
    """The stored hourly rows (hour epoch as text -> [room temperature, kWh, outdoor, sun, target]) with
    the recorder's fresh hours merged in (a fresh value replaces; a missing one leaves the stored value),
    pruned to ``keep_days``; and the five dicts the fit reads, built from them. An hour's kWh is
    only taken once the hour is complete (before ``now_hour``)."""
    kwh = {t: v for t, v in energy if v is not None and t < now_hour}
    merged = {k: list(v) for k, v in stored.items()}
    for t in set(temps) | set(kwh) | set(outdoor) | set(sun) | set(targets):
        if t > now_hour:
            continue
        new = [temps.get(t), kwh.get(t), outdoor.get(t), sun.get(t), targets.get(t)]
        old = merged.get(str(int(t)), [None] * 5)
        merged[str(int(t))] = [n if n is not None else o for n, o in zip(new, old)]
    cutoff = now_hour - keep_days * 24 * HOUR
    merged = {k: v for k, v in merged.items() if float(k) >= cutoff}
    t_, e_, o_, s_, g_ = {}, [], {}, {}, {}
    for k in sorted(merged, key=float):
        t = float(k)
        tin, e, to, su, tg = merged[k]
        if tin is not None:
            t_[t] = tin
        if e is not None:
            e_.append((t, e))
        if to is not None:
            o_[t] = to
        if su is not None:
            s_[t] = su
        if tg is not None:
            g_[t] = tg
    return merged, t_, e_, o_, s_, g_
