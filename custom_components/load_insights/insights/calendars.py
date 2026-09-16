"""Calendars as explanatory inputs. Pure.

A calendar is linked without saying what it means. Its EXISTENCE signal - an
event is active in this hour - is fitted against the profile's residuals, and
the fit decides its role: an "away" calendar comes out as daytime factors near
0.4 and night factors near 1; a "guests" calendar as evening factors above 1;
a calendar that means nothing for consumption fails the guard and is ignored.
Titles are fitted second, each on the hours the calendar is on, against the
calendar's other on-hours - so a title earns a factor only for how it DIFFERS
from the calendar's average effect, never for that effect twice.

The fit is multiplicative, per hour of day: on-hours' actual-over-expected
divided by off-hours' actual-over-expected, weighted like the profile. That is
the shape occupancy has - an empty house drops the day to the base load and
barely touches the night - and a single factor is the fallback when an hour
of day has too few on-hours to stand alone. Same guard as every covariate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MIN_ON_HOURS = 24          # a calendar (or title) on for less than a day in the window: no fit
MIN_OFF_HOURS = 24
MIN_PER_HOUR_OF_DAY = 5    # on-hours at one hour of day, else the single factor stands in
MIN_EXPLAINED = 0.03
FACTOR_CLAMP = (0.1, 5.0)


def _norm_title(title: Optional[str]) -> str:
    return " ".join((title or "").strip().lower().split())


@dataclass
class CalendarSignals:
    """Hour keys (epoch seconds of the period start) an event is active in,
    over history AND horizon, for one calendar; titles are subsets."""
    entity: str
    existence: Set[float] = field(default_factory=set)
    titles: Dict[str, Set[float]] = field(default_factory=dict)

    @classmethod
    def from_events(cls, entity: str, events: Iterable[Tuple[float, float, str]], hour_keys: Sequence[float]) -> "CalendarSignals":
        """``events`` are (start_key, end_key, title); an hour counts as on
        when the event overlaps it. ``hour_keys`` are the hours we care about."""
        sig = cls(entity=entity)
        keys = sorted(hour_keys)
        for start, end, title in events:
            if end <= start:
                continue
            on = [k for k in keys if k < end and k + 3600.0 > start]
            sig.existence.update(on)
            t = _norm_title(title)
            if t:
                sig.titles.setdefault(t, set()).update(on)
        return sig


@dataclass(frozen=True)
class Factors:
    """Multipliers for a binary signal, TWO-LEVEL: the on-hours' own
    actual-over-expected and the off-hours' own. The slot means were fitted on
    a mixture of both kinds of hour, so the off-hours deserve their correction
    as much as the on-hours do - an "away" calendar lifts ordinary days a
    little as well as dropping the away days a lot. Per hour of day where the
    data can afford it, the single pair elsewhere. ``engaged`` False means the
    guard failed and every multiplier is 1.

    ``contrast`` (on over off) is the number a person reads: 0.4 means "the
    house runs at 40 % while this calendar is on"."""
    on: float = 1.0
    off: float = 1.0
    on_by_hour: tuple = ()             # 24 x Optional[float]
    off_by_hour: tuple = ()
    explained: float = 0.0
    on_hours: int = 0
    engaged: bool = False

    def at(self, hour_of_day: int, is_on: bool) -> float:
        if not self.engaged:
            return 1.0
        table = self.on_by_hour if is_on else self.off_by_hour
        single = self.on if is_on else self.off
        if table and table[hour_of_day] is not None:
            return table[hour_of_day]
        return single

    @property
    def contrast(self) -> float:
        return (self.on / self.off) if self.off else 1.0

    def contrast_at(self, hour_of_day: int) -> float:
        off = self.at(hour_of_day, False)
        return (self.at(hour_of_day, True) / off) if off else 1.0


NO_FACTORS = Factors()


def _ratio(sub):
    num = sum(w * a for _, _, w, a, _ in sub)
    den = sum(w * e for _, _, w, _, e in sub)
    return (num / den) if den > 0 else None


def fit_factors(rows: Sequence[Tuple[float, int, float, float, float]], on: Set[float], off: Set[float],
                scale_off: bool = True) -> Factors:
    """``rows`` are (hour_key, hour_of_day, weight, actual, expected) for the
    history; ``on`` / ``off`` the hour keys to compare. With ``scale_off``
    the off-hours get their own factor (existence); without, only the
    on-hours are scaled and the off-set only decides that the signal is
    worth fitting (titles, whose off-set is the calendar's other on-hours)."""
    on_rows = [r for r in rows if r[0] in on and r[4] > 0]
    off_rows = [r for r in rows if r[0] in off and r[4] > 0]
    if len(on_rows) < MIN_ON_HOURS or len(off_rows) < MIN_OFF_HOURS:
        return NO_FACTORS
    r_on, r_off = _ratio(on_rows), _ratio(off_rows)
    if not r_on or not r_off:
        return NO_FACTORS
    on_single = _clamp(r_on)
    off_single = _clamp(r_off) if scale_off else 1.0

    on_bh: List[Optional[float]] = [None] * 24
    off_bh: List[Optional[float]] = [None] * 24
    for h in range(24):
        so = [r for r in on_rows if r[1] == h]
        sf = [r for r in off_rows if r[1] == h]
        if len(so) >= MIN_PER_HOUR_OF_DAY and len(sf) >= MIN_PER_HOUR_OF_DAY:
            ro, rf = _ratio(so), _ratio(sf)
            if ro and rf:
                on_bh[h] = _clamp(ro)
                off_bh[h] = _clamp(rf) if scale_off else 1.0
    cand = Factors(on=on_single, off=off_single, on_by_hour=tuple(on_bh), off_by_hour=tuple(off_bh),
                   on_hours=len(on_rows), engaged=True)

    # the guard: the residual over the hours the signal touches must shrink
    before = sum(w * (a - e) ** 2 for _, _, w, a, e in on_rows + (off_rows if scale_off else []))
    after = (sum(w * (a - e * cand.at(h, True)) ** 2 for _, h, w, a, e in on_rows)
             + (sum(w * (a - e * cand.at(h, False)) ** 2 for _, h, w, a, e in off_rows) if scale_off else 0.0))
    if before <= 0:
        return NO_FACTORS
    explained = 1.0 - after / before
    if explained < MIN_EXPLAINED:
        return NO_FACTORS
    return Factors(on=on_single, off=off_single, on_by_hour=tuple(on_bh), off_by_hour=tuple(off_bh),
                   explained=explained, on_hours=len(on_rows), engaged=True)


def _clamp(f: float) -> float:
    return max(FACTOR_CLAMP[0], min(FACTOR_CLAMP[1], f))


@dataclass(frozen=True)
class CalendarModel:
    entity: str
    existence: Factors
    titles: Dict[str, Factors]

    def multiplier(self, hour_key: float, hour_of_day: int, signals: CalendarSignals) -> float:
        is_on = hour_key in signals.existence
        m = self.existence.at(hour_of_day, is_on)
        if is_on:
            for title, f in self.titles.items():
                if hour_key in signals.titles.get(title, ()):
                    m *= f.at(hour_of_day, True)
        return m

    @property
    def engaged(self) -> bool:
        return self.existence.engaged or any(f.engaged for f in self.titles.values())


def fit_calendar(rows: Sequence[Tuple[float, int, float, float, float]], signals: CalendarSignals,
                 scale_off: bool = True) -> CalendarModel:
    """Existence first; then each title against the calendar's OTHER on-hours,
    on the expectation that already carries the existence factor.

    Two uses. DETECTION runs against the mixture profile with ``scale_off``
    (the off-hours are biased by the mixture and need their own factor for
    the fit to be fair). EFFECTS run against a BASELINE profile fitted on
    off-hours only, ``scale_off=False`` - there the off-hours are already
    right and only the on-hours carry a factor."""
    hist_keys = {r[0] for r in rows}
    on = signals.existence & hist_keys
    off = hist_keys - on
    existence = fit_factors(rows, on, off, scale_off=scale_off)

    titles: Dict[str, Factors] = {}
    if on:
        adjusted = [(k, h, w, a, e * existence.at(h, k in on)) for k, h, w, a, e in rows]
        for title, keys in signals.titles.items():
            t_on = keys & on
            t_off = on - t_on
            f = fit_factors(adjusted, t_on, t_off, scale_off=False)
            if f.engaged:
                titles[title] = f
    return CalendarModel(entity=signals.entity, existence=existence, titles=titles)
