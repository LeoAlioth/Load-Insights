"""Hour-of-week consumption profiles, and a forecast built from one.

The model is deliberately the simplest thing that is honest about a household:
consumption repeats weekly. Each of the 168 (weekday, hour) slots holds a
recency-weighted mean of every hour ever seen in that slot - weight halves
every HALF_LIFE weeks, so a habit that changed three weeks ago has already
mostly replaced the old one. A slot never seen falls back to the hour-of-day
mean across the week, then to the overall mean.

On top of the profile sits one LEVEL correction: the last 24 completed hours
against what the profile would have said for them, clamped and damped, so a
cold snap or a house full of guests lifts the coming days a little without a
single odd day rewriting the profile. Nothing here is learned in the machine
sense, and every number can be explained on a dashboard.

Times are tz-aware local datetimes; slots use local wall-clock weekday/hour,
which is what people's habits follow across a DST change. Horizon buckets are
generated in UTC and converted, so they are 168 real consecutive hours.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

Sample = Tuple[datetime, float]

HOURS_PER_WEEK = 168
DEFAULT_HALF_LIFE_WEEKS = 3.0
LEVEL_CLAMP = (0.5, 2.0)
LEVEL_DAMPING = 0.5      # adj = 1 + damping * (ratio - 1)
LEVEL_MIN_HOURS = 12     # fewer completed hours than this: no correction
HISTORY_HOURS = 48       # actual hourly kWh carried beside the forecast, for actual-vs-forecast cards
WEEK_SECONDS = 7 * 86400.0


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _key(t: datetime) -> float:
    """The instant. Aware datetimes sharing a tzinfo compare by naive wall
    clock (PEP 495), so the DST-end repeated hour would collide on the
    datetime itself; see series._key."""
    return t.timestamp()


def slot_of(dt: datetime) -> int:
    return dt.weekday() * 24 + dt.hour


def hour_buckets(start: datetime, hours: int) -> List[datetime]:
    """``hours`` consecutive real hours from ``start``, in ``start``'s zone."""
    tz = start.tzinfo
    base = start.astimezone(timezone.utc)
    return [(base + timedelta(hours=i)).astimezone(tz) for i in range(hours)]


@dataclass(frozen=True)
class Profile:
    slots: tuple                 # 168 x Optional[float]  kWh/h
    hour_of_day: tuple           # 24 x Optional[float]   fallback
    overall: Optional[float]
    sample_count: int
    span_weeks: float

    def slot_kwh(self, dt: datetime) -> Optional[float]:
        v = self.slots[slot_of(dt)]
        if v is None:
            v = self.hour_of_day[dt.hour]
        if v is None:
            v = self.overall
        return v

    def predict(self, start: datetime, hours: int, level: float = 1.0) -> List[Sample]:
        out = []
        for t in hour_buckets(floor_hour(start), hours):
            v = self.slot_kwh(t)
            out.append((t, (v if v is not None else 0.0) * level))
        return out


def fit_profile(samples: Sequence[Sample], now: datetime,
                half_life_weeks: float = DEFAULT_HALF_LIFE_WEEKS) -> Profile:
    """Recency-weighted slot means. The hour containing ``now`` is excluded:
    its statistic is still accumulating and would read low."""
    cutoff = floor_hour(now)
    cutoff_k = _key(cutoff)
    sw = [0.0] * HOURS_PER_WEEK
    swx = [0.0] * HOURS_PER_WEEK
    hw = [0.0] * 24
    hwx = [0.0] * 24
    tw = 0.0
    twx = 0.0
    n = 0
    oldest = None
    for t, v in samples:
        if v is None or _key(t) >= cutoff_k:
            continue
        age_weeks = (_key(now) - _key(t)) / WEEK_SECONDS
        w = 0.5 ** (age_weeks / half_life_weeks) if half_life_weeks > 0 else 1.0
        s = slot_of(t)
        sw[s] += w
        swx[s] += w * v
        hw[t.hour] += w
        hwx[t.hour] += w * v
        tw += w
        twx += w * v
        n += 1
        if oldest is None or _key(t) < _key(oldest):
            oldest = t
    slots = tuple((swx[i] / sw[i]) if sw[i] > 0 else None for i in range(HOURS_PER_WEEK))
    hod = tuple((hwx[i] / hw[i]) if hw[i] > 0 else None for i in range(24))
    overall = (twx / tw) if tw > 0 else None
    span = ((cutoff_k - _key(oldest)) / WEEK_SECONDS) if oldest else 0.0
    return Profile(slots=slots, hour_of_day=hod, overall=overall, sample_count=n, span_weeks=span)


def level_correction(profile: Profile, samples: Sequence[Sample], now: datetime) -> float:
    """Last 24 completed hours, actual over profile, clamped then damped."""
    end = _key(floor_hour(now))
    start = end - 24 * 3600.0
    actual = 0.0
    expected = 0.0
    hours = 0
    for t, v in samples:
        if v is None or not (start <= _key(t) < end):
            continue
        e = profile.slot_kwh(t)
        if e is None:
            continue
        actual += v
        expected += e
        hours += 1
    if hours < LEVEL_MIN_HOURS or expected <= 0:
        return 1.0
    ratio = min(LEVEL_CLAMP[1], max(LEVEL_CLAMP[0], actual / expected))
    return 1.0 + LEVEL_DAMPING * (ratio - 1.0)


def next_hour_watts(predicted: Sequence[Sample], now: datetime) -> Optional[float]:
    """Expected average power over the coming 60 minutes: the current hour's
    bucket and the next, blended by how far into the hour we are."""
    if not predicted:
        return None
    cur = floor_hour(now)
    by_start = {_key(t): v for t, v in predicted}
    if _key(cur) not in by_start:
        return None
    nxt = hour_buckets(cur, 2)[1]
    f = (_key(now) - _key(cur)) / 3600.0
    a = by_start[_key(cur)]
    b = by_start.get(_key(nxt), a)
    return ((1.0 - f) * a + f * b) * 1000.0


def day_total_kwh(actual: Sequence[Sample], predicted: Sequence[Sample],
                  day: datetime, now: datetime) -> float:
    """One local calendar day: actual for its completed hours before ``now``,
    the forecast for the hour in progress and the rest."""
    cutoff = _key(floor_hour(now))
    d = day.date()
    total = 0.0
    seen = set()
    for t, v in actual:
        if v is not None and t.date() == d and _key(t) < cutoff:
            total += v
            seen.add(_key(t))
    for t, v in predicted:
        if t.date() == d and _key(t) >= cutoff and _key(t) not in seen:
            total += v
    return total


@dataclass(frozen=True)
class Forecast:
    hourly: tuple                # 168 x (period start, kWh)
    next_hour_w: Optional[float]
    today_kwh: float
    tomorrow_kwh: float
    level: float
    sample_count: int
    span_weeks: float
    history: tuple = ()          # last HISTORY_HOURS completed hours, actual kWh


def recent_history(samples: Sequence[Sample], now: datetime, hours: int = HISTORY_HOURS) -> tuple:
    """The completed hours before ``now``, oldest first, same shape as the
    forecast so a card can draw actual and forecast off one entity."""
    end = _key(floor_hour(now))
    start = end - hours * 3600.0
    return tuple(sorted(((t, v) for t, v in samples if v is not None and start <= _key(t) < end), key=lambda s: _key(s[0])))


def forecast(samples: Sequence[Sample], now: datetime, horizon_hours: int = HOURS_PER_WEEK,
             half_life_weeks: float = DEFAULT_HALF_LIFE_WEEKS) -> Forecast:
    profile = fit_profile(samples, now, half_life_weeks)
    level = level_correction(profile, samples, now)
    hourly = profile.predict(now, horizon_hours, level)
    today = floor_hour(now).replace(hour=0)
    tomorrow = hour_buckets(today, 25)[24]
    return Forecast(
        hourly=tuple(hourly),
        next_hour_w=next_hour_watts(hourly, now),
        today_kwh=day_total_kwh(samples, hourly, today, now),
        tomorrow_kwh=day_total_kwh(samples, hourly, tomorrow, now),
        level=level,
        sample_count=profile.sample_count,
        span_weeks=profile.span_weeks,
        history=recent_history(samples, now),
    )
