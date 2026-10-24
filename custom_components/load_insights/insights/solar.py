"""Which of a solar device's sensors are the ARRAY's.

The Energy dashboard names a solar source by its energy statistic, and the
per-phase power sensors are then looked up on that statistic's device. Some
integrations hang more than the inverter on that one device: SolarEdge Modbus
Multi puts the M1 meter (the GRID meter, export positive) beside the inverter,
so "the array's phase powers" came out as the grid meter and every cloud step
was explained by the meter itself (INS-23, Andrej's site, 2026-10-21).
"""
from __future__ import annotations

import re
from typing import Iterable, List, Sequence

# a meter's reading is named for it: SolarEdge's "m1" / "m2", or the words meter / grid
_METER_WORD = re.compile(r"(?:^|_)(?:m\d|meter|grid)(?:_|$)")


def array_rows(rows: Sequence[dict], grid_power: Iterable[str] = ()) -> List[dict]:
    """``rows`` (entity_id, device_class, name) without the readings that are a
    meter's: the grid meter's configured power sensors, and any named for a
    meter."""
    grid = {g for g in grid_power if g}
    out = []
    for r in rows:
        text = re.sub(r"[^a-z0-9]+", "_", f"{r.get('entity_id', '')} {r.get('name', '')}".lower())
        if r.get("entity_id") in grid or _METER_WORD.search(text):
            continue
        out.append(r)
    return out
