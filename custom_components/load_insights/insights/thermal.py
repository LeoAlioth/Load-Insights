"""Energy per degree for thermostat loads. Pure.

A tank or heater that heats from T0 to T1 on E kWh has a thermal mass

    k = E / (T1 - T0)        [kWh per K]

so the energy it can still take to reach its boost setpoint is exactly

    room = max(0, boost - now) x k

Learned from the load's own hourly energy and the thermostat's temperature at the
top of each hour (INS-2, docs/specs/2026-10-18-energy-per-degree.md). A run is
consecutive hours in which the load drew; its energy is the sum over those hours
and its rise is the temperature after the run less the temperature before it.

Two kinds, with their own defaults: a hot water tank's energy has little to do
with the weather (a long window of runs, a tight spread), a room's loses heat to
the outside (a short window, so it follows the season, and a wider spread).
Runs in which the temperature ever falls are dropped (a hot-water draw, an open
window); the long runs, which weigh most in the figure, take care of the draws that did not show.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

HOUR = 3600.0

# "Heating" is an hour with at least this share of the load's own heavy hour
# (its 90th percentile of non-zero hours): measured per load, not in watts.
HEAT_SHARE = 0.2
# A run must rise by this many of the thermometer's own steps (the low
# percentile of its non-zero hourly changes), so a rise inside the reading's
# resolution is not a measurement.
RISE_QUANTA = 5
QUANTUM_PERCENTILE = 10


@dataclass(frozen=True)
class ThermalKind:
    name: str
    window: int          # the most recent runs that count
    min_runs: int        # below this the figure is unknown
    max_spread: float    # median absolute deviation / median above which it is unknown


TANK = ThermalKind("tank", window=12, min_runs=3, max_spread=0.35)
ROOM = ThermalKind("room", window=6, min_runs=3, max_spread=0.6)
KINDS = {k.name: k for k in (TANK, ROOM)}


@dataclass(frozen=True)
class Run:
    start: float        # epoch of the run's first hour
    kwh: float
    rise: float         # K

    @property
    def k(self) -> float:
        return self.kwh / self.rise


def _percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def heating_runs(energy: Sequence[Tuple[float, float]], temps: Dict[float, float]) -> List[Run]:
    """The clean heating runs. ``energy``: (hour start epoch, kWh) in time order;
    ``temps``: hour start epoch -> the thermostat's temperature then. A run is
    kept when the hour before it did not heat, the temperature is known at its
    start and just after its end, it never falls inside the run and it rises by
    ``RISE_QUANTA`` of the thermometer's steps."""
    drawn = [kwh for _, kwh in energy if kwh and kwh > 0]
    if len(drawn) < 2:
        return []
    heavy = _percentile(drawn, 90)
    hot = {t for t, kwh in energy if kwh is not None and kwh >= HEAT_SHARE * heavy}
    seq = [temps[t] for t in sorted(temps)]
    steps = [abs(b - a) for a, b in zip(seq, seq[1:]) if b != a]
    quantum = _percentile(steps, QUANTUM_PERCENTILE) if steps else None
    if quantum is None:
        return []
    kwh_of = {t: kwh for t, kwh in energy if kwh is not None}
    runs: List[Run] = []
    for t, _ in energy:
        if t not in hot or (t - HOUR) in hot:
            continue
        end = t
        while (end + HOUR) in hot:
            end += HOUR
        hours = [t + i * HOUR for i in range(int((end - t) / HOUR) + 2)]   # the run's hours and the one after
        if any(h not in temps for h in hours):
            continue
        series = [temps[h] for h in hours]
        if any(b < a for a, b in zip(series, series[1:])):
            continue
        rise = series[-1] - series[0]
        if rise < RISE_QUANTA * quantum:
            continue
        total = sum(kwh_of.get(t + i * HOUR, 0.0) for i in range(len(hours) - 1))
        runs.append(Run(t, total, rise))
    return runs


def kwh_per_degree(runs: Sequence[Run], kind: ThermalKind) -> Optional[Tuple[float, float, int]]:
    """(k, spread, runs used) from the kind's most recent runs: their total energy over
    their total rise, so the long runs weigh most (a one-degree run's standby loss
    would inflate its own ratio), and the relative spread of the single runs'
    figures about their median; None with too few runs or a spread above the kind's limit."""
    recent = sorted(runs, key=lambda r: r.start)[-kind.window:]
    if len(recent) < kind.min_runs:
        return None
    ks = [r.k for r in recent]
    mid = median(ks)
    if mid <= 0:
        return None
    spread = median(abs(k - mid) for k in ks) / mid
    k = sum(r.kwh for r in recent) / sum(r.rise for r in recent)
    return None if spread > kind.max_spread else (k, spread, len(recent))


def room_from_thermal(k: float, current: Optional[float], boost: Optional[float]) -> Optional[float]:
    """kWh the load takes to go from ``current`` to ``boost``; None when either is unknown."""
    if current is None or boost is None:
        return None
    return round(max(0.0, boost - current) * k, 2)
