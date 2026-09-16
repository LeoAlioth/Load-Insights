"""Hourly series arithmetic. Pure."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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
    """``base`` minus every series in ``ids``, hour by hour, never below zero
    (a device metering more than the site is a meter fault, not negative
    household).

    Before a device's FIRST statistics row it counts as zero: its energy in
    those hours was not metered, so it was part of the unmetered remainder,
    and subtracting nothing is exactly right. Without this the youngest
    device caps the whole series - on the Kozolec two small loads added three
    weeks ago cut 10.5 weeks of remainder history to 2.8 (2026-09-16). AFTER
    its first row a missing hour is a real gap (sensor dropout) and the hour
    is dropped, as in ``combine``. A device with no rows at all is zero
    throughout; ``coverage`` reports it.
    """
    ids = list(ids)
    if not ids:
        return list(base)
    maps = []
    for sid in ids:
        m = {_key(t): v for t, v in parts.get(sid, ()) if v is not None}
        maps.append((m, min(m) if m else None))
    out = []
    for t, v in base:
        k = _key(t)
        total = 0.0
        ok = True
        for m, first in maps:
            if first is None or k < first:
                continue            # not yet metered: nothing to subtract
            if k not in m:
                ok = False          # metered by then but this hour is missing
                break
            total += m[k]
        if ok:
            out.append((t, max(0.0, v - total)))
    return out


def coverage(parts: Dict[str, Sequence[Sample]], ids: Iterable[str]) -> Tuple[Optional[datetime], List[str]]:
    """From when every listed device has statistics (the remainder is
    complete from there on), and which devices have none at all."""
    since = None
    missing = []
    for sid in ids:
        rows = [t for t, v in parts.get(sid, ()) if v is not None]
        if not rows:
            missing.append(sid)
            continue
        first = min(rows, key=_key)
        if since is None or _key(first) > _key(since):
            since = first
    return since, missing
