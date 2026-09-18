"""Load detection on raw per-phase power. Pure, incremental, persistable.

The unit is the SESSION: one load, from the step up that started it to the
step down that matched it. Steps, not excursions - on a house main the power
never returns to its idle floor while anything else is running, and waiting
for that produced 100-hour "loads" of hundreds of kWh. Other loads may come
and go in between; the pairing is by size, most recent first. Sessions that
start and end together on several phases are one multi-phase session - a
two-phase kiln is 3 kW on A and 3 kW on C, and nothing else has that shape.

Closed sessions are matched to SIGNATURES: phase set, dominant power per
phase (within ~10 % or the noise), duration within a factor, power factor
when known. No match makes a new signature. Signatures carry counts, typical
duration and repeat interval, and an hour-of-day histogram - the material
the naming page describes them with and the forecast will later schedule.

Everything here works sample by sample with a small persisted state per
phase, so the recorder can be read in slices and the detector resumed from
where it stopped. Timestamps are epoch seconds; powers are watts.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .classify import Guess, classify

PHASES = ("a", "b", "c")
WEEK_SECONDS = 7 * 24 * 3600.0
MIN_NOISE_W = 100.0            # never call a change smaller than this a transition
NOISE_MAD_FACTOR = 4.0
SUSTAIN_SAMPLES = 2            # a level change must hold this many samples...
SUSTAIN_SECONDS = 5.0          # ...and at least this long
BASELINE_EMA = 0.02            # idle baseline drifts slowly
BASELINE_SEED_SAMPLES = 24     # two minutes at 5 s; the seed takes a LOW percentile, not the median,
BASELINE_SEED_PERCENTILE = 0.25  # so a window that begins mid-load does not call the load the floor
SLOW_FOLLOW = 0.02
# how many idle samples the pre-step reactive median is taken over
Q_RECENT_SAMPLES = 8             # the held level follows drift this fast, so a ramp is never a step
# A cloud IS a step at the meter, and a big one. It is the sun switching,
# not a load, and it gives itself away by moving PV the opposite way at the
# same moment. The share is a RANGE because the array's reading may be this
# phase's own (share about 1) or the inverter's total (about a third of it
# on each phase), and three phases are never quite balanced either.
PV_SHARE_MIN = 0.25
PV_SHARE_MAX = 1.25
PV_MIN_SWING_W = 200.0         # below this the sun hardly moved and there is nothing to tell
PV_MIN_SAMPLES = 30
# A reading that CONTAINS generation goes below zero whenever the site
# exports; one that carries the house alone cannot. Both thresholds are
# deliberately slack: a meter's noise sits well inside 50 W, and half a per
# cent of a day is minutes, not a spike.
EXPORT_FLOOR_W = 50.0
EXPORT_SHARE = 0.005
# Below this an AC input is carrying nothing; a generator sits here almost
# always, while a utility connection crosses zero and moves on.
SOURCE_IDLE_W = 25.0
SOURCE_IDLE_SHARE = 0.9
# a reference circuit has to carry something this often to be one
LIVE_SHARE = 0.2
SOURCE_UTILITY = "utility"
SOURCE_GENERATOR = "generator"
SOURCE_NONE = "none"
MATCH_EDGE_REL = 0.15          # a step down pairs with a step up this close in size, or the noise
MAX_OPEN_S = 24 * 3600.0       # a start whose stop never came is given up on after this
MAX_OPEN_EDGES = 12            # loads believed to be running at once on one phase
MERGE_TOLERANCE_S = 15.0       # sessions on different phases this close in start and end are one
NOISE_SESSION_WH = 3.0         # a blip smaller than this AND shorter than NOISE_SESSION_S is dropped
NOISE_SESSION_S = 20.0
MATCH_POWER_REL = 0.10
MATCH_DURATION_FACTOR = 3.0
MATCH_PF_TOL = 0.15
MAX_SIGNATURES = 200
MAX_RECENT_SESSIONS = 200
HELD_TAIL_S = 60.0             # closed sessions wait this long for a partner on another phase


def _median(xs: Sequence[float]) -> float:
    return statistics.median(xs) if xs else 0.0


# ------------------------------------------------------------------ sessions
@dataclass
class Session:
    phases: str                          # "a", "ac", ...
    start: float
    end: float
    levels: Dict[str, List[Tuple[float, float]]]   # phase -> [(since_ts, watts above baseline)]
    pf: Optional[float] = None           # mean power factor during the session, if known

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    def power_by_phase(self) -> Dict[str, float]:
        """Energy-weighted mean watts per phase - the dominant level."""
        out = {}
        for ph, lv in self.levels.items():
            e = 0.0
            for i, (since, w) in enumerate(lv):
                until = lv[i + 1][0] if i + 1 < len(lv) else self.end
                e += w * max(0.0, until - since)
            out[ph] = e / self.duration_s if self.duration_s > 0 else 0.0
        return out

    @property
    def energy_wh(self) -> float:
        return sum(p * self.duration_s for p in self.power_by_phase().values()) / 3600.0

    @property
    def max_w(self) -> float:
        return sum(max((w for _, w in lv), default=0.0) for lv in self.levels.values())

    @property
    def level_count(self) -> int:
        return max((len(lv) for lv in self.levels.values()), default=0)

    def to_dict(self) -> dict:
        return {"phases": self.phases, "start": self.start, "end": self.end, "pf": self.pf,
                "levels": {ph: [list(x) for x in lv] for ph, lv in self.levels.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(phases=d["phases"], start=d["start"], end=d["end"], pf=d.get("pf"),
                   levels={ph: [tuple(x) for x in lv] for ph, lv in d["levels"].items()})


# ------------------------------------------------------------------ per-phase tracker
def _is_the_sun(step: float, pv_step: Optional[float]) -> bool:
    """Is this step at the meter just PV moving the other way?

    A grid meter carries the house MINUS the array, so a cloud arrives on it
    as a load switching on and clears as one switching off. A step that a
    simultaneous PV step of the opposite sign accounts for is not a load.
    Where the reading already excludes PV nothing fires here, because there
    is no step to explain."""
    if pv_step is None or step * pv_step >= 0:
        return False
    share = abs(step) / abs(pv_step)
    return PV_SHARE_MIN <= share <= PV_SHARE_MAX


def delta_correlation(samples: Sequence[Tuple[float, float]],
                      other_by_ts: Dict[float, float],
                      min_swing_w: float = PV_MIN_SWING_W) -> Optional[float]:
    """How the two series' CHANGES move together, between -1 and 1.

    Levels would say almost nothing - two readings of one site are both
    large and both wander - while their changes say exactly how one responds
    to the other. None when the second series hardly moved."""
    xs: List[float] = []
    ys: List[float] = []
    prev: Optional[Tuple[float, float]] = None
    for ts, w in samples:
        p = other_by_ts.get(ts)
        if p is None:
            continue
        if prev is not None:
            xs.append(w - prev[0])
            ys.append(p - prev[1])
        prev = (w, p)
    if len(xs) < PV_MIN_SAMPLES or (max(ys) - min(ys)) < min_swing_w:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def carries_generation(rows: Sequence[Tuple[float, float]]) -> Optional[bool]:
    """Does this reading contain the site's generation, or the house alone?

    It decides two things at once - whether a cloud can masquerade as a load
    in this signal, and whether the inverter's output has to be added back
    to get what the house draws - and it is answered by physics rather than
    statistics: a reading that includes generation goes BELOW ZERO whenever
    the site exports, and one that carries only the house cannot.

    I tried correlating the two series' changes first, and Anze's own data
    threw it out (2026-09-18): on a day when the grid meter exported 4.5 kW
    the correlation called the array absent from it. Sampling the two series
    at different instants is enough to destroy that signal, while "did it go
    negative" survives anything.

    None when there is too little to look at."""
    if len(rows) < PV_MIN_SAMPLES:
        return None
    below = sum(1 for _, value in rows if value < -EXPORT_FLOOR_W)
    return below >= EXPORT_SHARE * len(rows)


def exports_positive(grid_rows: Sequence[Tuple[float, float]],
                     generation: Sequence[Tuple[float, float]]) -> Optional[bool]:
    """Which way round is this grid meter wired?

    "House = the meter plus the inverter" holds only in the dashboard's
    convention, where importing is positive. Anze's SolarEdge M1 is the
    other way round - verified on a real day, 2177 of 2177 samples positive
    while the array was over 12 kW - and summing it would have counted the
    array twice instead of cancelling it.

    Nobody should have to know this about their own meter, and the data
    says it outright: when generation is at its peak the site is exporting,
    so whichever sign the meter shows THEN is its export sign.

    Only meaningful for a reading that exports at all, so it answers None
    unless the reading goes both ways - a site that never exports has no
    export sign to find, and the sum is unaffected either way."""
    if carries_generation(grid_rows) is not True:
        return None
    if len(generation) < PV_MIN_SAMPLES:
        return None
    peak = max(value for _, value in generation)
    if peak <= 0:
        return None
    busy = {ts for ts, value in generation if value >= 0.8 * peak}
    if len(busy) < PV_MIN_SAMPLES:
        return None
    # the grid reading as of each of those moments, sample and hold
    ordered, i, seen = sorted(generation), 0, []
    at = sorted(busy)
    rows = sorted(grid_rows)
    j = 0
    for ts in at:
        while j + 1 < len(rows) and rows[j + 1][0] <= ts:
            j += 1
        if rows and rows[0][0] <= ts:
            seen.append(rows[j][1])
    if len(seen) < PV_MIN_SAMPLES:
        return None
    return statistics.median(seen) > 0


def carries_load(rows: Sequence[Tuple[float, float]]) -> bool:
    """Is this reading actually carrying power, or is it a dead port?

    Kozolec's MultiPlus AC input publishes power, current AND voltage - a
    perfectly coherent triple, and all three sit at zero because the
    generator behind them is off. Coherence is necessary and not sufficient:
    a reference for reactive power has to be a circuit something flows
    through (Anze, 2026-09-18)."""
    if len(rows) < PV_MIN_SAMPLES:
        return False
    live = sum(1 for _, value in rows if abs(value) > SOURCE_IDLE_W)
    return live >= LIVE_SHARE * len(rows)


def classify_source(rows: Sequence[Tuple[float, float]]) -> Optional[str]:
    """What is behind an AC input, from the reading alone.

    The reading cannot tell a utility meter from a generator's - both are
    watts at an input port - but the BEHAVIOUR separates them, and neither
    is a preference anyone should have to type (Anze, 2026-09-18):

      * only a utility ABSORBS a surplus, so a reading that goes usefully
        negative is the grid and nothing else;
      * a generator is off far more than it is on, so a source that spends
        almost all its life at zero and never absorbs is one;
      * a port that has never carried anything at all is, as far as the data
        goes, not connected.

    The last two are the same reading for a generator that has not run in
    the window, which is the one case the setting is worth overriding for -
    "you will need the generator" and "you will go dark" are not the same
    warning. Callers should keep the most informative verdict they have seen
    rather than following this down to ``none`` again.

    None when there is too little to look at."""
    if len(rows) < PV_MIN_SAMPLES:
        return None
    if any(value < -EXPORT_FLOOR_W for _, value in rows):
        return SOURCE_UTILITY
    live = sum(1 for _, value in rows if abs(value) > SOURCE_IDLE_W)
    if not live:
        return SOURCE_NONE
    return SOURCE_GENERATOR if live <= (1.0 - SOURCE_IDLE_SHARE) * len(rows) else SOURCE_UTILITY


def _pf_from(watts: float, var: Optional[float]) -> Optional[float]:
    """The LOAD's power factor, from its OWN step in real and reactive power.

    The meter's power factor is the whole site's and says nothing about the
    load that just started (Anze, 2026-09-17). Watts and VAr add across loads,
    a ratio does not, so the load's factor comes from how much each of them
    moved when it switched - not from how the meter's factor read.
    """
    if var is None:
        return None
    s = math.hypot(watts, var)
    return None if s <= 0 else max(0.0, min(1.0, abs(watts) / s))


@dataclass
class _Open:
    """A load believed to be running: the step that started it, what is still
    running of it, and every level it has held."""
    since: float
    watts: float
    var: Optional[float]                          # the reactive step it started with
    levels: List[Tuple[float, float]] = field(default_factory=list)

    def as_list(self) -> list:
        return [self.since, self.watts, self.var, [list(x) for x in self.levels]]

    @classmethod
    def of(cls, raw) -> "_Open":
        since, watts, var = raw[0], raw[1], raw[2]
        levels = [tuple(x) for x in (raw[3] if len(raw) > 3 else [])] or [(since, watts)]
        return cls(since=since, watts=watts, var=var, levels=levels)


@dataclass
class PhaseState:
    """Edges, not excursions.

    A load is a STEP: the phase rises by its power when it starts and falls
    by the same amount when it stops. Waiting instead for the meter to come
    back to its idle floor - the first design - only works on a phase that
    goes quiet between loads, and a house main never does: the kettle starts
    while the fridge is running, so the excursion that opened at breakfast
    closed at midnight. Anze's home meter proved it (2026-09-17): 76
    signatures from 160 sessions, 61 % of them seen once, the longest single
    "load" 155 hours and 778 kWh, and four with NEGATIVE power because the
    sun pushed the grid meter below its own floor.

    So each sustained step is recorded as an edge, and a step DOWN is paired
    with the open step UP that best matches its size, most recent first. That
    is what survives loads overlapping: A on, B on, B off, A off pairs
    correctly. A slow drift - sunrise on a grid meter, an element tapering -
    never becomes a step at all, because the tracked level follows it.
    """
    baseline: Optional[float] = None
    noise: float = MIN_NOISE_W
    level: Optional[float] = None                # what the phase is holding now
    q_level: Optional[float] = None              # reactive VAr at that level, when known
    # The reactive power just BEFORE a step, as a short median rather than
    # the slow EMA q_level is. Watts drift slowly while nothing switches, so
    # an EMA tracks them; the grid meter's VAr does not - at home it swings
    # from 259 var at night to 2447 at midday with the inverter's voltage
    # support, and a 0.02 EMA lags that badly enough to swamp a load's own
    # step. Measured on the kiln: 0.934 from the EMA, 0.989 from the median,
    # and the classifier's heater band starts at 0.93 (Anze, 2026-09-18).
    q_recent: List[float] = field(default_factory=list)
    pv_level: Optional[float] = None             # what the array was making then
    # True where this reading is the house alone, which cannot go below
    # zero. Anze's per-phase template dips negative for 49 samples out of
    # 20764 - the moments its own inputs do not line up - and one of those
    # dragged the idle floor to -569 W for the rest of the day, so every
    # step after it was measured from nonsense (2026-09-18).
    floor_zero: bool = False
    seed: List[float] = field(default_factory=list)
    idle_diffs: List[float] = field(default_factory=list)
    pending: List[Tuple[float, float, Optional[float], Optional[float]]] = field(default_factory=list)
    open_edges: List[_Open] = field(default_factory=list)   # believed to be running
    last_ts: Optional[float] = None

    def process(self, ts: float, w: float, q: Optional[float] = None,
                pv: Optional[float] = None) -> List[Session]:
        """One sample: seconds, watts, reactive VAr where the meter gives
        enough to work it out, and what the array was making at the time.
        Returns the sessions this sample closed - more than one when several
        loads stopped together."""
        if self.last_ts is not None and ts <= self.last_ts:
            return []
        self.last_ts = ts
        if self.baseline is None:
            self.seed.append(w)
            if len(self.seed) >= BASELINE_SEED_SAMPLES:
                ordered = sorted(self.seed)
                self.baseline = ordered[int(BASELINE_SEED_PERCENTILE * (len(ordered) - 1))]
                if self.floor_zero:
                    self.baseline = max(self.baseline, 0.0)
                near = [x for x in ordered if x - self.baseline < 2 * MIN_NOISE_W]
                diffs = [abs(x - self.baseline) for x in near] or [0.0]
                self.noise = max(MIN_NOISE_W, NOISE_MAD_FACTOR * _median(diffs))
                self.level = self.baseline
                self.q_level = q
                self.pv_level = pv
                self.seed = []
            return []

        if self.open_edges and ts - self.open_edges[0].since > MAX_OPEN_S:
            # a start whose stop was never seen: give up rather than pair it
            # with an unrelated load hours later
            self.open_edges = [e for e in self.open_edges if ts - e.since <= MAX_OPEN_S]

        if abs(w - self.level) < self.noise:
            self.pending = []
            # no step - follow the drift, so a ramp never becomes a load
            self.level += SLOW_FOLLOW * (w - self.level)
            if q is not None:
                self.q_level = q if self.q_level is None else self.q_level + SLOW_FOLLOW * (q - self.q_level)
                self.q_recent.append(q)
                del self.q_recent[:-Q_RECENT_SAMPLES]
            if pv is not None:
                self.pv_level = pv if self.pv_level is None else self.pv_level + SLOW_FOLLOW * (pv - self.pv_level)
            if not self.open_edges:
                self.baseline += BASELINE_EMA * (w - self.baseline)
                if self.floor_zero:
                    self.baseline = max(self.baseline, 0.0)
                self.level = self.baseline
                self.idle_diffs.append(abs(w - self.baseline))
                if len(self.idle_diffs) >= 240:
                    self.noise = max(MIN_NOISE_W, NOISE_MAD_FACTOR * _median(self.idle_diffs))
                    self.idle_diffs = self.idle_diffs[-120:]
            return []

        self.pending.append((ts, w, q, pv))
        if len(self.pending) < SUSTAIN_SAMPLES or (ts - self.pending[0][0]) < SUSTAIN_SECONDS:
            return []
        new_level = _median([x for _, x, _, _ in self.pending])
        known_q = [x for _, _, x, _ in self.pending if x is not None]
        known_pv = [x for _, _, _, x in self.pending if x is not None]
        new_q = _median(known_q) if known_q else None
        new_pv = _median(known_pv) if known_pv else None
        since = self.pending[0][0]
        self.pending = []
        step = new_level - self.level
        # the level it stepped FROM, measured over the samples just before
        # rather than followed, so a fast-drifting reactive signal does not
        # leak its drift into the load's own step
        was_q = _median(self.q_recent) if self.q_recent else self.q_level
        step_q = None if (new_q is None or was_q is None) else new_q - was_q
        pv_step = None if (new_pv is None or self.pv_level is None) else new_pv - self.pv_level
        self.level = new_level
        if new_q is not None:
            self.q_level = new_q
            self.q_recent = [new_q]
        if new_pv is not None:
            self.pv_level = new_pv
        if _is_the_sun(step, pv_step):
            return []
        if step > 0:
            self.open_edges.append(_Open(since, step, step_q, [(since, step)]))
            if len(self.open_edges) > MAX_OPEN_EDGES:
                self.open_edges.pop(0)
            return []
        closed = self._pair(since, -step, None if step_q is None else -step_q, new_level)
        if self.open_edges and new_level <= self.baseline + self.noise:
            # back at the idle floor, so whatever was still open has stopped
            # without us seeing it go. Holding those starts open would have
            # them pair with an unrelated load hours later, and meanwhile
            # count as running.
            self.open_edges = []
        return closed

    def _tol(self, a: float, b: float) -> float:
        return max(self.noise, MATCH_EDGE_REL * max(a, b))

    def _pair(self, at: float, watts: float, var: Optional[float],
              new_level: float) -> List[Session]:
        """What a step down means, most recent load first.

        Either a load STOPPED, in which case the step undoes its own step up
        and the session closes; or a load STEPPED DOWN to a lower level and
        is still running - a washer leaving its heater for its motor - in
        which case the level is recorded and the session stays open. Anything
        else is a stop we never saw start, and is dropped rather than pinned
        on an unrelated load."""
        for i in range(len(self.open_edges) - 1, -1, -1):
            o = self.open_edges[i]
            if abs(o.watts - watts) <= self._tol(o.watts, watts):
                self.open_edges.pop(i)
                return [self._close(o, at, watts, var)]
        for i in range(len(self.open_edges) - 1, -1, -1):
            o = self.open_edges[i]
            if o.watts - watts > self._tol(o.watts, watts):
                o.watts -= watts
                o.levels.append((at, o.watts))
                return []
        # several loads going together - the oven and its fan, a programme
        # ending - leave one step too big for any of them alone. Take them
        # largest first while the step still covers them, or nothing.
        order = sorted(range(len(self.open_edges)), key=lambda i: -self.open_edges[i].watts)
        taken, left = [], watts
        for i in order:
            o = self.open_edges[i]
            if o.watts <= left + self._tol(o.watts, left):
                taken.append(i)
                left -= o.watts
                if left <= self.noise:
                    break
        if len(taken) >= 2 and abs(left) <= self._tol(watts, watts):
            out = []
            for i in sorted(taken, reverse=True):
                o = self.open_edges.pop(i)
                out.append(self._close(o, at, o.watts, None))
            return sorted(out, key=lambda x: x.start)
        if not self.open_edges:
            # nothing was running: the floor itself moved
            self.baseline = max(new_level, 0.0) if self.floor_zero else new_level
        return []

    def _close(self, o: _Open, at: float, watts: float, var: Optional[float]) -> Session:
        levels = list(o.levels)
        if len(levels) == 1:
            # one level throughout: both steps measure the same load, so
            # average them, and its power factor with them
            levels = [(o.since, 0.5 * (levels[0][1] + watts))]
            known = [abs(x) for x in (o.var, var) if x is not None]
            q = sum(known) / len(known) if known else None
        else:
            q = o.var                         # the factor of the level it started at
        return Session(phases="", start=o.since, end=at, levels={"": levels},
                       pf=_pf_from(levels[0][1], q))

    def active(self, now_ts: float) -> Optional[Tuple[float, float]]:
        """(since, watts) of everything believed to be running on this phase."""
        if not self.open_edges:
            return None
        return min(o.since for o in self.open_edges), sum(o.watts for o in self.open_edges)

    def to_dict(self) -> dict:
        return {"baseline": self.baseline, "noise": self.noise, "level": self.level,
                "q_level": self.q_level, "q_recent": list(self.q_recent), "pv_level": self.pv_level, "seed": self.seed,
                "idle_diffs": self.idle_diffs[-120:], "pending": [list(x) for x in self.pending],
                "open_edges": [o.as_list() for o in self.open_edges], "last_ts": self.last_ts}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PhaseState":
        if not d:
            return cls()
        return cls(baseline=d.get("baseline"), noise=d.get("noise", MIN_NOISE_W), level=d.get("level"),
                   q_level=d.get("q_level"), q_recent=list(d.get("q_recent") or []), pv_level=d.get("pv_level"), seed=list(d.get("seed") or []),
                   idle_diffs=list(d.get("idle_diffs") or []),
                   pending=[tuple(list(x) + [None] * (4 - len(x))) for x in d.get("pending") or []],
                   open_edges=[_Open.of(x) for x in d.get("open_edges") or []], last_ts=d.get("last_ts"))


# ------------------------------------------------------------------ signatures
@dataclass
class Signature:
    id: int
    phases: str
    power: Dict[str, float]              # running mean watts per phase
    duration_s: float                    # running mean
    pf: Optional[float]
    count: int
    first_seen: float
    last_seen: float
    interval_s: Optional[float] = None   # running mean START-to-start spacing ("every 5 min")
    last_start: Optional[float] = None
    locations: Dict[str, int] = field(default_factory=dict)   # submeter name -> sessions also seen there

    @property
    def location(self) -> str:
        """The meter that saw most of this signature's sessions, or "main"
        when none did. ``most_specific`` refines this with the hierarchy."""
        if not self.locations:
            return "main"
        name, n = max(self.locations.items(), key=lambda kv: kv[1])
        return name if n * 2 >= self.count else "main"
    # Where this load's ENERGY goes, in watt-hours, by hour of day and by
    # weekday (Monday first). Counting starts answered "how often"; what it
    # actually costs on a Saturday is its runtime times its draw, and that
    # is the thing worth looking at (Anze, 2026-09-17).
    hour_wh: List[float] = field(default_factory=lambda: [0.0] * 24)
    day_wh: List[float] = field(default_factory=lambda: [0.0] * 7)
    level_count: float = 1.0
    name: Optional[str] = None
    # how much each reading WANDERS between sightings, as a running mean
    # absolute deviation. A load that repeats to within a few per cent is a
    # real device; one whose power and duration are all over the place is
    # the detector pairing unrelated edges, and the evidence score says so.
    power_mad: float = 0.0
    duration_mad: float = 0.0
    interval_mad: Optional[float] = None

    def matches(self, s: Session, noise_w: float) -> Optional[float]:
        """A score in (0, 1] when ``s`` fits, None when it does not."""
        if s.phases != self.phases:
            return None
        pw = s.power_by_phase()
        score = 1.0
        for ph in self.phases:
            mine, theirs = self.power.get(ph, 0.0), pw.get(ph, 0.0)
            tol = max(MATCH_POWER_REL * max(mine, theirs), noise_w)
            if abs(mine - theirs) > tol:
                return None
            score *= 1.0 - abs(mine - theirs) / (2 * tol)
        ratio = max(s.duration_s, 1.0) / max(self.duration_s, 1.0)
        if ratio > MATCH_DURATION_FACTOR or ratio < 1.0 / MATCH_DURATION_FACTOR:
            return None
        if self.pf is not None and s.pf is not None and abs(self.pf - s.pf) > MATCH_PF_TOL:
            return None
        return score

    def alike(self, other: "Signature", noise_w: float) -> bool:
        """Would these two be the same signature if they arrived now?

        The same test as ``matches``, between two signatures rather than a
        signature and a session."""
        if self.phases != other.phases or self is other:
            return False
        if self.name and other.name and self.name != other.name:
            return False                       # named apart on purpose
        for ph in self.phases:
            mine, theirs = self.power.get(ph, 0.0), other.power.get(ph, 0.0)
            if abs(mine - theirs) > max(MATCH_POWER_REL * max(mine, theirs), noise_w):
                return False
        ratio = max(other.duration_s, 1.0) / max(self.duration_s, 1.0)
        if ratio > MATCH_DURATION_FACTOR or ratio < 1.0 / MATCH_DURATION_FACTOR:
            return False
        if self.pf is not None and other.pf is not None and abs(self.pf - other.pf) > MATCH_PF_TOL:
            return False
        return True

    def swallow(self, other: "Signature") -> None:
        """Take another signature's sightings into this one, by weight."""
        a, b = self.count, other.count
        n = a + b
        if n <= 0:
            return
        for ph in set(self.power) | set(other.power):
            self.power[ph] = (self.power.get(ph, 0.0) * a + other.power.get(ph, 0.0) * b) / n
        self.duration_s = (self.duration_s * a + other.duration_s * b) / n
        self.level_count = (self.level_count * a + other.level_count * b) / n
        self.power_mad = (self.power_mad * a + other.power_mad * b) / n
        self.duration_mad = (self.duration_mad * a + other.duration_mad * b) / n
        if self.pf is None:
            self.pf = other.pf
        elif other.pf is not None:
            self.pf = (self.pf * a + other.pf * b) / n
        if other.interval_s is not None and (self.interval_s is None or b > a):
            self.interval_s, self.interval_mad = other.interval_s, other.interval_mad
        self.hour_wh = [x + y for x, y in zip(self.hour_wh, other.hour_wh)]
        self.day_wh = [x + y for x, y in zip(self.day_wh, other.day_wh)]
        for name, k in other.locations.items():
            self.locations[name] = self.locations.get(name, 0) + k
        self.first_seen = min(self.first_seen, other.first_seen)
        self.last_seen = max(self.last_seen, other.last_seen)
        if other.last_start is not None:
            self.last_start = max(self.last_start or 0.0, other.last_start)
        self.name = self.name or other.name
        self.count = n

    def absorb(self, s: Session, tz) -> None:
        n = self.count
        pw = s.power_by_phase()
        self.power_mad = (self.power_mad * n + abs(sum(pw.values()) - sum(self.power.values()))) / (n + 1)
        self.duration_mad = (self.duration_mad * n + abs(s.duration_s - self.duration_s)) / (n + 1)
        for ph, w in s.power_by_phase().items():
            self.power[ph] = (self.power.get(ph, w) * n + w) / (n + 1)
        self.duration_s = (self.duration_s * n + s.duration_s) / (n + 1)
        self.level_count = (self.level_count * n + s.level_count) / (n + 1)
        if s.pf is not None:
            self.pf = s.pf if self.pf is None else (self.pf * n + s.pf) / (n + 1)
        if self.last_start is not None:
            gap = s.start - self.last_start
            if gap > 0:
                if self.interval_s is not None:
                    self.interval_mad = (abs(gap - self.interval_s) if self.interval_mad is None
                                         else 0.7 * self.interval_mad + 0.3 * abs(gap - self.interval_s))
                self.interval_s = gap if self.interval_s is None else 0.7 * self.interval_s + 0.3 * gap
        self.last_start = s.start
        self.last_seen = max(self.last_seen, s.end)
        self._spread(s, tz)
        self.count += 1

    @property
    def watts(self) -> float:
        return sum(self.power.values())

    @property
    def energy_wh(self) -> float:
        """What this load has actually used over everything seen of it."""
        return sum(self.hour_wh)

    @property
    def weekly_wh(self) -> float:
        """At the rate it has been going, what it costs in a week.

        Never extrapolated from less than a day: a load first seen an hour
        ago would otherwise claim a hundred and sixty-eight times its own
        energy."""
        span = max(self.last_seen - self.first_seen, 1.0)
        return self.energy_wh * min(WEEK_SECONDS / span, 7.0)

    @property
    def per_run_wh(self) -> float:
        return self.energy_wh / max(self.count, 1)

    def row(self, tz) -> str:
        """One line for a menu, and a MENU ROW IS NARROW - it truncated at
        about sixty characters and took the useful half with it (Anze,
        2026-09-18). So: what it draws, what it costs a week and a run, how
        long it runs, how often, one word for what it might be, and the week
        itself in seven characters."""
        phases = "+".join(p.upper() for p in self.phases)
        bits = [f"{self.watts / 1000:.1f} kW ({phases})",
                f"{_fmt_wh(self.weekly_wh)}/{_fmt_wh(self.per_run_wh)}",
                _fmt_s(self.duration_s)]
        # how OFTEN is left to the load's own page: of everything here it is
        # the least use for telling one row from another
        tag = self.guess().tag
        if tag:
            bits.append(tag)
        line = ", ".join(bits)
        bars = sparkline(self.day_wh)          # seven characters, one per day
        return f"{line} {bars}" if bars else line

    @property
    def evidence(self) -> float:
        """How sure we are this is a REAL repeating load rather than a pair
        of unrelated edges: how often it has been seen, and how tightly its
        power and duration repeat. This is the score that decides whether it
        is worth putting in front of anyone."""
        seen = min(1.0, (self.count - 1) / 4.0)          # five sightings is plenty
        if self.count < 3:
            return round(0.4 * seen, 2)                  # nothing has repeated enough to measure
        tight_w = 1.0 - min(1.0, (self.power_mad / max(abs(self.watts), 1.0)) / 0.15)
        tight_d = 1.0 - min(1.0, (self.duration_mad / max(self.duration_s, 1.0)) / 0.5)
        return round(0.5 * seen + 0.3 * tight_w + 0.2 * tight_d, 2)

    @property
    def regular(self) -> bool:
        """It comes back on a clock - a thermostat, a timer, a fridge."""
        return (self.count >= 4 and self.interval_s is not None and self.interval_mad is not None
                and self.interval_mad < 0.35 * self.interval_s)

    def guess(self) -> Guess:
        """What KIND of thing this might be. Never a claim - see classify."""
        return classify(self.watts, self.pf, self.level_count, self.duration_s)

    def _spread(self, s: Session, tz) -> None:
        """Put a session's energy into every hour and day it occupied.

        A run from 23:50 to 03:00 belongs to four hours and two days, not to
        the one it began in."""
        watts = sum(s.power_by_phase().values())
        if watts <= 0 or s.end <= s.start:
            return
        t = s.start
        while t < s.end:
            moment = datetime.fromtimestamp(t, tz)
            into = moment.minute * 60 + moment.second + moment.microsecond / 1e6
            step = min(s.end - t, max(3600.0 - into, 1.0))
            wh = watts * step / 3600.0
            self.hour_wh[moment.hour] += wh
            self.day_wh[moment.weekday()] += wh
            t += step

    def describe(self, tz) -> str:
        """Words for the naming page: '6.1 kW on A+C, ~80 s, every 3 min, seen
        258 times - maybe a heating element (power factor 1.00, one level)'."""
        phases = "+".join(p.upper() for p in self.phases)
        dur = _fmt_s(self.duration_s)
        gap = f", every {_fmt_s(self.interval_s)}" if self.interval_s else ""
        lvl = f", {round(self.level_count)} levels" if self.level_count >= 1.5 else ""
        pf = f", PF {self.pf:.2f}" if self.pf is not None else ""
        span = max(self.last_seen - self.first_seen, 0.0)
        over = f" over {_fmt_s(span)}" if span > 0 else ""
        line = (f"{self.watts / 1000:.1f} kW on {phases}, ~{dur}{gap}{lvl}{pf}, "
                f"seen {self.count} times{over}")
        guess = self.guess()
        # the short form: the line above already carries the factor, the
        # levels and the size the guess rests on
        return f"{line} - {guess.short}" if guess.kind else line

    def detail(self, tz, parents: Optional[Dict[str, Optional[str]]] = None) -> str:
        """Markdown for the naming form, once one is picked: what it is, when
        it runs, where it is, and how sure each of those is."""
        lines = [f"**{self.describe(tz)}**", ""]
        bars = hour_histogram(self.hour_wh)
        if bars:
            lines += [f"Where its energy goes, by hour of day - tallest bar "
                      f"{_fmt_wh(max(self.hour_wh))} of {_fmt_wh(sum(self.hour_wh))}:",
                      "", "```", *bars, "```", ""]
        week = day_histogram(self.day_wh)
        if week:
            # every chart is scaled to its own tallest bar, so without this
            # you cannot tell one sighting from twenty (Anze, 2026-09-17)
            lines += [f"And by day of week - tallest bar {_fmt_wh(max(self.day_wh))} "
                      f"of {_fmt_wh(sum(self.day_wh))}:", "", "```", *week, "```", ""]
        guess = self.guess()
        if guess.kind:
            both = guess.kind if not guess.alternative else f"{guess.kind} or {guess.alternative}"
            lines.append(f"Looks like {both} - {', '.join(guess.because)} "
                         f"(confidence {guess.confidence:.2f}).")
        elif guess.because:
            lines.append(guess.because[0].capitalize() + ".")
        lines.append(f"Where: {describe_location(self.locations, self.count, parents, self.phases)}.")
        clock = " It comes back on a clock." if self.regular else ""
        lines.append(f"Confidence that this is a real repeating load: {self.evidence:.2f}.{clock}")
        if tz is not None and self.last_seen > self.first_seen:
            first = datetime.fromtimestamp(self.first_seen, tz)
            last = datetime.fromtimestamp(self.last_seen, tz)
            lines.append(f"Seen {self.count} times between {first:%a %d %b %H:%M} "
                         f"and {last:%a %d %b %H:%M}.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"id": self.id, "phases": self.phases, "power": self.power, "duration_s": self.duration_s,
                "pf": self.pf, "count": self.count, "first_seen": self.first_seen, "last_seen": self.last_seen,
                "interval_s": self.interval_s, "hour_wh": self.hour_wh, "day_wh": self.day_wh,
                "level_count": self.level_count, "name": self.name,
                "last_start": self.last_start, "locations": self.locations, "power_mad": self.power_mad,
                "duration_mad": self.duration_mad, "interval_mad": self.interval_mad}

    @classmethod
    def from_dict(cls, d: dict) -> "Signature":
        return cls(id=d["id"], phases=d["phases"], power=dict(d["power"]), duration_s=d["duration_s"], pf=d.get("pf"),
                   count=d["count"], first_seen=d["first_seen"], last_seen=d["last_seen"], interval_s=d.get("interval_s"),
                   hour_wh=list(d.get("hour_wh") or [0.0] * 24),
                   day_wh=list(d.get("day_wh") or [0.0] * 7),
                   level_count=d.get("level_count", 1.0), name=d.get("name"),
                   last_start=d.get("last_start"), locations=dict(d.get("locations") or {}),
                   power_mad=d.get("power_mad", 0.0), duration_mad=d.get("duration_mad", 0.0),
                   interval_mad=d.get("interval_mad"))


def _fmt_wh(x: float) -> str:
    return f"{x:.0f} Wh" if x < 1000 else f"{x / 1000:.1f} kWh"


def _fmt_s(x: Optional[float]) -> str:
    if x is None:
        return "?"
    if x < 90:
        return f"{x:.0f} s"
    if x < 5400:
        return f"{x / 60:.0f} min"
    if x < 172800:                      # past two days, hours stop being readable
        return f"{x / 3600:.1f} h"
    return f"{x / 86400:.1f} days"


# ------------------------------------------------------------------ the detector
@dataclass
class Detector:
    phases: Dict[str, PhaseState] = field(default_factory=lambda: {p: PhaseState() for p in PHASES})
    held: List[Session] = field(default_factory=list)          # closed, waiting for a partner phase
    signatures: List[Signature] = field(default_factory=list)
    recent: List[dict] = field(default_factory=list)           # last sessions with their signature id
    next_id: int = 1
    tz_offset_s: float = 0.0

    # ------------------------------------------------ ingest
    def process(self, samples: Dict[str, Sequence[Tuple[float, float]]],
                q: Optional[Dict[str, Dict[float, float]]] = None, now_ts: Optional[float] = None,
                pv: Optional[Dict[str, Dict[float, float]]] = None) -> List[Session]:
        """Feed new (ts, watts) samples per phase, in time order per phase.
        ``q`` is reactive VAr keyed by the SAME timestamps, where the meter
        gives enough to work it out. Returns the sessions this batch closed."""
        closed: List[Session] = []
        latest = now_ts or 0.0
        for ph, rows in samples.items():
            if ph not in self.phases:
                continue
            st = self.phases[ph]
            qm = (q or {}).get(ph) or {}
            pvm = (pv or {}).get(ph) or {}
            for ts, w in rows:
                latest = max(latest, ts)
                for s in st.process(ts, w, qm.get(ts), pvm.get(ts)):
                    s.phases = ph
                    s.levels = {ph: s.levels.pop("")}
                    closed.append(s)
        return self._merge_and_file(closed, latest)

    def _merge_and_file(self, closed: List[Session], latest: float) -> List[Session]:
        pool = self.held + closed
        pool.sort(key=lambda s: s.start)
        groups: List[List[Session]] = []
        for s in pool:
            for g in groups:
                if (abs(g[0].start - s.start) <= MERGE_TOLERANCE_S and abs(g[0].end - s.end) <= MERGE_TOLERANCE_S
                        and all(s.phases not in m.phases for m in g)):
                    g.append(s)
                    break
            else:
                groups.append([s])
        done: List[Session] = []
        self.held = []
        for g in groups:
            # a group still young enough that a partner phase may yet close waits
            if latest - max(m.end for m in g) < HELD_TAIL_S and len(g) < 3:
                self.held.extend(g)
                continue
            done.append(self._combine(g))
        out = []
        for s in done:
            if s.energy_wh < NOISE_SESSION_WH and s.duration_s < NOISE_SESSION_S:
                continue
            self._file(s)
            out.append(s)
        return out

    def signature_of(self, s: Session) -> Optional["Signature"]:
        """The signature a just-filed session went into (its recent entry)."""
        for r in reversed(self.recent):
            if r["start"] == s.start and r["end"] == s.end and r["phases"] == s.phases:
                return next((x for x in self.signatures if x.id == r["signature"]), None)
        return None

    @staticmethod
    def _combine(g: List[Session]) -> Session:
        if len(g) == 1:
            return g[0]
        levels = {}
        pfs = []
        for m in g:
            levels.update(m.levels)
            if m.pf is not None:
                pfs.append(m.pf)
        return Session(phases="".join(sorted(levels)), start=min(m.start for m in g), end=max(m.end for m in g),
                       levels=levels, pf=(sum(pfs) / len(pfs)) if pfs else None)

    def _file(self, s: Session) -> None:
        tz = timezone.utc if not self.tz_offset_s else timezone(__import__("datetime").timedelta(seconds=self.tz_offset_s))
        noise = max(self.phases[p].noise for p in s.phases) if s.phases else MIN_NOISE_W
        best, best_score = None, 0.0
        for sig in self.signatures:
            sc = sig.matches(s, noise)
            if sc is not None and sc > best_score:
                best, best_score = sig, sc
        if best is None:
            best = Signature(id=self.next_id, phases=s.phases, power=s.power_by_phase(), duration_s=s.duration_s,
                             pf=s.pf, count=0, first_seen=s.start, last_seen=s.start, level_count=float(s.level_count))
            self.next_id += 1
            self.signatures.append(best)
            best.absorb(s, tz)
            best.count = 1
        else:
            best.absorb(s, tz)
        self.recent.append({"start": s.start, "end": s.end, "phases": s.phases, "kwh": round(s.energy_wh / 1000.0, 3),
                            "max_w": round(s.max_w), "levels": s.level_count, "signature": best.id})
        self.recent = self.recent[-MAX_RECENT_SESSIONS:]
        self.consolidate(noise)
        self._prune()

    def consolidate(self, noise_w: float = MIN_NOISE_W) -> int:
        """Merge signatures that have BECOME alike, and say how many went.

        Power and duration are running MEANS, so two signatures that are now
        indistinguishable need not have been when the second was created.
        Kozolec had one 1.8 kW load split five ways - 230, 136, 50, 27 and 18
        sightings, all within 4 % of each other - because the first one's
        mean duration was different at the moment the second arrived. Filing
        only ever looks at the signatures as they stand, and nothing came
        back to them afterwards; this does, by the same rule.
        """
        gone = 0
        again = True
        while again:
            again = False
            self.signatures.sort(key=lambda x: -x.count)
            for i, keep in enumerate(self.signatures):
                doomed = [j for j in range(i + 1, len(self.signatures))
                          if keep.alike(self.signatures[j], noise_w)]
                if not doomed:
                    continue
                moved = {}
                for j in reversed(doomed):
                    other = self.signatures.pop(j)
                    moved[other.id] = keep.id
                    keep.swallow(other)
                    gone += 1
                for r in self.recent:            # the sessions still point at them
                    if r.get("signature") in moved:
                        r["signature"] = moved[r["signature"]]
                again = True
                break
        return gone

    def _prune(self) -> None:
        """Keep the library to its cap, weakest first.

        A NAMED load is never evicted, which the old ordering had exactly
        backwards: it sorted named signatures to the front and then kept the
        tail, so the ones the user had taken the trouble to name were the
        first to go. Among the unnamed the best evidence survives."""
        if len(self.signatures) > MAX_SIGNATURES:
            self.signatures.sort(key=lambda x: (x.name is not None, x.evidence, x.count, x.last_seen))
            self.signatures = self.signatures[-MAX_SIGNATURES:]

    # ------------------------------------------------ query
    def active(self, now_ts: float) -> List[dict]:
        """What is on right now, per phase group, with the best signature guess."""
        out = []
        for ph, st in self.phases.items():
            a = st.active(now_ts)
            if a is None:
                continue
            since, w = a
            out.append({"phases": ph, "since": since, "watts": round(w), "signature": self._guess(ph, w, now_ts - since)})
        # phases that started together are one load
        merged: List[dict] = []
        for o in sorted(out, key=lambda x: x["since"]):
            for m in merged:
                if abs(m["since"] - o["since"]) <= MERGE_TOLERANCE_S:
                    m["phases"] += o["phases"]
                    m["watts"] += o["watts"]
                    m["signature"] = None
                    break
            else:
                merged.append(dict(o))
        for m in merged:
            m["phases"] = "".join(sorted(m["phases"]))
            if m["signature"] is None:
                m["signature"] = self._guess(m["phases"], m["watts"], now_ts - m["since"], per_phase=None)
            sig = next((x for x in self.signatures if x.id == m["signature"]), None) if m["signature"] else None
            m["name"] = sig.name if sig else None
        return merged

    def _guess(self, phases: str, watts: float, elapsed: float, per_phase=None) -> Optional[int]:
        best, best_d = None, None
        for sig in self.signatures:
            if sig.phases != phases:
                continue
            total = sum(sig.power.values())
            tol = max(MATCH_POWER_REL * max(total, watts), MIN_NOISE_W)
            if abs(total - watts) <= tol and elapsed <= sig.duration_s * MATCH_DURATION_FACTOR + 60:
                d = abs(total - watts)
                if best_d is None or d < best_d:
                    best, best_d = sig.id, d
        return best

    def unknown_power(self, now_ts: float) -> float:
        return float(sum(m["watts"] for m in self.active(now_ts)))

    def active_by_name(self, now_ts: float) -> Dict[str, float]:
        """Watts on right now per NAME - signatures sharing a name are one
        device, which is what giving two of them the same name means."""
        out: Dict[str, float] = {}
        for a in self.active(now_ts):
            if a.get("name"):
                out[a["name"]] = out.get(a["name"], 0.0) + float(a["watts"])
        return out

    def names(self) -> Dict[str, List[int]]:
        """name -> the signature ids filed under it."""
        out: Dict[str, List[int]] = {}
        for sig in self.signatures:
            if sig.name:
                out.setdefault(sig.name, []).append(sig.id)
        return out

    def rename(self, signature_id: int, name: Optional[str]) -> bool:
        for sig in self.signatures:
            if sig.id == signature_id:
                sig.name = (name or "").strip() or None
                return True
        return False

    # ------------------------------------------------ storage
    def to_dict(self) -> dict:
        return {"phases": {p: st.to_dict() for p, st in self.phases.items()}, "held": [s.to_dict() for s in self.held],
                "signatures": [s.to_dict() for s in self.signatures], "recent": self.recent, "next_id": self.next_id,
                "tz_offset_s": self.tz_offset_s}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Detector":
        det = cls()
        if not d:
            return det
        det.phases = {p: PhaseState.from_dict((d.get("phases") or {}).get(p)) for p in PHASES}
        det.held = [Session.from_dict(x) for x in d.get("held") or []]
        det.signatures = [Signature.from_dict(x) for x in d.get("signatures") or []]
        det.recent = list(d.get("recent") or [])
        det.next_id = d.get("next_id", 1)
        det.tz_offset_s = d.get("tz_offset_s", 0.0)
        return det


# ------------------------------------------------------------------ the fleet: main meter + downstream meters
@dataclass
class Fleet:
    """One detector per meter. The MAIN meter sees everything; a DOWNSTREAM
    meter sees only its own subpanel or circuit. A main-meter
    session that a downstream meter also saw - same start, same end, same
    phases, same size - is located there; one that none saw is upstream of
    them all. Locations accumulate per signature, so the answer sharpens
    with every session."""
    main: Detector = field(default_factory=Detector)
    subs: Dict[str, Detector] = field(default_factory=dict)
    pending_main: List[Session] = field(default_factory=list)    # main sessions awaiting a downstream partner
    pending_sub: Dict[str, List[Session]] = field(default_factory=dict)
    agnostic: Dict[str, bool] = field(default_factory=dict)      # meters that report only a total

    def process(self, main_samples, sub_samples: Dict[str, Dict[str, Sequence[Tuple[float, float]]]],
                main_q=None, sub_q=None, now_ts: Optional[float] = None,
                agnostic: Optional[Dict[str, bool]] = None,
                pv: Optional[Dict[str, Dict[float, float]]] = None) -> None:
        latest = now_ts or 0.0
        if agnostic:
            self.agnostic.update(agnostic)
        # only the main meter needs the array: a downstream meter sees the
        # house side of it and never the sun
        closed_main = self.main.process(main_samples, main_q, now_ts, pv)
        closed_sub = {}
        for name, samples in sub_samples.items():
            det = self.subs.setdefault(name, Detector())
            det.tz_offset_s = self.main.tz_offset_s
            closed_sub[name] = det.process(samples, (sub_q or {}).get(name), now_ts)
        self._locate(closed_main, closed_sub, latest)

    def _locate(self, closed_main: List[Session], closed_sub: Dict[str, List[Session]], latest: float) -> None:
        self.pending_main += closed_main
        for name, sessions in closed_sub.items():
            self.pending_sub.setdefault(name, []).extend(sessions)
        still: List[Session] = []
        for m in self.pending_main:
            sig = self.main.signature_of(m)
            hit = None
            for name, subs in self.pending_sub.items():
                for i, s in enumerate(subs):
                    if _same_load(m, s, self.agnostic.get(name, False)):
                        hit = (name, i)
                        break
                if hit:
                    break
            if hit and sig is not None:
                name, i = hit
                self.pending_sub[name].pop(i)
                sig.locations[name] = sig.locations.get(name, 0) + 1
            elif latest - m.end < HELD_TAIL_S * 2:
                still.append(m)          # a partner may still close on a slower meter
        self.pending_main = still
        for name in list(self.pending_sub):
            self.pending_sub[name] = [s for s in self.pending_sub[name] if latest - s.end < HELD_TAIL_S * 2]

    def to_dict(self) -> dict:
        return {"main": self.main.to_dict(), "subs": {n: d.to_dict() for n, d in self.subs.items()},
                "pending_main": [s.to_dict() for s in self.pending_main],
                "pending_sub": {n: [s.to_dict() for s in v] for n, v in self.pending_sub.items()},
                "agnostic": self.agnostic}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Fleet":
        f = cls()
        if not d:
            return f
        f.main = Detector.from_dict(d.get("main"))
        f.subs = {n: Detector.from_dict(v) for n, v in (d.get("subs") or {}).items()}
        f.pending_main = [Session.from_dict(x) for x in d.get("pending_main") or []]
        f.pending_sub = {n: [Session.from_dict(x) for x in v] for n, v in (d.get("pending_sub") or {}).items()}
        f.agnostic = dict(d.get("agnostic") or {})
        return f


def _same_load(a: Session, b: Session, phase_agnostic: bool = False) -> bool:
    """Is the downstream session ``b`` the same load as the main-meter
    session ``a``? Always the same moment; then the same size.

    ``phase_agnostic`` is for a meter that reports only a total - most
    single-device meters do. It cannot say which phase the load is on, so
    only the magnitude is compared; the main meter's own session supplies the
    phase, which is how a device's phase gets learned for free."""
    if abs(a.start - b.start) > MERGE_TOLERANCE_S or abs(a.end - b.end) > MERGE_TOLERANCE_S:
        return False
    pa, pb = a.power_by_phase(), b.power_by_phase()
    if phase_agnostic:
        ta, tb = sum(pa.values()), sum(pb.values())
        return abs(ta - tb) <= max(MATCH_POWER_REL * max(ta, tb), MIN_NOISE_W)
    if a.phases != b.phases:
        return False
    for ph in a.phases:
        tol = max(MATCH_POWER_REL * max(pa[ph], pb.get(ph, 0.0)), MIN_NOISE_W)
        if abs(pa[ph] - pb.get(ph, 0.0)) > tol:
            return False
    return True


def suggest_levels(signatures: Sequence[Signature], recent: Sequence[dict]) -> List[List[int]]:
    """Signatures that look like different settings of ONE device.

    A hob on three settings looks like three signatures: same phases, the
    same power factor, and - because it is one appliance - never two of them
    running at once. That is the whole test; the sizes are deliberately not
    compared, since settings can be any ratio. It is only a suggestion, and
    confirming it means giving them the same name."""
    times: Dict[int, List[Tuple[float, float]]] = {}
    for r in recent:
        times.setdefault(r["signature"], []).append((r["start"], r["end"]))

    def overlap(x: int, y: int) -> bool:
        for s1, e1 in times.get(x, ()):
            for s2, e2 in times.get(y, ()):
                if s1 < e2 and s2 < e1:
                    return True
        return False

    def compatible(a: Signature, b: Signature) -> bool:
        if a.phases != b.phases:
            return False
        if (a.pf is None) != (b.pf is None):
            return False
        if a.pf is not None and abs(a.pf - b.pf) > MATCH_PF_TOL:
            return False
        return not overlap(a.id, b.id)

    pool = [s for s in signatures if not s.name and s.count >= 2]
    groups: List[List[Signature]] = []
    for sig in sorted(pool, key=lambda x: -x.count):
        for g in groups:
            if all(compatible(sig, m) for m in g):
                g.append(sig)
                break
        else:
            groups.append([sig])
    return [sorted(x.id for x in g) for g in groups if len(g) > 1]


def most_specific(locations: Dict[str, int], count: int, parents: Optional[Dict[str, Optional[str]]] = None) -> str:
    """Which meter a signature belongs to, given the Energy dashboard's
    nesting. A load seen by both the workshop's meter and the boiler's is the
    BOILER's - the deepest meter that saw it, not the widest. ``parents`` maps
    a meter to the meter it sits inside (``included_in_stat``)."""
    seen = {n for n, k in locations.items() if k * 2 >= count}
    if not seen:
        return "main"
    parents = parents or {}

    def ancestors(name):
        out, cur = [], parents.get(name)
        while cur and cur not in out:
            out.append(cur)
            cur = parents.get(cur)
        return out

    deepest = [n for n in seen if not any(n in ancestors(m) for m in seen if m != n)]
    return sorted(deepest or seen)[0]


_BARS = " ▁▂▃▄▅▆▇█"


def sparkline(counts: Sequence[float]) -> str:
    """One line of bars, for a place that has only one line - a menu row.

    Each row of a flow menu is a single label, so the shape of the day has
    to fit on it or not appear at all (Anze, 2026-09-18)."""
    top = max(counts) if counts else 0
    if top <= 0:
        return ""
    return "".join(_BARS[0] if not c else _BARS[min(8, max(1, round(8 * c / top)))] for c in counts)


HISTOGRAM_ROWS = 5
HISTOGRAM_COL = 2              # characters per hour, so the day is 48 wide


def hour_histogram(counts: Sequence[int], rows: int = HISTOGRAM_ROWS,
                   width: int = HISTOGRAM_COL) -> List[str]:
    """The day as a block chart, for a form that renders markdown.

    A config flow cannot draw a graph. It can print one: 24 columns two
    characters wide, five rows tall, half-blocks for the halves - which says
    a good deal more than one line of sparkline did.
    """
    top = max(counts) if counts else 0
    if top <= 0:
        return []
    out = []
    for r in range(rows, 0, -1):
        line = []
        for c in counts:
            level = (c / top) * rows
            line.append(("█" if level >= r else "▄" if level >= r - 0.5 else " ") * width)
        out.append("|" + "".join(line))
    out.append("+" + "-" * (len(counts) * width))
    ruler = [" "] * (len(counts) * width)
    for h in range(0, len(counts), 3):
        for i, ch in enumerate(str(h)):
            if h * width + i < len(ruler):
                ruler[h * width + i] = ch
    out.append(" " + "".join(ruler))
    return out


DAY_NAMES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


def day_histogram(counts: Sequence[int], rows: int = 3, width: int = 3) -> List[str]:
    """The week as a block chart, Monday first.

    Which DAYS a load runs on separates a washing machine from a dishwasher
    far better than the hour does, and the hour histogram alone could not
    show it (Anze, 2026-09-17)."""
    top = max(counts) if counts else 0
    if top <= 0:
        return []
    out = []
    for r in range(rows, 0, -1):
        line = []
        for c in counts:
            level = (c / top) * rows
            line.append(("█" if level >= r else "▄" if level >= r - 0.5 else " ") * width)
        out.append("|" + "".join(line))
    out.append("+" + "-" * (len(counts) * width))
    out.append(" " + "".join(name[:width].ljust(width) for name in DAY_NAMES[:len(counts)]))
    return out


def _and(names: Sequence[str]) -> str:
    names = list(names)
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def describe_location(locations: Dict[str, int], count: int,
                      parents: Optional[Dict[str, Optional[str]]] = None,
                      phases: str = "") -> str:
    """Where the load is, said by EXCLUSION.

    "In the house" is worth little when the house meter covers everything.
    What narrows it is what did NOT see it: a load the house meter saw but
    neither the boy's room nor the office sockets did is somewhere in the
    rest of the house, and that sentence is the useful one. With no meter at
    all the phase is still a clue, being one leg of the board."""
    parents = parents or {}
    on = f"on phase {'+'.join(p.upper() for p in phases)}" if phases else ""
    where = most_specific(locations, count, parents)
    if where != "main":
        children = [c for c, parent in parents.items() if parent == where]
        missed = sorted(c for c in children if locations.get(c, 0) * 2 < count)
        return f"in {where}, outside {_and(missed)}" if missed else f"in {where}"
    partial = sorted((n for n, k in locations.items() if k), key=lambda n: -locations[n])
    if partial:
        n = partial[0]
        return f"under no meter, though {n} saw it {locations[n]} of {count} times" + (f", {on}" if on else "")
    return f"under no meter, {on}" if on else "under no meter"


def location_confidence(locations: Dict[str, int], count: int,
                        parents: Optional[Dict[str, Optional[str]]] = None) -> float:
    """How sure the location is: the share of sightings that agree with it."""
    if count <= 0:
        return 0.0
    where = most_specific(locations, count, parents)
    if where != "main":
        return round(min(1.0, locations.get(where, 0) / count), 2)
    return round(1.0 - min(1.0, (max(locations.values()) / count) if locations else 0.0), 2)
