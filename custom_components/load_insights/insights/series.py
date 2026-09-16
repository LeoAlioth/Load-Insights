"""Hourly series arithmetic. Pure."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Sequence, Tuple

Sample = Tuple[datetime, float]   # (period start, kWh in that hour)


def _key(t: datetime) -> float:
    """Dict/set key for an hour: the INSTANT, never the aware datetime.

    Two aware datetimes sharing one tzinfo object compare by their naive wall
    clock (PEP 495), so in the repeated hour at the end of DST the 02:00 CEST
    and 02:00 CET buckets are "equal" and collide. Epoch seconds do not.
    """
    return t.timestamp()


def combine(series_by_id: Dict[str, Sequence[Sample]], terms: Iterable[Tuple[str, float]]) -> List[Sample]:
    """Signed sum of several hourly series, hour by hour.

    An hour is kept only when EVERY term has a row for it: a gap in one
    statistic must produce a gap in the result, not a silently smaller
    number - a missing PV hour would otherwise read as lower consumption.
    """
    terms = list(terms)
    if not terms:
        return []
    maps = []
    for sid, sign in terms:
        maps.append(({_key(t): v for t, v in series_by_id.get(sid, ()) if v is not None}, sign))
    when = {_key(t): t for t, v in series_by_id.get(terms[0][0], ()) if v is not None}
    hours = set(maps[0][0])
    for m, _ in maps[1:]:
        hours &= set(m)
    return [(when[k], sum(m[k] * sign for m, sign in maps)) for k in sorted(hours)]


def subtract_all(base: Sequence[Sample], parts: Dict[str, Sequence[Sample]], ids: Iterable[str]) -> List[Sample]:
    """``base`` minus every series in ``ids``, hour by hour, same gap rule,
    never below zero (a device metering more than the site is a meter fault,
    not negative household)."""
    ids = list(ids)
    if not ids:
        return list(base)
    maps = [{_key(t): v for t, v in parts.get(sid, ()) if v is not None} for sid in ids]
    out = []
    for t, v in base:
        k = _key(t)
        if all(k in m for m in maps):
            out.append((t, max(0.0, v - sum(m[k] for m in maps))))
    return out
