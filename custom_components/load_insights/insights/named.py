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


def plan_backfill(hourly_wh: Dict[int, float], first_start: Optional[float],
                  now_hour: float) -> Tuple[List[dict], float]:
    """The hours a named load's energy meter is owed, from what detection saw.

    Home Assistant's statistics for a meter start at its first recorded hour,
    though detection saw the load for days before it was named. ``hourly_wh``
    is hour start (epoch s) -> Wh; ``first_start`` the start of the meter's
    first long-term statistics row, or None when it has none. Rows cover the
    complete hours strictly before that row (or before ``now_hour``), each
    with ``start`` (epoch s), and ``state`` and ``sum`` in kWh counted from 0
    before the first of them, the way Home Assistant reads a meter's first
    row. Returns the rows and their total, which is what every existing row's
    sum must be raised by for the hours after them to read as they did.

    Nothing that is already recorded is written, so a second run finds no
    hour before the first row - the first of its own - and plans nothing."""
    stop = now_hour if first_start is None else min(first_start, now_hour)
    by_hour: Dict[int, float] = {}
    for hour, wh in hourly_wh.items():
        start = int(hour // 3600 * 3600)          # a half-hour time zone's hours onto UTC's
        if start < stop:
            by_hour[start] = by_hour.get(start, 0.0) + wh
    rows, total = [], 0.0
    for start in sorted(by_hour):
        total += by_hour[start] / 1000.0
        rows.append({"start": start, "state": total, "sum": total})
    return rows, total
