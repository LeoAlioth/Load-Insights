"""Any sensor as an explanatory input. Pure.

Attach an entity and Load Insights works out what it means for consumption,
the same way it does for a calendar: no declaration of what the thing is, no
per-sensor special case. The trick is that every input becomes a CATEGORICAL
signal - a label per hour - and categorical signals already have a fitting
machinery, a guard and a projection.

  * A sensor with few distinct values IS its label: a tariff block reading
    2 / 3 / 4, a mode select, a person's home/away.
  * A numeric one is cut into quantile BANDS, so irradiance or a price
    becomes "low / mid-low / mid-high / high". Bands rather than a regression
    on purpose: the response need not be linear, and a band costs one factor
    the same as any other state.

An input without a forward source of its own can only reach the horizon two
ways, and both are here:

  * PROJECTED, when the input is schedule-like - the same label in the same
    hour-of-week slot at least CONSISTENCY of the time. A tariff block is
    exactly this and projects perfectly.
  * HELD, for the first HOLD_HOURS, when it is not. "Away" or "guests"
    persists for hours even though no slot predicts it, and the near horizon
    is where a device's next few hours are decided.

Beyond that an unprojectable input simply carries no label, and those hours
fall back to the profile - which is the honest answer rather than a confident
wrong one.

Worth knowing before attaching anything: an input that is a PURE function of
weekday and hour is already absorbed by the 168-slot profile, so it explains
nothing on top and will not engage. That is not a failure - it means the
profile already knew. Such an input earns its place only when its schedule
CHANGES (a tariff's seasonal switch), where it lets the profile adapt at once
instead of over weeks. Inputs that pay their way immediately are the ones the
weekly profile cannot see: a price that moves day to day, occupancy, a mode.
"""
from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

MAX_LABELS = 8          # more distinct values than this and it is treated as numeric
BANDS = 4               # quantile bands for a numeric input
MIN_HOURS = 168         # an input seen for less than a week cannot be fitted
HOURS_PER_WEEK = 168
CONSISTENCY = 0.8       # a slot must agree with itself this often to be projected
HOLD_HOURS = 6          # how far an unprojectable input's current label is carried


def _is_number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def label_history(raw: Dict[float, object]) -> Tuple[Dict[float, str], str]:
    """hour key -> raw value, into hour key -> label. Returns the labels and
    how the input was read ("categorical" or "banded")."""
    values = [v for v in raw.values() if v is not None and v != ""]
    if not values:
        return {}, "empty"
    distinct = {str(v) for v in values}
    numeric = all(_is_number(v) for v in values)
    if len(distinct) <= MAX_LABELS:
        return {k: str(v) for k, v in raw.items() if v is not None and v != ""}, "categorical"
    if not numeric:
        # too many distinct strings to be a state, and not numbers either
        return {}, "unusable"
    nums = sorted(float(v) for v in values)
    edges = [nums[int(i * (len(nums) - 1) / BANDS)] for i in range(1, BANDS)]

    def band(x: float) -> str:
        for i, e in enumerate(edges):
            if x < e:
                return f"band {i + 1}"
        return f"band {BANDS}"

    return ({k: band(float(v)) for k, v in raw.items() if v is not None and _is_number(v)}, "banded")


def project(labels: Dict[float, str], horizon_keys: Sequence[float],
            tz_offset_s: float = 0.0, current: Optional[str] = None) -> Dict[float, str]:
    """The horizon's labels: projected where the input is schedule-like, held
    where it is not, absent where neither applies."""
    if not labels:
        return {}
    by_slot: Dict[int, Counter] = {}
    for k, lab in labels.items():
        by_slot.setdefault(_slot(k, tz_offset_s), Counter())[lab] += 1
    firm: Dict[int, str] = {}
    for slot, c in by_slot.items():
        lab, n = c.most_common(1)[0]
        if n >= CONSISTENCY * sum(c.values()):
            firm[slot] = lab
    out: Dict[float, str] = {}
    for i, k in enumerate(sorted(horizon_keys)):
        lab = firm.get(_slot(k, tz_offset_s))
        if lab is not None:
            out[k] = lab
        elif current is not None and i < HOLD_HOURS:
            out[k] = current
    return out


def _slot(key: float, tz_offset_s: float) -> int:
    """(weekday, hour) of an epoch second, in the site's local time."""
    local = key + tz_offset_s
    hour = int(local // 3600)
    # 1970-01-01 was a Thursday, so shift to make Monday 0
    return ((hour // 24 + 3) % 7) * 24 + (hour % 24)


def usable(labels: Dict[float, str]) -> bool:
    """A fit needs a week of hours and more than one label to compare."""
    return len(labels) >= MIN_HOURS and len({v for v in labels.values()}) > 1
