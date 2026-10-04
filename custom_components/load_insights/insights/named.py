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


# How far a meter's recorded sum may sit from what plan_rewrite would write
# before the hourly check rewrites it, in kWh: the Energy dashboard's last
# digit for these meters (suggested_display_precision 2). The library keeps
# each hour to 0.1 Wh, which over the window's ~260 hours drifts a few Wh at
# most, so a restart's rounding alone never rewrites a meter.
REWRITE_TOLERANCE_KWH = 0.01


def first_change(rows: List[dict], hours: List[dict], tolerance: float = REWRITE_TOLERANCE_KWH) -> Optional[int]:
    """The first hour plan_rewrite's ``rows`` would change, or None when the
    meter already says what detection saw: an hour it has no row for, or
    whose recorded sum is more than ``tolerance`` kWh off. Sums are running
    totals, so an hour filed into the wrong one shows here as much as a
    total that drifted (the shift is the last hour's difference). The rows
    before it stay as recorded, and five-minute rows with them."""
    recorded = {int(r["start"]): r["sum"] for r in hours}
    return next((r["start"] for r in rows
                 if r["start"] not in recorded or abs(r["sum"] - recorded[r["start"]]) > tolerance), None)


def vanished_into(before: Dict[str, list], after: Dict[str, list]) -> Dict[str, List[str]]:
    """name -> the names a rename or an adoption left with no signature,
    every one of whose signatures now wears that name - between two
    ``Fleet.names()``: a load named after another named one joins it, a
    name given a new one is renamed. Their meters' older history belongs
    to it now. One whose signatures went partly elsewhere, or were
    forgotten, is in nothing: its history cannot be split."""
    wears = {ref: name for name, refs in after.items() for ref in refs}
    out: Dict[str, List[str]] = {}
    for name, refs in sorted(before.items()):
        now = {wears.get(ref) for ref in refs}
        if name not in after and len(now) == 1 and None not in now:
            out.setdefault(now.pop(), []).append(name)
    return out


def plan_carry(extra_kwh: Dict[int, float], covered_from: int, hours: List[dict],
               fives: List[dict]) -> Tuple[List[dict], List[dict], float]:
    """A named load's meter's statistics before the window, with energy from
    elsewhere added: a name that vanished into it (vanished_into: the
    differences of its meter's recorded sum), the hours a signature kept
    from before it was named (Signature.older).

    ``extra_kwh`` is hour start (epoch s) -> kWh, only what is before
    ``covered_from`` counted - plan_rewrite writes the window from the
    library; ``hours`` and ``fives`` the meter's rows as plan_rewrite takes
    them. Each recorded row's sum gains what was added up to its hour, each
    five-minute row its hour's share pro rata, as plan_rewrite shares them,
    so the two tables still agree. An hour before the meter's first row - it
    is newer than what is carried - is made as plan_rewrite makes one;
    a gap between rows stays one, the next row's sum carrying it.

    Returns the hour rows and five-minute rows to write, and ``shift``: what
    every row from ``covered_from`` on is raised by. Added each time it
    runs - the runner carries each source once."""
    by_hour: Dict[int, float] = {}
    for hour, kwh in extra_kwh.items():
        start = int(hour // 3600 * 3600)          # a half-hour time zone's hours onto UTC's
        if start < covered_from:
            by_hour[start] = by_hour.get(start, 0.0) + kwh
    if not hours or not by_hour:
        return [], [], 0.0
    recorded = {int(r["start"]): r for r in hours}
    first_own = int(hours[0]["start"])
    added, at_end, hour_rows = 0.0, {}, []
    for h in range(min(by_hour), covered_from, 3600):
        added += by_hour.get(h, 0.0)
        at_end[h] = added
        rec = recorded.get(h)
        if rec is not None:
            hour_rows.append({"start": h, "state": rec["state"], "sum": rec["sum"] + added})
        elif h < first_own:
            hour_rows.append({"start": h, "state": added, "sum": added})
    five_rows = []
    for r in fives:
        h = int(r["start"] // 3600 * 3600)
        if h in at_end:
            used = by_hour.get(h, 0.0)
            share = min(1.0, (r["start"] + 300 - h) / 3600.0)
            five_rows.append({"start": r["start"], "state": r["state"],
                              "sum": r["sum"] + at_end[h] - used + used * share})
    return hour_rows, five_rows, added


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
