"""What a named load is to Home Assistant, as far as that needs no Home
Assistant: whether it IS a metered device, and the history its energy meter
is owed. Pure."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple


def _key(name: Optional[str]) -> str:
    return (name or "").strip().casefold()


def metered_device(name: Optional[str], meters: Iterable[str]) -> Optional[str]:
    """The meter a named load is, or None: one of ``meters`` - those holding a
    single device - with the same name, whatever the case and the spaces
    round it. Such a load was named to join its device ("name it Water Pump
    so it merges"), and the meter's own readings and forecast stand for it,
    so it gets no detected entities (Anze, 2026-09-29)."""
    key = _key(name)
    return next((m for m in meters if key and _key(m) == key), None)


def one_device_meters(meters: Dict[str, bool]) -> List[str]:
    """meter name -> holds one device, as the names a load may be the same
    as: what the naming page offers under "Same device as"."""
    return sorted(m for m, one in meters.items() if one)


def chosen_name(typed: Optional[str], picked: Optional[str]) -> Optional[str]:
    """The name the naming page gives: a meter picked from the list, exactly
    as the meter is called, over whatever was typed; else the typed name;
    None when neither was given, which changes nothing."""
    return picked or (typed or "").strip() or None


def regrouped(before: Dict[str, list], after: Dict[str, list]) -> List[str]:
    """The names whose signatures changed between two ``Fleet.names()`` - a
    rename, an adoption or a forgetting - that still exist: the name given,
    and the one it was taken from while other signatures still wear it.
    Their energy meters' statistics hold the old signatures' history until
    rewritten (Inkubator, moved from the grid's #6 to Hiša's #14 on
    2026-10-04, read ~0 kWh a day until backfilled by hand)."""
    return sorted(n for n, refs in after.items() if set(refs) != set(before.get(n, ())))


def plan_rewrite(hourly_wh: Dict[int, float], covered_from: int, hours: List[dict],
                 fives: List[dict]) -> Tuple[List[dict], List[dict], float]:
    """The statistics a named load's energy meter should hold for the hours
    detection watched, from what detection saw in them.

    ``hourly_wh`` is hour start (epoch s) -> Wh; ``covered_from`` the first
    whole hour detection read; ``hours`` and ``fives`` the meter's long- and
    short-term rows as recorded - ``start`` (epoch s), ``state`` and ``sum``
    in kWh, oldest first, ``hours`` from the meter's first. The window runs to
    the last hour Home Assistant compiled.

    Recorded hours were wrong whenever the reading was not a clean meter: it
    appeared with days already in it, and a detection reset dropped it, which
    Home Assistant reads as a new meter and counts again (the kiln's 19 kWh on
    22.09). So every hour in the window gets detection's figure, carried on
    from the sum recorded before the window, and every five-minute row its
    hour's share pro rata - detection keeps hours, not five minutes - which
    is also what keeps the two tables agreeing: an hour's sum is compiled
    from its last five-minute row. Recorded states stay, since the next
    compile reads the live reading against the last one; an hour before the
    meter's first row gets its sum as its state, as a new meter's first row
    reads.

    Returns the hour rows and five-minute rows to write, and ``shift``: what
    every row after the window must be raised by for the live hours to carry
    on from it. Nothing when nothing is recorded yet - the next compile would
    restart the sum from 0 below what was written. Running it twice writes
    the same rows."""
    if not hours:
        return [], [], 0.0
    end = int(hours[-1]["start"]) + 3600
    by_hour: Dict[int, float] = {}
    for hour, wh in hourly_wh.items():
        start = int(hour // 3600 * 3600)          # a half-hour time zone's hours onto UTC's
        if covered_from <= start < end:
            by_hour[start] = by_hour.get(start, 0.0) + wh / 1000.0
    recorded = {int(r["start"]): r for r in hours}
    before = [r for r in hours if r["start"] < covered_from]
    total = before[-1]["sum"] if before else 0.0
    first = next((h for h in range(covered_from, end, 3600) if h in recorded or h in by_hour), None)
    if first is None:
        return [], [], 0.0
    hour_rows, at_end = [], {}
    for h in range(first, end, 3600):
        total += by_hour.get(h, 0.0)
        rec = recorded.get(h)
        hour_rows.append({"start": h, "state": rec["state"] if rec else total, "sum": total})
        at_end[h] = total
    five_rows = []
    for r in fives:
        h = int(r["start"] // 3600 * 3600)
        if h in at_end:
            used = by_hour.get(h, 0.0)
            share = min(1.0, (r["start"] + 300 - h) / 3600.0)
            five_rows.append({"start": r["start"], "state": r["state"], "sum": at_end[h] - used + used * share})
    return hour_rows, five_rows, total - hours[-1]["sum"]


def carry_reading(reported: Optional[float], seen: Optional[float], now: float,
                  counting: bool) -> Tuple[float, float]:
    """What a named load's energy meter reads, given what it last reported,
    detection's total when it did (``seen``), detection's total ``now`` (all
    kWh), and whether what detection gained since is new energy.

    The meter only ever counts up by what detection GAINS. Its total restarts
    from ten days at a reset and climbs again as history is re-read, and
    rounds a hair down on a restart; Home Assistant books a drop of under 10 %
    as a negative hour and a bigger one as a new meter, counting it all again
    (the kiln: 19 kWh on 22.09, a day it never fired). A re-read is not
    ``counting``: those hours are old, and plan_rewrite writes them.
    Returns (reading, the total to count on from)."""
    if reported is None:
        return now, now                    # a new meter: its first reading is where it starts
    if seen is None or not counting:
        return reported, now
    return reported + max(0.0, now - seen), now
