"""Auto-naming of confidently detected loads (INS-21; plan `docs/auto-naming-plan.md`). Pure.

Two kinds only: a FRIDGE or freezer (a compressor cycling) and a HEATER (a
heating element). Everything here is replayable: the recogniser (`recognise`)
over a load's `Facts`, the bar's hysteresis (`Tracker`), the numbering
(`next_name`), the tombstones (`Tombstone`), and the Energy dashboard's list as
it should read (`plan_dashboard`). Nothing here reads Home Assistant; the layer
above (`autonaming.py`) owns the store, the rename and the dashboard write.

Every number below is POLICY about what to put a name on, not a constant that
describes the world, and each says what it was calibrated on. Calibrated on
2026-10-20 on the live exports of Home, Kozolec and Andrej's site and on
production-style replays of Home's and Kozolec's last ten days
(`tests/autoname_bench.py`; the table is in the plan).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .classify import COOKING, HEATER, WORKSHOP

FRIDGE_KIND = "fridge"
HEATER_KIND = "heater"
KINDS = (FRIDGE_KIND, HEATER_KIND)

# ---------------------------------------------------------------- the bar
# Shared. Evidence is the detector's own "a real repeating load" score
# (`Signature.evidence`); 0.7 is the naming page's bar (`DEFAULT_MIN_EVIDENCE`),
# and the hand-named loads that are fridges or heaters at Home all sit above it
# (Mansarda's fridge and freezer 0.85 / 0.71) while Kozolec's fridges, split by
# the detector over dozens of signatures, sit at 0.5-0.67 and so are NOT
# named automatically (the plan says so).
MIN_EVIDENCE = 0.7
MIN_DAYS_ACTIVE = 3            # weekdays the load has energy on; ten days of history, so ~ days seen

# A fridge: a compressor cycling, one phase, a level that does not change.
FRIDGE_PHASES = 1
FRIDGE_WATTS = (30.0, 400.0)       # Kozolec's two (40-75 W), Home's kitchen fridge 65 W, Mansarda's 47 and 66 W, Andrej's 72 W
FRIDGE_RUN_S = (360.0, 5400.0)     # runs of 7-90 min: Andrej 7.5 min, Mansarda 19-22 min, Kozolec's median 29 min
FRIDGE_PERIOD_S = (600.0, 10800.0) # start to start, 10 min - 3 h: Andrej 21 min, Mansarda 41-90 min
FRIDGE_DUTY = (0.1, 0.85)          # run / period
FRIDGE_MIN_RUNS = 40               # a day or two of a fridge is 15-30 cycles; ten days is hundreds
FRIDGE_MIN_PER_DAY = 6.0           # cycles a day while it is seen (needs a day or more of span)
FRIDGE_MIN_BUSY_HOURS = 6          # hours of the day with real energy: a compressor does not run in one burst
FRIDGE_MAX_LEVELS = 1.6            # one level (a washing programme or a heater with two stages is not a compressor)
FRIDGE_MAX_JITTER = 0.6            # interval_mad / interval, where known (Mansarda 0.19-0.45)

# A heater: the classifier's own guess, its evidence and a repeated size.
HEATER_MIN_CONFIDENCE = 0.6        # of a 0.85 ceiling: pf >= 0.93 trusted, a steady level, 80 W - 20 kW
HEATER_MIN_WATTS = 500.0           # under it a "heater" is a chart of small steps (Home: dozens at 100-300 W)
HEATER_MIN_RUNS = 8                # Le Hottie's smallest named piece has 5 runs; the bar asks for 8
HEATER_MAX_LEVELS = 1.5
HEATER_MIN_SPAN_DAYS = 5.0         # first to last sighting: Andrej's night-time pulses span 2.2 days, Le Hottie's 6.5 (the best piece)
HEATER_NOT = (COOKING, WORKSHOP)   # a kettle or an oven is a heating element too, and "Heater 1" would be wrong

# Hysteresis. The load is checked once an hour; a day counts once.
DAYS_TO_NAME = 3                   # distinct days on which it cleared the bar before it is named
DAYS_TO_REVOKE = 7                 # consecutive days it has NOT cleared before its name is taken back
KEEP_DAYS = 30                     # how many past days a candidate remembers

# One device or two: look-alikes share a name. The detector splits one fridge
# over several signatures (Kozolec's two fridges over 30), so a second look-alike
# is never "Fridge 2" - only a load of another size or phase is.
SAME_WATTS = 0.25                  # within this share of the larger
TOMBSTONE_WATTS = 0.15             # an owner's removal covers a load this close in size...
TOMBSTONE_DURATION = 2.0           # ...and within this factor in duration


@dataclass(frozen=True)
class Facts:
    """What the recogniser reads of one signature. Optional fields may be None
    (the exported attribute does not carry them): a None skips its test."""
    ref: Tuple[str, int]
    phases: str
    watts: float
    duration_s: float
    interval_s: Optional[float]
    count: int
    span_days: Optional[float]
    evidence: float
    hour_wh: Sequence[float] = ()
    day_wh: Sequence[float] = ()
    level_count: Optional[float] = None
    interval_mad: Optional[float] = None
    guess_kind: Optional[str] = None
    guess_confidence: float = 0.0
    appliance: Optional[str] = None
    named: Optional[str] = None           # its name now, if any (an owner's, or ours)
    metered: bool = False                 # a metered device sits behind it: never touched


@dataclass(frozen=True)
class Verdict:
    kind: Optional[str]                   # FRIDGE_KIND, HEATER_KIND or None
    failed: Tuple[str, ...] = ()          # what stopped it (the nearest kind's failures), for the table
    nearest: Optional[str] = None


def busy_hours(hour_wh: Sequence[float]) -> int:
    """Hours of the day holding more than a quarter of an even share of the energy."""
    total = sum(hour_wh)
    return sum(1 for w in hour_wh if total > 0 and w > total / 96.0)


def days_active(day_wh: Sequence[float]) -> int:
    return sum(1 for w in day_wh if w > 0)


def fridge_failures(f: Facts) -> List[str]:
    out = []
    if len(set(f.phases)) != FRIDGE_PHASES:
        out.append("phases")
    if not FRIDGE_WATTS[0] <= f.watts <= FRIDGE_WATTS[1]:
        out.append("watts")
    if not FRIDGE_RUN_S[0] <= f.duration_s <= FRIDGE_RUN_S[1]:
        out.append("run length")
    if f.interval_s is None or not FRIDGE_PERIOD_S[0] <= f.interval_s <= FRIDGE_PERIOD_S[1]:
        out.append("period")
    elif not FRIDGE_DUTY[0] <= f.duration_s / f.interval_s <= FRIDGE_DUTY[1]:
        out.append("duty")
    if f.count < FRIDGE_MIN_RUNS:
        out.append("runs")
    if f.span_days is not None and f.span_days >= 1.0 and (f.count - 1) / f.span_days < FRIDGE_MIN_PER_DAY:
        out.append("cycles a day")
    if f.hour_wh and busy_hours(f.hour_wh) < FRIDGE_MIN_BUSY_HOURS:
        out.append("hours of the day")
    if f.level_count is not None and f.level_count >= FRIDGE_MAX_LEVELS:
        out.append("levels")
    if f.interval_mad is not None and f.interval_s and f.interval_mad / f.interval_s > FRIDGE_MAX_JITTER:
        out.append("irregular")
    if f.evidence < MIN_EVIDENCE:
        out.append("evidence")
    if f.day_wh and days_active(f.day_wh) < MIN_DAYS_ACTIVE:
        out.append("days")
    return out


def heater_failures(f: Facts) -> List[str]:
    out = []
    if f.guess_kind != HEATER:
        out.append("not a heating element")
    elif f.guess_confidence < HEATER_MIN_CONFIDENCE:
        out.append("guess confidence")
    if f.appliance in HEATER_NOT:
        out.append("appliance")
    if f.watts < HEATER_MIN_WATTS:
        out.append("watts")
    if f.count < HEATER_MIN_RUNS:
        out.append("runs")
    if f.level_count is not None and f.level_count >= HEATER_MAX_LEVELS:
        out.append("levels")
    if f.evidence < MIN_EVIDENCE:
        out.append("evidence")
    if f.span_days is not None and f.span_days < HEATER_MIN_SPAN_DAYS:
        out.append("days seen")
    return out


def recognise(f: Facts) -> Verdict:
    """The kind this load clears the bar for, or None - with what stopped the
    nearest kind (fewest failures), so a table can say why a load was left."""
    if f.metered:
        return Verdict(None, ("a metered device",), None)
    fridge, heater = fridge_failures(f), heater_failures(f)
    if not fridge:
        return Verdict(FRIDGE_KIND)
    if not heater:
        return Verdict(HEATER_KIND)
    near = FRIDGE_KIND if len(fridge) <= len(heater) else HEATER_KIND
    return Verdict(None, tuple(fridge if near == FRIDGE_KIND else heater), near)


# ---------------------------------------------------------------- hysteresis
@dataclass
class Candidate:
    kind: str
    days: List[str] = field(default_factory=list)       # ISO dates it cleared the bar on, oldest first
    missed: List[str] = field(default_factory=list)     # ISO dates it was checked and did not, since it last cleared
    descriptor: dict = field(default_factory=dict)      # phases, watts, duration_s - for tombstones and look-alikes

    def to_dict(self) -> dict:
        return {"kind": self.kind, "days": self.days, "missed": self.missed, "descriptor": self.descriptor}

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(d["kind"], list(d.get("days") or []), list(d.get("missed") or []), dict(d.get("descriptor") or {}))


def descriptor(f: Facts) -> dict:
    return {"phases": f.phases, "watts": round(f.watts, 1), "duration_s": round(f.duration_s, 1)}


def alike(a: dict, b: dict, watts: float = SAME_WATTS, duration: Optional[float] = None) -> bool:
    """Two descriptors of one phase set and size (and, if asked, length)."""
    if a.get("phases") != b.get("phases"):
        return False
    big = max(a["watts"], b["watts"], 1.0)
    if abs(a["watts"] - b["watts"]) / big > watts:
        return False
    if duration is not None:
        lo, hi = sorted((max(a["duration_s"], 1.0), max(b["duration_s"], 1.0)))
        return hi / lo <= duration
    return True


def tick(cands: Dict[str, Candidate], seen: Dict[str, Facts], day: str) -> Dict[str, Candidate]:
    """One hourly check on ``day`` (ISO date): every candidate key in ``seen``
    (its Facts now) clears or misses; a day counts once, and clearing wins over
    missing on the same day. Keys not in ``seen`` (gone or named by someone
    else) are dropped. Returns the new candidates; the input is not changed."""
    out: Dict[str, Candidate] = {}
    for key, f in seen.items():
        verdict = recognise(f)
        c = Candidate.from_dict(cands[key].to_dict()) if key in cands else None
        if verdict.kind is None and c is None:
            continue
        if c is None or (verdict.kind is not None and verdict.kind != c.kind):
            c = Candidate(verdict.kind) if verdict.kind else c
        if verdict.kind is not None:
            if day not in c.days:
                c.days = (c.days + [day])[-KEEP_DAYS:]
            c.missed = []                                  # a clear day ends a streak of misses
            c.descriptor = descriptor(f)
        elif day not in c.days and day not in c.missed:
            c.missed = (c.missed + [day])[-KEEP_DAYS:]
        out[key] = c
    return out


def ready(c: Candidate) -> bool:
    """Cleared the bar on enough distinct days."""
    return len(c.days) >= DAYS_TO_NAME


def lapsed(c: Candidate) -> bool:
    """Has not cleared for DAYS_TO_REVOKE days in a row (one bad day never revokes)."""
    return len(c.missed) >= DAYS_TO_REVOKE


# ---------------------------------------------------------------- names
@dataclass(frozen=True)
class Tombstone:
    """What the owner removed or renamed away: an auto-name is never made again for a load like it."""
    kind: str
    descriptor: dict
    name: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "descriptor": self.descriptor, "name": self.name}

    @classmethod
    def from_dict(cls, d: dict) -> "Tombstone":
        return cls(d["kind"], dict(d.get("descriptor") or {}), d.get("name") or "")


def buried(f: Facts, kind: str, tombs: Iterable[Tombstone]) -> bool:
    mine = descriptor(f)
    return any(t.kind == kind and alike(mine, t.descriptor, TOMBSTONE_WATTS, TOMBSTONE_DURATION) for t in tombs)


def next_name(pattern: str, taken: Iterable[str]) -> str:
    """``pattern`` ("Fridge {n}") with the lowest n not in ``taken`` (case and
    surrounding spaces ignored): never a name that is already someone's."""
    used = {(t or "").strip().casefold() for t in taken}
    n = 1
    while pattern.format(n=n).strip().casefold() in used:
        n += 1
    return pattern.format(n=n)


def name_for(f: Facts, kind: str, patterns: Dict[str, str], marks: Dict[str, dict], taken: Iterable[str]) -> str:
    """The name for a load about to be named: that of an auto-named look-alike
    of the same kind (the same device, split by the detector), else the next
    free number. ``marks`` is name -> {"kind", "descriptor"} of our own names."""
    mine = descriptor(f)
    for name, m in sorted(marks.items()):
        if m.get("kind") == kind and alike(mine, m.get("descriptor") or {}):
            return name
    return next_name(patterns[kind], taken)


def decide(facts: Dict[str, Facts], cands: Dict[str, Candidate], marks: Dict[str, dict], tombs: Sequence[Tombstone],
           patterns: Dict[str, str], taken: Iterable[str], owned: Sequence[Tuple[str, dict]] = (),
           fridge_word=lambda name: False) -> List[Tuple[str, str, str]]:
    """Who is named now: ``[(key, name, kind)]`` for each ready candidate that
    wears no name, is not buried and is not metered. A name an owner already
    gave is never in here (``Facts.named`` set), and the numbering sees the
    names handed out earlier in the same call."""
    taken = list(taken)
    marks = dict(marks)
    out = []
    for key in sorted(cands):
        c, f = cands[key], facts.get(key)
        if f is None or f.named or f.metered or not ready(c) or buried(f, c.kind, tombs) \
                or claimed(f, c.kind, owned, fridge_word):
            continue
        name = name_for(f, c.kind, patterns, marks, taken)
        marks[name] = {"kind": c.kind, "descriptor": c.descriptor}
        taken.append(name)
        out.append((key, name, c.kind))
    return out


# ---------------------------------------------------------------- the Energy dashboard
def plan_dashboard(devices: Sequence[dict], wanted: Dict[str, str], added: Sequence[str],
                   declined: Sequence[str] = ()) -> Tuple[Optional[List[dict]], List[str], List[str]]:
    """The Energy dashboard's ``device_consumption`` as it should read.

    ``wanted`` is the energy statistic of each auto-named load -> its name;
    ``added`` the statistics Load Insights put there before, ``declined`` those
    the owner took out again. Returns ``(new list or None when nothing changes,
    added, declined)``:

    * a wanted statistic that is not listed (by anyone) is appended as
      ``{"stat_consumption": stat, "name": name}``; one already listed, by the
      owner or by us, is left alone - no duplicates;
    * a statistic in ``added`` that is no longer wanted is removed - only that
      entry, and only if it is still one of ours (a stat in ``added``);
    * a statistic in ``added`` that is wanted but no longer listed was taken
      out by the owner: it goes to ``declined`` and is never added again.
    Entries the owner made are never touched."""
    listed = {d.get("stat_consumption") for d in devices}
    added, declined = list(added), list(declined)
    new = list(devices)
    for stat in list(added):
        if stat not in wanted:
            new = [d for d in new if d.get("stat_consumption") != stat]
            added.remove(stat)
        elif stat not in listed:
            added.remove(stat)
            declined.append(stat)
    for stat, name in sorted(wanted.items()):
        if stat in declined or any(d.get("stat_consumption") == stat for d in new):
            continue
        new.append({"stat_consumption": stat, "name": name})
        added.append(stat)
    declined = [s for s in dict.fromkeys(declined) if s in wanted]
    return (new if new != list(devices) else None), added, declined


# ---------------------------------------------------------------- reading a signature, and the owner's hand
def key_of(ref: Tuple[str, int]) -> str:
    return f"{ref[0]}#{ref[1]}"


def facts_of(ref: Tuple[str, int], sig) -> Facts:
    """The Facts of a ``Signature`` (duck-typed: this module imports no detector)."""
    g = sig.guess()
    return Facts(ref=ref, phases=sig.phases, watts=float(sig.watts), duration_s=float(sig.duration_s),
                 interval_s=sig.interval_s, count=sig.count, span_days=max(0.0, sig.last_seen - sig.first_seen) / 86400.0,
                 evidence=sig.evidence, hour_wh=list(sig.hour_wh), day_wh=list(sig.day_wh),
                 level_count=sig.level_count, interval_mad=sig.interval_mad, guess_kind=g.kind,
                 guess_confidence=g.confidence, appliance=g.appliance, named=sig.name)


def reconcile(marks: Dict[str, dict], present: Iterable[str]) -> Tuple[Dict[str, dict], List[Tombstone]]:
    """Our names against the names the library holds now (``present``, those a
    reset is carrying included). A name of ours that nothing wears any more was
    removed, renamed away or forgotten by the owner: it leaves ``marks`` and a
    Tombstone is made, so it is never made again for a load like it."""
    here = {(n or "").strip().casefold() for n in present}
    kept, buried_ = {}, []
    for name, m in marks.items():
        if name.strip().casefold() in here:
            kept[name] = m
        else:
            buried_.append(Tombstone(m.get("kind", ""), dict(m.get("descriptor") or {}), name))
    return kept, buried_


def claimed(f: Facts, kind: str, owned: Iterable[Tuple[str, dict]], fridge_word) -> bool:
    """Does an owner-named load already stand for this one - the detector having
    split a load the owner named? ``owned`` is (name, descriptor) of every
    signature wearing a name that is not ours. A fridge is claimed by a
    fridge-worded name (``fridge_word(name)``) of the same phases and size; a
    heater by any owner-named load of the same phases and size (an element's
    power is exact)."""
    mine = descriptor(f)
    for name, d in owned:
        if not alike(mine, d):
            continue
        if kind == HEATER_KIND or fridge_word(name):
            return True
    return False


async def write_dashboard(manager, wanted: Dict[str, str], added: Sequence[str],
                          declined: Sequence[str]) -> Tuple[bool, List[str], List[str]]:
    """Read the dashboard's device list off ``manager`` (Home Assistant's
    EnergyManager, or anything with ``data`` and an async ``async_update``),
    plan it (``plan_dashboard``) and write the whole list once if it changes.
    Returns (written, added, declined). Nothing is written while the dashboard
    has no preferences at all (``data`` is None: never set up)."""
    if manager.data is None:
        return False, list(added), list(declined)
    new, added, declined = plan_dashboard(list(manager.data.get("device_consumption") or []), wanted, added, declined)
    if new is not None:
        await manager.async_update({"device_consumption": new})
    return new is not None, added, declined
