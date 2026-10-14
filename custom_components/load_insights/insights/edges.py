"""Cross-meter edges, stage 1 (docs/specs/2026-10-14-cross-meter-edges.md).

An EDGE is a moment of change. It holds one entry per fed meter and channel -
the grid's three phases, each circuit meter's channels, each plug - and an
entry is that meter's OWN declared step (its own detector, one measure for
every meter), raw watts, with its reactive step and what its own readings
show around it (``properties``). An entry is 0 where the meter had read and
declared nothing then, unknown where it had not read yet. Nothing is
overwritten: choosing between entries is the grouping's business.

The same moment: a meter's step is read over its own span (PhaseState.declared,
from when the change can have happened - its latency back, see span_start -
to its first reading at the new level, and its lead on where its readings run
ahead of the grid's - the feeder's, see detect.LAG_PERCENTILE), and a meter's steps on several
channels are one event within its event window (rises as its detector formed
them, falls grouped here the same way). A step's ENTRY on the meter above is
that meter's step over the same moment on its phase (its phase map) nearest
it in size - and for a channel the map places nowhere yet, that meter's
event, its legs together, as one total may be on any phase. The PLACE is the
innermost meter with a non-zero entry, rises and falls alike, so an edge can
be born on a sub-meter alone. Steps on separate branches are separate edges:
an entry above that both reach into is kept on both, marked SHARED, and an
entry above whose children's steps do not add up to it is a place too - the
rest is a load no meter below reads (shared with them).

Across meters, sizes are compared and added in the GRID's terms: each step
times its meter's learned gain against the grid as of when its detector
classified it (Fleet.gain, 1 until learned; a nested meter against its parent
follows from both gains against the grid). Kozolec's Victron reads ~5 % below
the boiler's plug (10-04 14:57:44: +1825 against +1938, tolerance 105), so
raw, its moments failed to add up on calibration alone (stage-1 card,
2026-10-14: grid>Boiler 54 %, median ratio 0.949). The entries keep each
meter's raw step; only the comparisons take the gains.

Streaming: steps come in pass by pass (``Assembler.feed``) and a moment is
decided only once every meter has read past it by its own reach - nothing
it could still declare can touch it - so the slicing never changes an edge.

Grouping (``group``): per (place, direction, phase pattern on the place
meter), cut at the valleys of the place entry's size density and then of its
angle, as the detector's EdgeCluster library cuts today. The scorecard
(``scorecard``, ``conservation``, ``coverage``, ``compare``) needs no devices.

Pure: no Home Assistant. Fed beside the detector in replays
(tests/edge_bench.py); the detector is unchanged.
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from . import detect as _d   # read at call time, so the bench's dials reach the grouping

# How far past a step its properties read the channel: the ramp is looked for
# up to 90 s on, the on side after a rise up to 65 s (the throwaway extractor
# of 2026-10-14 that the properties were first measured with). A moment is
# decided only once every meter's readings reach this far past it.
PROPS_REACH_S = 90.0
# A step's levels are read over WINDOWS of its channel's readings (Anze,
# 2026-10-14: "an edge should include a couple readings before and after the
# actual step ... up to my 5 minute max delay ... and of course to the
# next/previous event"): before it from this long back or the channel's
# previous step, after it to this long on or its next, the transition left
# out (``windows``). A moment is decided once every meter has read this far
# past it.
WINDOW_S = 300.0
# A clear step (conservation and coverage, see _clear): one at least the
# meters' idle noises added together (PhaseState.noise, a step's floor) - the
# parent's channels and every child leg involved - with no factor on top
# (Anze, 2026-10-14: no multiplier). Conservation judges only those moments
# and counts the rest apart: of the grid>Hiša moments that did not add up,
# 4,461 of the 4,945 with a step of their size nearby had children under
# 100 W, their parent's step of that size anywhere in the two minutes looked
# in - chance, not a meter at fault. Not the noise at the level (noise_at):
# on a loaded grid phase that put the bar near 1 kW, and 1,452 of the
# hidrofor's 1,461 moments fell under it.


def _clear(step: float, floors: Sequence[float]) -> bool:
    return abs(step) >= sum(floors)

# The scorecard's judgements, from the spec (2026-10-14): a property mixed
# where it splits into two piles each holding this share of a group; a group
# counted with this many edges, seen on this many days.
MIXED_SHARE = 0.2
GROUP_MIN = 5
GROUP_DAYS = 3
# Each recorded property's histogram bin and smoothing for the mixed test, in
# its own units: the angle as the grouping cuts it today, the rest a
# judgement (ponytail: fixed; measure each property's own error when weights
# over them are benched) - and for the three that are readings over the step
# (ERR_PROPS), at least what the noise makes of them, the group's noise over
# its size: cut finer, a 50 W step's settling read in 10 W of noise split
# into "piles" by noise alone (Home's first card, 2026-10-14: settle mixed
# 27 % of edges).
PROP_BINS = {"angle": (None, None), "surge": (0.05, 0.15), "settle": (0.01, 0.03),
             "wobble": (0.01, 0.03), "ramp_n": (1.0, 1.0)}
ERR_PROPS = ("surge", "settle", "wobble")
PROPS = tuple(PROP_BINS)
MAD_SD = 1.4826                 # a median deviation as a standard deviation


@dataclass
class Step:
    """One meter's own declared step on one channel (PhaseState.declared, as
    its detector classified it)."""
    meter: str                  # "" the grid
    ch: str                     # the meter's own channel label (the grid's: its phase)
    t: float                    # since
    dp: float                   # raw watts
    dq: Optional[float]
    a: float                    # its span: from when the change can have happened...
    b: float                    # ...to its first reading at the new level
    phase: Optional[str]        # the grid phase(s) the meter's map gives the channel ("abc": a total on all three); None: not mapped yet
    window: float = 0.0         # its meter's event window
    interval: float = 0.0       # its reading interval then
    noise: float = 0.0          # its noise at the step (PhaseState.noise_at) - the tolerances'
    floor: float = 0.0          # the channel's idle noise (PhaseState.noise) - the grouping's unit
    event: Optional[int] = None  # the detector's own event (a rise's legs); None: grouped here
    gain: float = 1.0           # its meter's learned gain against the grid when classified (Fleet.gain)
    props: dict = field(default_factory=dict)
    declared: Optional[Tuple[float, Optional[float]]] = None   # (dp, dq) as declared, before a settle - see _settle_once
    absorbed: bool = False      # a settle another step took - see _settle_once

    @property
    def key(self) -> str:
        return f"{self.meter}|{self.ch}"

    @property
    def grid(self) -> float:
        """Its step in the grid's terms - what is compared across meters."""
        return self.dp * self.gain

    def entry(self) -> dict:
        return {"t": self.t, "dp": self.dp, "dq": self.dq, "angle": angle(self.dp, self.dq),
                "iv": self.interval, "noise": self.noise, **self.props}


def angle(dp: float, dq: Optional[float]) -> Optional[float]:
    """The reactive angle of a load's step, degrees - a load's rise and its fall
    the same (the detector's atan2(var, |watts|) has a fall's sign flipped)."""
    if dq is None or not dp:
        return None
    return math.degrees(math.atan2(dq if dp > 0 else -dq, abs(dp)))


def _median(xs):
    return statistics.median(xs) if xs else None


def _mad(xs):
    if len(xs) < 3:
        return None
    m = statistics.median(xs)
    return statistics.median(abs(x - m) for x in xs)


def _win(rows, a, b):
    i, j = bisect.bisect_left(rows, (a, -math.inf)), bisect.bisect_right(rows, (b, math.inf))
    return rows[i:j]


def _held(rows, t):
    i = bisect.bisect_right(rows, (t, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


def properties(rows: Sequence[Tuple[float, float]], t: float, dp: float,
               volts: Optional[Sequence[Tuple[float, float]]] = None) -> dict:
    """What a channel's own readings show around its step of ``dp`` at ``t``:
    ramp (seconds and readings from the last at the old level to the first
    at the new or past it, within 15 % of the step), surge ratio (the first readings'
    furthest excursion past the old level / the step: 1 where they overshoot
    nothing), settling (the on side's last 15 s less its first, over the
    minute after a rise or before a fall, / the step), wobble (the on side's
    median deviation less the off side's / the step), and the voltage before
    and after (medians over 30 s)."""
    out = dict.fromkeys(("ramp_s", "ramp_n", "surge", "settle", "wobble", "v0", "v1"))
    size, up = abs(dp), dp > 0
    pre = [v for _, v in _win(rows, t - 30, t - 0.01)]
    lo = _median(pre) if pre else _held(rows, t - 0.01)
    if lo is None or not size:
        return out
    hi, band = lo + dp, 0.15 * size
    after = _win(rows, t - 10, t + PROPS_REACH_S)
    t_off = next((ta for ta, v in reversed(after) if ta < t and abs(v - lo) <= band), None)
    t_on = next((ta for ta, v in after if ((v - hi) if up else (hi - v)) >= -band), None)   # at the new level or past it
    if t_on is not None:
        if t_off is None or t_off > t_on:
            t_off = max((ta for ta, _ in after if ta < t_on), default=t_on)
        out["ramp_s"] = t_on - t_off
        out["ramp_n"] = sum(1 for ta, _ in after if t_off < ta < t_on)
    t1 = t_on if t_on is not None else t
    first = [(ta, v) for ta, v in after if t1 <= ta <= t1 + 8][:3]
    peak = max(((v - lo) if up else (lo - v)) for _, v in first) if first else size
    out["surge"] = peak / size
    # where the old level was last read and where the new one is read past
    # the surge - the windows' ends (``windows``)
    out["t_off"] = t_off if t_off is not None else max((ta for ta, _ in after if ta < t), default=t - 0.01)
    out["t_set"] = (first[-1][0] if first else t1) + 0.01
    on = _win(rows, t1 + 5, t1 + 65) if up else _win(rows, t - 65, t - 5)
    off = _win(rows, t - 35, t - 5) if up else _win(rows, t1 + 5, t1 + 35)
    if len(on) >= 4:
        early = [v for ta, v in on if ta <= on[0][0] + 15]
        late = [v for ta, v in on if ta >= on[-1][0] - 15]
        out["settle"] = (_median(late) - _median(early)) / size
    m_on, m_off = _mad([v for _, v in on]), _mad([v for _, v in off])
    if m_on is not None and m_off is not None:
        out["wobble"] = (m_on - m_off) / size
    if volts:
        b4 = [v for _, v in _win(volts, t - 31, t - 1)]
        af = [v for _, v in _win(volts, t1 + 1, t1 + 31)]
        out["v0"] = _median(b4) if b4 else _held(volts, t - 1)
        out["v1"] = _median(af) if af else _held(volts, t1 + 1)
    return out


def windows(rows, t: float, dp: float, dq: Optional[float], p: dict, start: float, end: float,
            qrows=None, volts=None, noise: float = 0.0) -> Tuple[float, Optional[float], dict]:
    """A step's levels from its WINDOWS (WINDOW_S): before it, the readings
    from ``start`` (its channel's previous step, or WINDOW_S back) to where
    the old level was last read; after it, from past the surge to ``end``
    (the next step, or WINDOW_S on) - each level their median, so a voltage
    that lags the power or one reading's wobble does not set it. The same
    for the reactive power, the voltage (the on side's for R = V^2 / |dP|:
    after a rise, before a fall) and the spread of each (noise, a median
    deviation as a standard deviation). A window of fewer than two readings
    falls back to the step as declared (``dp``, ``dq``) and the readings
    beside it (``p``: ``properties``), and so does a step whose windows show
    none of its sign above its ``noise`` (``contra``). Returns (dP, dQ, the properties):
    p0/p1, q0/q1, pf0/pf1, v0/v1, dv, dv_kw (volts per kW), r, n0/n1, the
    reactive power's spread over both (qn), and
    the readings each window held (nb/na)."""
    def both(series, fall0, fall1, size):
        b = [v for _, v in _win(series or [], start, p["t_off"])]
        a = [v for _, v in _win(series or [], p["t_set"], end)]
        x0 = _median(b) if len(b) >= 2 else None
        x1 = _median(a) if len(a) >= 2 else None
        if x0 is None and x1 is None:
            x0, x1 = fall0, fall1
        elif x0 is None:
            x0 = None if size is None else x1 - size
        elif x1 is None:
            x1 = None if size is None else x0 + size
        return x0, x1, b, a
    lo = _median([v for _, v in _win(rows, t - 30, t - 0.01)])
    lo = (_held(rows, t - 0.01) or 0.0) if lo is None else lo
    p0, p1, b, a = both(rows, lo, lo + dp, dp)
    q0, q1, qb, qa = both(qrows, None, None, dq) if qrows else (None, None, [], [])
    v0, v1 = both(volts, p.get("v0"), p.get("v1"), None)[:2] if volts else (None, None)
    v0 = p.get("v0") if v0 is None else v0
    v1 = p.get("v1") if v1 is None else v1
    dP = p1 - p0
    contra = abs(dP) <= noise or (dP > 0) != (dp > 0)
    if contra:                       # the windows show no step of its sign above its noise: as declared
        p1, dP = p0 + dp, dp
    dQ = (q1 - q0) if q0 is not None and q1 is not None and not contra else dq
    out = {"p0": p0, "p1": p1, "q0": q0, "q1": q1, "v0": v0, "v1": v1, "nb": len(b), "na": len(a), "contra": contra,
           "pf0": p0 / math.hypot(p0, q0) if q0 is not None and (p0 or q0) else None,
           "pf1": p1 / math.hypot(p1, q1) if q1 is not None and (p1 or q1) else None,
           "n0": MAD_SD * _mad(b) if len(b) >= 3 else None, "n1": MAD_SD * _mad(a) if len(a) >= 3 else None,
           "qn": math.hypot(MAD_SD * (_mad(qb) or 0.0), MAD_SD * (_mad(qa) or 0.0)) if qa or qb else None}
    out["dv"] = v1 - v0 if v0 is not None and v1 is not None else None
    out["dv_kw"] = out["dv"] / (dP / 1000.0) if out["dv"] is not None and dP else None
    von = v1 if dP > 0 else v0
    out["r"] = von * von / abs(dP) if von is not None and dP else None
    for k in ("surge", "settle", "wobble"):          # the transition's, over the step as its windows read it
        if p.get(k) is not None and dP:
            out[k] = p[k] * abs(dp) / abs(dP)
    return dP, dQ, out


class _Group:
    """A meter's steps taken together: an EVENT - its channels at one moment -
    or a PIECE of one, what is matched against the meter above: a channel its
    map places on a phase, alone, or its channels mapped nowhere yet,
    together (they may be on any phase, as one total)."""
    __slots__ = ("meter", "legs", "t", "a", "b", "phases", "event")

    def __init__(self, meter: str, legs: List[Step], event: Optional["_Group"] = None):
        self.meter, self.legs, self.event = meter, legs, event
        self.t, self.a, self.b = min(x.t for x in legs), min(x.a for x in legs), max(x.b for x in legs)
        self.phases = _phases(legs)

    @property
    def dp(self) -> float:
        """In the grid's terms; its legs' steps as read so far (``windows``
        at a decision)."""
        return sum(x.grid for x in self.legs)

    def pieces(self) -> List["_Group"]:
        """Each leg on one phase alone; those on any phase or several together."""
        loose = [x for x in self.legs if x.phase is None or len(x.phase) > 1]
        return [_Group(self.meter, [x], self) for x in self.legs if x.phase is not None and len(x.phase) == 1] + \
            ([_Group(self.meter, loose, self)] if loose else [])

    @property
    def loose(self) -> bool:
        return any(x.phase is None or len(x.phase) > 1 for x in self.legs)


def _phases(legs: Sequence[Step]):
    """The grid phases ``legs`` may be on, None: any."""
    return None if any(x.phase is None for x in legs) else set("".join(x.phase for x in legs))


def _fits(phase: Optional[str], phases) -> bool:
    """Can a channel on ``phase`` (one or several, None: any) be on ``phases``?"""
    return phase is None or phases is None or bool(set(phase) & phases)


def _tol(noises: Sequence[float], size: float) -> float:
    """Two meters' views of one change agree within both meters' noise and
    METER_CAL_SLACK of it - as the Fleet places a step (_step_meter)."""
    return math.hypot(*noises) + _d.METER_CAL_SLACK * abs(size) if noises else _d.METER_CAL_SLACK * abs(size)


class Assembler:
    """Every meter's declared steps, fed pass by pass, assembled into edges
    once each moment is decided (``edges``); what each parent's step was
    against the steps below it, both in the grid's terms (``cons``)."""

    def __init__(self, parents: Optional[Dict[str, Optional[str]]] = None):
        self.parents = {k: v for k, v in (parents or {}).items() if v}   # meter -> the meter it hangs under
        self.steps: List[Step] = []          # not decided yet
        self.rows: Dict[Tuple[str, str], list] = {}
        self.qrows: Dict[Tuple[str, str], list] = {}     # reactive power, read as the rows
        self.qsigned: Dict[Tuple[str, str], float] = {}  # from when each channel's reactive power is signed - see signed
        self.marks: Dict[str, list] = {}     # each channel's steps, [(t, id, step)] - a window's ends (_ends)
        self.volts: Dict[Tuple[str, str], list] = {}
        self.first: Dict[Tuple[str, str], float] = {}
        self.chans: Dict[str, Dict[str, Optional[str]]] = {"": {}}
        self.noise: Dict[Tuple[str, str], float] = {}
        self.floor: Dict[Tuple[str, str], float] = {}     # each channel's idle noise - see _clear
        self.latency: Dict[str, float] = {}
        self.edges: List[dict] = []
        self.cons: List[dict] = []
        self.late = 0                         # steps reaching into a moment already decided
        self._until = -math.inf
        self._ids = 0

    # ------------------------------------------------ feeding
    def feed(self, steps: Sequence[Step], rows=None, volts=None, frontier=None, reach=None,
             now: float = math.inf, chans=None, noise=None, latency=None, floor=None, qrows=None,
             qsigned=None) -> None:
        """A pass: the steps its detectors classified; each channel's new
        readings and voltages, {(meter, ch): [(t, x)]}; per meter the earliest
        step it may still declare (``frontier``) and how far a step's moment
        reaches back from it (``reach``: its latency and event window); how
        far the readings reach (``now``); each meter's channels and their
        grid phases, {meter: {ch: phase or None}}; each channel's noise; each
        meter's latency; each channel's idle noise (``floor``); its reactive
        readings (``qrows``) and from when they are the meter's own signed
        ones, {(meter, ch): t} (``qsigned``; one worked out from the apparent
        power or V x I has no sign)."""
        for store, fed in ((self.rows, rows), (self.volts, volts), (self.qrows, qrows)):
            for k, rr in (fed or {}).items():
                if not rr:
                    continue
                have = store.setdefault(k, [])
                if have and rr[0][0] < have[-1][0]:
                    have.extend(rr)
                    have.sort()
                else:
                    have.extend(rr)
                if store is self.rows:
                    self.first.setdefault(k, rr[0][0])
        self.qsigned.update(qsigned or {})
        for m, cs in (chans or {}).items():
            self.chans.setdefault(m, {}).update(cs)
        self.noise.update(noise or {})
        self.floor.update(floor or {})
        self.latency.update(latency or {})
        for s in steps:
            self.chans.setdefault(s.meter, {}).setdefault(s.ch, s.phase)
            bisect.insort(self.marks.setdefault(s.key, []), (s.t, id(s), s))
            if s.a < self._until:
                self.late += 1
            self.steps.append(s)
        until = min((f - (reach or {}).get(m, 0.0) for m, f in (frontier or {}).items()), default=math.inf)
        self._decide(min(until, now - WINDOW_S), now)

    def flush(self) -> None:
        """The readings ended: every moment left is decided."""
        self._decide(math.inf, math.inf)

    # ------------------------------------------------ topology
    def ancestors(self, m: str) -> List[str]:
        """The meters ``m`` hangs under, innermost first, the grid last."""
        out, seen = [], {m}
        while m:
            m = self.parents.get(m) or ""
            if m in seen:
                break
            seen.add(m)
            out.append(m)
        return out

    def under(self, n: str, anc: str) -> bool:
        return n != anc and anc in self.ancestors(n)

    # ------------------------------------------------ deciding
    def _events(self, steps: List[Step]) -> List[_Group]:
        """Each meter's steps at one moment: a rise's legs as its detector
        formed the event, falls the same way here - channels within the
        meter's event window, the smallest at least EVENT_BALANCE of the
        largest (Detector._form_event)."""
        out, by = [], {}
        for s in steps:
            if s.event is not None:
                by.setdefault((s.meter, s.event), []).append(s)
        out += [_Group(m, legs) for (m, _), legs in by.items()]
        loose = sorted((s for s in steps if s.event is None), key=lambda s: (s.meter, s.t, s.ch))
        taken = set()
        for i, s in enumerate(loose):
            if id(s) in taken:
                continue
            legs, chs, near = [s], {s.ch}, []
            for x in loose[i + 1:]:
                if x.meter != s.meter or x.t - s.t > s.window:
                    break
                near.append(x)
            for x in sorted(near, key=lambda x: (x.t - s.t, x.ch)):
                if id(x) in taken or x.ch in chs or (x.dp > 0) != (s.dp > 0):
                    continue
                ws = [abs(y.dp) for y in legs] + [abs(x.dp)]
                if min(ws) < _d.EVENT_BALANCE * max(ws):
                    continue
                legs.append(x)
                chs.add(x.ch)
            taken.update(id(x) for x in legs)
            out.append(_Group(s.meter, legs))
        return sorted(out, key=lambda e: (e.t, e.meter, e.legs[0].ch))


    def _up(self, p: _Group, pieces: Dict[str, tuple], events: Dict[str, tuple]) -> List[Step]:
        """``p``'s entry on the nearest meter above that stepped over its
        moment: a piece on one phase, that meter's piece on it whose span
        overlaps, nearest in size (then in time); a piece on any phase, that
        meter's event, its legs together - a three-phase charger read as one
        total is the grid's three legs. Its steps, or none."""
        for anc in self.ancestors(p.meter):
            got = (events if p.loose else pieces).get(anc)
            if not got:
                continue
            starts, cands, longest = got
            best = None
            for k in range(bisect.bisect_right(starts, p.b) - 1, -1, -1):
                q = cands[k]
                if q.a < p.a - longest:
                    break
                legs = [x for x in q.legs if _fits(x.phase, p.phases)]
                if not any(x.a <= p.b and x.b >= p.a for x in legs):
                    continue
                rank = (abs(sum(x.grid for x in legs) - p.dp), abs(q.t - p.t), q.t, legs[0].key)
                if best is None or rank < best[0]:
                    best = (rank, legs)
            if best is not None:
                return best[1]
        return []

    def _next_read(self, x: Step) -> float:
        """The first reading of ``x``'s channel after its own, within
        PROPS_REACH_S (``x.t`` where there is none) - see _settle_once."""
        rows = self.rows.get((x.meter, x.ch)) or []
        i = bisect.bisect_right(rows, (x.t + 0.01, math.inf))
        return rows[i][0] if i < len(rows) and rows[i][0] <= x.t + PROPS_REACH_S else x.t

    def _settle_once(self) -> Tuple[List[Step], Dict[int, Step]]:
        """A level a meter read once is no level (Anze, 2026-10-14): a step
        whose channel's next reading starts a step back toward where it came
        from, no more than half its size, is measured to where the meter
        settled - that second step is its settle, not a step of its own, and
        its first reading its surge (``properties``). Kozolec's boiler plug read a
        switch-on as 1902 W, its silence confirmed it, and its next report,
        40 s on, read 1875 - the element warming: declared -27 W, it was
        blind on the grid 131 times in ten days. A step is held until its
        channel's next reading is decided (``_decide``), so the slicing
        never changes it; each pair is judged on the steps as declared, and
        a step that is a settle absorbs none, so neither does the order they
        come in. Returns the steps left and {absorber: its settle}."""
        by: Dict[str, List[Step]] = {}
        for x in sorted(self.steps, key=lambda x: x.t):
            if x.declared is None:
                x.declared = (x.dp, x.dq)
            x.dp, x.dq = x.declared
            by.setdefault(x.key, []).append(x)
        took: Dict[int, Step] = {}
        gone = set()
        for xs in by.values():
            for x, y in zip(xs, xs[1:]):
                if (id(x) not in gone and (x.dp > 0) != (y.dp > 0) and abs(y.dp) <= abs(x.dp) / 2
                        and y.t > x.t and abs(y.t - self._next_read(x)) <= 0.01):
                    x.dp += y.dp
                    x.dq = None if x.dq is None or y.dq is None else x.dq + y.dq
                    gone.add(id(y))
                    took[id(x)] = y
        for x in self.steps:
            x.absorbed = id(x) in gone
        return [x for x in self.steps if id(x) not in gone], took

    def _decide(self, until: float, now: float) -> None:
        if not self.steps or min(max(x.b, x.t) for x in self.steps) >= until:
            return                                # nothing can have closed
        steps, took = self._settle_once()
        evs = self._events(steps)
        parts = {id(e): e.pieces() for e in evs}
        piece_of = {id(x): q for e in evs for q in parts[id(e)] for x in q.legs}
        idx = []
        for groups in ([q for e in evs for q in parts[id(e)]], evs):
            by: Dict[str, list] = {}
            for g in groups:
                by.setdefault(g.meter, []).append(g)
            idx.append({m: ([g.a for g in gs], gs, max(g.b - g.a for g in gs))
                        for m, gs in ((m, sorted(gs, key=lambda g: g.a)) for m, gs in by.items())})
        root = {id(e): id(e) for e in evs}

        def find(k):
            while root[k] != k:
                root[k] = root[root[k]]
                k = root[k]
            return k
        ups = {}
        for e in evs:
            for q in parts[id(e)]:
                ups[id(q)] = self._up(q, idx[0], idx[1])
                for x in ups[id(q)]:
                    root[find(id(e))] = find(id(piece_of[id(x)].event))
        comps: Dict[int, List[_Group]] = {}
        for e in evs:
            comps.setdefault(find(id(e)), []).append(e)
        keep = []
        for comp in sorted(comps.values(), key=lambda c: min(e.t for e in c)):
            reach = max(max(x.b, x.t + WINDOW_S, self._next_read(x)) for e in comp for x in e.legs)
            if reach < until and max(e.t for e in comp) + WINDOW_S <= now:
                comp.sort(key=lambda e: (e.t, e.meter, e.legs[0].ch))
                self._settle(comp, [q for e in comp for q in parts[id(e)]], ups, piece_of)
            else:
                keep += [x for e in comp for x in e.legs]
        self._until = max(self._until, until)
        self.steps = keep + [took[id(x)] for x in keep if id(x) in took]   # a settle goes with its step
        self._prune(min([x.t for x in keep] + [until]))

    def _prune(self, oldest: float) -> None:
        """Readings older than any moment still to decide, its reach and the
        longest latency back, go - the last one before kept, as held."""
        if not math.isfinite(oldest):
            return
        cut = oldest - WINDOW_S - max(self.latency.values(), default=0.0) - 60.0
        for store in (self.rows, self.volts, self.qrows, self.marks):
            for k, rr in store.items():
                i = bisect.bisect_left(rr, (cut, -math.inf))
                if i > 1:
                    del rr[:i - 1]

    def _settle(self, comp: List[_Group], pieces: List[_Group], ups: dict, piece_of: dict) -> None:
        """A decided moment: each step's properties; the pieces and the steps
        above they took, in clusters - a parent's steps against its
        children's, which add up or not; every leg no cluster that adds up
        explains is a place; and the edges."""
        for e in comp:
            for x in e.legs:
                rows, v = self.rows.get((x.meter, x.ch)) or [], self._volts(x)
                x.props = properties(rows, x.t, x.dp, v)
                if "t_off" in x.props:
                    x.dp, x.dq, w = windows(rows, x.t, x.dp, x.dq, x.props, *self._ends(x),
                                            self.qrows.get((x.meter, x.ch)), v, x.noise)
                    del x.props["t_off"], x.props["t_set"]
                    x.props.update(w)
        root: Dict[tuple, tuple] = {}

        def find(k):
            root.setdefault(k, k)
            while root[k] != k:
                root[k] = root[root[k]]
                k = root[k]
            return k
        for q in pieces:
            for x in ups[id(q)]:
                root[find(("c", id(q)))] = find(("s", id(x)))
        clusters: Dict[tuple, tuple] = {}
        for q in pieces:
            for x in ups[id(q)]:
                kids, parents = clusters.setdefault(find(("c", id(q))), ({}, {}))
                kids[id(q)], parents[id(x)] = q, x
        explained = set()
        for kids, parents in clusters.values():
            ps, ks = sorted(parents.values(), key=lambda x: x.key), sorted(kids.values(), key=lambda q: (q.t, q.meter))
            p, c = sum(x.grid for x in ps), sum(q.dp for q in ks)
            noises = [x.noise for x in ps] + [x.noise for q in ks for x in q.legs]
            tol = _tol(noises, p)
            ok = abs(p - c) <= tol
            self.cons.append({"t": min(x.t for x in ps), "parent": ps[0].meter, "children": sorted({q.meter for q in ks}),
                              "p": p, "c": c, "tol": tol, "ok": ok, "at": min(q.t for q in ks),
                              "clear": _clear(c, [x.floor or x.noise for x in ps + [y for q in ks for y in q.legs]]),
                              "phases": None if any(q.phases is None for q in ks) else sorted(set().union(*(q.phases for q in ks))),
                              "keys": [x.key for x in ps] + [x.key for q in ks for x in q.legs]})
            if ok:
                explained.update(parents)
        for q in pieces:
            self._skipped(q, ups[id(q)])
        built = []
        for f in comp:
            places = [x for x in f.legs if id(x) not in explained]
            if places:
                built.append(self._edge(places, ups, piece_of))
        holders: Dict[int, list] = {}
        for edge, legs in built:
            for x in legs:
                holders.setdefault(id(x), []).append(edge)
        for edge, legs in built:
            for x in legs:
                others = [o["id"] for o in holders[id(x)] if o is not edge]
                if others:
                    edge["shared"][x.key] = others
            self.edges.append(edge)

    def signed(self, legs: Sequence[Step]) -> bool:
        """Do ``legs``' channels read a SIGNED reactive power then (``qsigned``:
        the meter publishes one - Home's grid from 30 Sep, Andrej's from
        8 Oct; one worked out from the apparent power, as Home's 3EMs', has
        no sign)?"""
        return all(self.qsigned.get((x.meter, x.ch), math.inf) <= x.t for x in legs)

    def _reactive(self, meter: str, places: List[Step], held: List[Step]):
        """The edge's reactive angle (Anze, 2026-10-14): its size from the
        innermost meter of its chain that reads a reactive power, its sign
        from the innermost that reads it signed - the place's own where it
        has them, else a meter's above whose entry is the same step (its
        step and the place's agree within both meters' noise, as
        conservation judges them) and whose reactive step clears that
        meter's own reactive noise (its windows' spread, ``windows``).
        Returns (the meter the size came from, the sign's, the angle -
        |angle| where no meter gave a sign; None, None, None with no size)."""
        dp = sum(x.grid for x in places)

        def lends(legs):
            if not legs or any(x.dq is None for x in legs):
                return False
            if legs is places:
                return True
            if abs(sum(x.grid for x in legs) - dp) > _tol([x.noise for x in legs + places], dp):
                return False
            return abs(sum(x.dq for x in legs)) > math.hypot(*[x.props.get("qn") or 0.0 for x in legs])
        chain = [(meter, places)] + [(m, [x for x in held if x.meter == m]) for m in self.ancestors(meter)]
        mag = next(((m, legs) for m, legs in chain if lends(legs)), None)
        if mag is None:
            return None, None, None
        sgn = next(((m, legs) for m, legs in chain if self.signed(legs) and lends(legs)), None)
        size = abs(math.degrees(math.atan2(sum(x.dq for x in mag[1]), abs(dp))))
        if sgn is None:
            return mag[0], None, size
        load = sum(x.dq for x in sgn[1]) * (1 if sum(x.dp for x in sgn[1]) > 0 else -1)   # a load's, rise or fall
        return mag[0], sgn[0], math.copysign(size, load)

    def _volts(self, x: Step):
        """The voltage at ``x``'s channel: its meter's own, else a meter's
        above on its phase, else the grid's."""
        own = self.volts.get((x.meter, x.ch))
        if own or not x.phase:
            return own
        for anc in self.ancestors(x.meter):
            for c, ph in sorted(self.chans.get(anc, {}).items()):
                if ph == x.phase and self.volts.get((anc, c)):
                    return self.volts[(anc, c)]
        return None

    def _ends(self, x: Step) -> Tuple[float, float]:
        """Where ``x``'s windows end: its channel's previous step (where that
        one was read at its new level) or WINDOW_S back; its next step (from
        when it can have begun) or WINDOW_S on. A settle another step took
        (``_settle_once``) is no step."""
        ms = self.marks.get(x.key) or []
        i = bisect.bisect_left(ms, (x.t, -1))
        prev = next((m[2] for m in reversed(ms[:i]) if not m[2].absorbed), None)
        nxt = next((m[2] for m in ms[i:] if m[2].t > x.t and not m[2].absorbed), None)
        start = max(x.t - WINDOW_S, prev.b if prev is not None else -math.inf)
        end = min(x.t + WINDOW_S, nxt.a if nxt is not None and nxt.t <= x.t + WINDOW_S else math.inf)
        return start, end

    def _skipped(self, q: _Group, up: List[Step]) -> None:
        """Conservation where a meter above showed nothing: each meter between
        ``q``'s and its entry's (or above it all), read at the moment on
        ``q``'s phases, against ``q``'s step."""
        top = up[0].meter if up else None
        for anc in self.ancestors(q.meter):
            if anc == top:
                break
            chans = [c for c, ph in self.chans.get(anc, {}).items() if _fits(ph, q.phases)]
            if not chans or any(self.first.get((anc, c), math.inf) > q.t for c in chans):
                continue
            noise = max(self.noise.get((anc, c), 0.0) for c in chans)
            tol = _tol([noise] + [x.noise for x in q.legs], q.dp)
            self.cons.append({"t": q.t, "parent": anc, "children": [q.meter], "p": 0.0, "c": q.dp, "tol": tol,
                              "ok": abs(q.dp) <= tol, "at": q.t,
                              "clear": _clear(q.dp, [self.floor.get((anc, c), self.noise.get((anc, c), 0.0)) for c in chans]
                                              + [x.floor or x.noise for x in q.legs]), "phases": None if q.phases is None else sorted(q.phases),
                              "keys": [x.key for x in q.legs]})

    def _edge(self, places: List[Step], ups: dict, piece_of: dict) -> Tuple[dict, List[Step]]:
        """The edge placed at ``places``' meter: their entries, the chain of
        entries above, and every other channel of the place, of the meters
        above and below it on its phases, 0 or unknown."""
        self._ids += 1
        t, meter = min(x.t for x in places), places[0].meter
        dp = sum(x.dp for x in places)
        phases = _phases(places)
        held = list(places)
        entries = {x.key: x.entry() for x in places}
        todo, seen = list(places), set()
        while todo:
            q = piece_of[id(todo.pop())]
            if id(q) in seen:
                continue
            seen.add(id(q))
            for x in ups[id(q)]:
                if x.key not in entries:
                    entries[x.key] = x.entry()
                    held.append(x)
                todo.append(x)
        blind, silent = [], []
        above, chain = set(self.ancestors(meter)), {x.meter for x in held}
        others = [m for m in self.chans if m != meter and (m in above or self.under(m, meter))]
        for m in [meter] + sorted(others):
            states = []
            for c, ph in sorted(self.chans.get(m, {}).items()):
                k = f"{m}|{c}"
                if k in entries or (m != meter and not _fits(ph, phases)):
                    continue
                if self.first.get((m, c), math.inf) > t:
                    entries[k] = None
                else:
                    entries[k] = 0.0
                    lat = self.latency.get(m, 0.0)
                    if not _win(self.rows.get((m, c)) or [], t - lat, t + lat):
                        silent.append(k)
                states.append((c, entries[k]))
            if m in above and m not in chain and states and all(v is not None for _, v in states):
                # a meter above read through the moment and showed nothing of a clear step
                if _clear(sum(x.grid for x in places), [self.floor.get((m, c), self.noise.get((m, c), 0.0)) for c, _ in states]
                          + [x.floor or x.noise for x in places]):
                    blind.append(m)
        size = abs(dp)
        q_from, sign_from, ang = self._reactive(meter, places, held)
        props = {}
        for p in ("surge", "settle", "wobble", "ramp_n", "ramp_s"):
            got = [(abs(x.dp), x.props.get(p)) for x in places if x.props.get(p) is not None]
            props[p] = sum(w * v for w, v in got) / sum(w for w, _ in got) if got else None
        edge = {"id": self._ids, "t": t, "place": meter, "dir": 1 if dp > 0 else -1,
                "pattern": "".join(sorted(x.ch for x in places)), "size": size,
                "angle": ang, "q_from": q_from, "sign_from": sign_from,
                "unsigned": ang is not None and sign_from is None,
                "noise": sum(x.floor or x.noise for x in places), "err": sum(x.noise for x in places),
                "legs": [x.key for x in places],
                "entries": entries, "shared": {}, "silent": silent, "blind": blind, **props}
        return edge, held


# ------------------------------------------------------------- grouping
def _spread(xs: Sequence[float]) -> Optional[float]:
    m = _mad(list(xs))
    return None if m is None else MAD_SD * m


def group(edges: Sequence[dict]) -> List[dict]:
    """Edges grouped per (place, direction, phase pattern), cut at the valleys
    of the place entry's size density - measurement errors of the quietest
    noise the group's place saw, as the detector's histogram keeps its unit
    (Detector._by_density) - and then of its angle; a step without an angle
    joins its size segment's largest group, as today (_same_angle).
    ponytail: one batch over the decided edges, no fading (EDGE_TAU_S) - a
    streaming library is the stage that makes these the detector's edges."""
    by: Dict[tuple, List[dict]] = {}
    for e in edges:
        by.setdefault((e["place"], e["dir"], e["pattern"]), []).append(e)
    out = []
    for key in sorted(by):
        es = sorted(by[key], key=lambda e: e["t"])
        unit = _d.EDGE_NOISE_SHARE * max(min(e["noise"] for e in es), _d.MIN_NOISE_W)
        bins = {e["id"]: _d._bin(e["size"], unit) for e in es}
        for lo, hi in _d.valley_segments(Counter(bins.values())):
            seg = [e for e in es if lo <= bins[e["id"]] <= hi]
            angled = [e for e in seg if e["angle"] is not None]
            parts = []
            if angled:
                ab = {e["id"]: int(math.floor(e["angle"] / _d.EDGE_ANGLE_BIN)) for e in angled}
                for alo, ahi in _d.valley_segments(Counter(ab.values()), _d.EDGE_ANGLE_KERNEL / _d.EDGE_ANGLE_BIN):
                    parts.append([e for e in angled if alo <= ab[e["id"]] <= ahi])
            rest = [e for e in seg if e["angle"] is None]
            if rest and parts:
                max(parts, key=len).extend(rest)
            elif rest:
                parts.append(rest)
            out += [{"key": key, "unit": unit, "items": sorted(p, key=lambda e: e["t"])} for p in parts if p]
    return out


def summary(g: dict) -> dict:
    """What a group's edges hold, median and spread: the place entry's size
    (and in measurement errors), its angle and properties, and every other
    entry's step."""
    items = g["items"]
    out = {"key": list(g["key"]), "n": len(items), "unit": g["unit"]}
    for p in ("size", "angle") + PROPS:
        xs = [e[p] for e in items if e.get(p) is not None]
        out[p] = [_median(xs), _spread(xs)] if xs else None
    ent: Dict[str, list] = {}
    for e in items:
        for k, v in (e.get("entries") or {}).items():
            if isinstance(v, dict) and k not in e["legs"]:
                ent.setdefault(k, []).append(v["dp"])
    out["entries"] = {k: [_median(v), _spread(v), len(v)] for k, v in sorted(ent.items())}
    return out


# ------------------------------------------------------------- scorecard
def _scaled(g: dict) -> List[float]:
    return [_d.edge_scale(e["size"], g["unit"]) for e in g["items"]]


def _piles(xs: Sequence[float], bin_w: float, sd: float) -> List[int]:
    """How many of ``xs`` each pile holds, cut at the valleys of their
    density as the grouping cuts (valley_segments)."""
    h = Counter(int(math.floor(x / bin_w)) for x in xs)
    return [sum(n for b, n in h.items() if lo <= b <= hi) for lo, hi in _d.valley_segments(h, sd / bin_w)]


def _centre(g: dict) -> tuple:
    """(size in measurement errors and its spread, angle and its spread)."""
    s = _scaled(g)
    a = [e["angle"] for e in g["items"] if e.get("angle") is not None]
    return _median(s), _spread(s) or 0.0, _median(a), _spread(a) or 0.0


def _agree(ca: tuple, cb: tuple) -> bool:
    """Two groups' sizes and angles agree within measurement error: one error
    of size (EDGE_KERNEL) or their spreads together, the angle's smoothing
    (EDGE_ANGLE_KERNEL) or theirs; no angle on either side, or none on both."""
    if abs(ca[0] - cb[0]) > max(_d.EDGE_KERNEL, math.hypot(ca[1], cb[1])):
        return False
    if ca[2] is None or cb[2] is None:
        return ca[2] is None and cb[2] is None
    return abs(ca[2] - cb[2]) <= max(_d.EDGE_ANGLE_KERNEL, math.hypot(ca[3], cb[3]))


def scorecard(groups: Sequence[dict]) -> dict:
    """The groups' figures (4-8 of the spec), shares by edges:
    tightness - the place entry's size spread in measurement errors, per group
    of three or more, weighted by edges (1: as tight as the meter allows);
    mixed - edges in groups of GROUP_MIN or more where a recorded property
    splits into two piles each holding MIXED_SHARE of the group, cut as the
    grouping cuts (PROP_BINS; an item's "err" is its noise in watts);
    duplicates - edges in groups with a same-key group of agreeing size and angle;
    count - groups, those of GROUP_MIN edges or more, the share of edges in
    smaller ones, groups seen on GROUP_DAYS days or more;
    mirror - rise edges in groups with a fall group of the same place and
    pattern of agreeing size and angle."""
    total = sum(len(g["items"]) for g in groups) or 1
    tight = [(len(g["items"]), _spread(_scaled(g))) for g in groups if len(g["items"]) >= 3]
    mixed_by = {p: 0 for p in PROPS}
    mixed = 0
    for g in groups:
        n = len(g["items"])
        if n < GROUP_MIN:
            continue
        hit = False
        err = _median([e["err"] / e["size"] for e in g["items"] if e.get("err") and e["size"]]) or 0.0
        for p, (bw, sd) in PROP_BINS.items():
            bw, sd = (bw or _d.EDGE_ANGLE_BIN, sd or _d.EDGE_ANGLE_KERNEL)
            if p in ERR_PROPS and err > sd:
                bw, sd = err * bw / sd, err
            xs = [e[p] for e in g["items"] if e.get(p) is not None]
            if len(xs) >= GROUP_MIN and sum(1 for c in _piles(xs, bw, sd) if c >= MIXED_SHARE * n) >= 2:
                mixed_by[p] += n
                hit = True
        mixed += n if hit else 0
    centres = [_centre(g) for g in groups]
    by_key: Dict[tuple, List[int]] = {}
    for i, g in enumerate(groups):
        by_key.setdefault(tuple(g["key"]), []).append(i)
    dup = sum(len(groups[i]["items"]) for ids in by_key.values() for i in ids
              if any(j != i and _agree(centres[i], centres[j]) for j in ids))
    rises = [i for i, g in enumerate(groups) if g["key"][1] > 0]
    mirrored = sum(len(groups[i]["items"]) for i in rises
                   if any(_agree(centres[i], centres[j])
                          for j in by_key.get((groups[i]["key"][0], -1, groups[i]["key"][2]), ())))
    rise_n = sum(len(groups[i]["items"]) for i in rises) or 1
    days = [len({datetime.fromtimestamp(e["t"]).date() for e in g["items"]}) for g in groups]
    return {"edges": sum(len(g["items"]) for g in groups),
            "tightness": sum(n * s for n, s in tight) / sum(n for n, _ in tight) if tight else None,
            "mixed": mixed / total, "mixed_by": {p: v / total for p, v in mixed_by.items()},
            "duplicates": dup / total,
            "groups": len(groups), "groups_min": sum(1 for g in groups if len(g["items"]) >= GROUP_MIN),
            "small_share": sum(len(g["items"]) for g in groups if len(g["items"]) < GROUP_MIN) / total,
            "groups_days": sum(1 for d in days if d >= GROUP_DAYS),
            "mirror": mirrored / rise_n}


def miss(c: dict) -> str:
    """How a parent's step and its children's fail to add up: "missed" - the
    parent read nothing over the children's moment; "opposite" - it stepped
    the other way; "more" - further than they did, a load no meter below reads
    beside them, or another meter's step the size-nearest choice took;
    "less" - not as far."""
    if not c["p"]:
        return "missed"
    if (c["p"] > 0) != (c["c"] > 0):
        return "opposite"
    return "more" if abs(c["p"]) > abs(c["c"]) else "less"


KINDS = ("missed", "opposite", "more", "less")


def conservation(cons: Sequence[dict]) -> Dict[str, list]:
    """Per meter pair "parent>child": [moments whose children's step is clear
    of the meters' idle noises added, of them adding up, the rest by how they fail
    (KINDS), and the noise-level moments, counted apart - see _clear]."""
    out: Dict[str, list] = {}
    for c in cons:
        for ch in c["children"]:
            row = out.setdefault(f"{c['parent']}>{ch}", [0, 0] + [0] * len(KINDS) + [0])
            if not c.get("clear", True):
                row[-1] += 1
                continue
            row[0] += 1
            if c["ok"]:
                row[1] += 1
            else:
                row[2 + KINDS.index(miss(c))] += 1
    return out


def coverage(edges: Sequence[dict]) -> Dict[str, dict]:
    """Per meter: its entries, the share unknown (not read yet), the share 0
    on silence alone (no reading within its latency either side - held, as
    the detector reads a silence), and per place meter the share of its
    edges where a meter above read 0 though the step was clear of its idle
    noise and the place's added (_clear)."""
    out: Dict[str, dict] = {}
    for e in edges:
        for k, v in e["entries"].items():
            row = out.setdefault(k.split("|", 1)[0], {"entries": 0, "unknown": 0, "silent": 0, "edges": 0, "blind": 0})
            row["entries"] += 1
            row["unknown"] += v is None
        for k in e["silent"]:
            out[k.split("|", 1)[0]]["silent"] += 1
        row = out.setdefault(e["place"], {"entries": 0, "unknown": 0, "silent": 0, "edges": 0, "blind": 0})
        row["edges"] += 1
        row["blind"] += bool(e["blind"])
    return out


def brief(e: dict) -> tuple:
    """An edge as invariance compares it: (time, place, direction, pattern,
    its non-zero entries' steps)."""
    return (round(e["t"], 2), e["place"], e["dir"], e["pattern"],
            {k: round(v["dp"], 1) for k, v in sorted(e["entries"].items()) if isinstance(v, dict)})


def compare(a: Sequence[tuple], b: Sequence[tuple], tol_s: float = 2.0) -> List[dict]:
    """Two replays' edges (``brief``): one with no edge of the same place,
    direction and pattern within ``tol_s`` in the other ("only A" / "only
    B"), or one whose non-zero entries differ (another meter, or a step
    more than half a watt apart) - "entries"."""
    out, pool = [], {}
    for k, e in enumerate(b):
        pool.setdefault(e[1:4], []).append(k)
    used = set()
    for e in a:
        cands = [k for k in pool.get(e[1:4], ()) if k not in used and abs(b[k][0] - e[0]) <= tol_s]
        if not cands:
            out.append({"kind": "only A", "a": e, "b": None})
            continue
        k = min(cands, key=lambda k: abs(b[k][0] - e[0]))
        used.add(k)
        ea, eb = e[4], b[k][4]
        if set(ea) != set(eb) or any(abs(ea[x] - eb[x]) > 0.5 for x in ea):
            out.append({"kind": "entries", "a": e, "b": b[k]})
    out += [{"kind": "only B", "a": None, "b": b[k]} for k in range(len(b)) if k not in used]
    return out
