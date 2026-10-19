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
from datetime import datetime, tzinfo
from typing import Dict, Optional, Sequence, Tuple

from .profile import slot_of

MAX_LABELS = 8          # more distinct values than this and it is treated as numeric
BANDS = 4               # quantile bands for a numeric input
MIN_HOURS = 168         # an input seen for less than a week cannot be fitted
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
            tz: tzinfo, current: Optional[str] = None) -> Dict[float, str]:
    """The horizon's labels: projected where the input is schedule-like, held
    where it is not, absent where neither applies. Slots are the profile's,
    in the site's zone ``tz`` - an hour of the week on either side of a
    clock change."""
    if not labels:
        return {}
    by_slot: Dict[int, Counter] = {}
    for k, lab in labels.items():
        by_slot.setdefault(slot_of(datetime.fromtimestamp(k, tz)), Counter())[lab] += 1
    firm: Dict[int, str] = {}
    for slot, c in by_slot.items():
        lab, n = c.most_common(1)[0]
        if n >= CONSISTENCY * sum(c.values()):
            firm[slot] = lab
    out: Dict[float, str] = {}
    for i, k in enumerate(sorted(horizon_keys)):
        lab = firm.get(slot_of(datetime.fromtimestamp(k, tz)))
        if lab is not None:
            out[k] = lab
        elif current is not None and i < HOLD_HOURS:
            out[k] = current
    return out


def usable(labels: Dict[float, str]) -> bool:
    """A fit needs a week of hours and more than one label to compare."""
    return len(labels) >= MIN_HOURS and len({v for v in labels.values()}) > 1


# An input's link to a named load rather than to an Energy dashboard device:
# a load is named before anyone could put it on the dashboard.
LOAD_PREFIX = "load:"


def migrate_inputs(options: dict) -> dict:
    """The options as they were before 2026-09-28 - the site's inputs, and
    each device's own under ``device_state_sensors`` - as ONE list of inputs
    in ``input_entities``, each with what it is linked to in ``input_links``
    (2026-09-28: "a single storage with the tags only deciding to which
    things it connects to"). Every input counts for the whole site; a link
    adds a device, and that device's parents with it. Unchanged when there
    is nothing to move."""
    legacy = options.get("device_state_sensors")
    if legacy is None:
        return options
    return add_inputs({k: v for k, v in options.items() if k != "device_state_sensors"},
                      [(eid, device) for device, eids in legacy.items()
                       for eid in ([eids] if isinstance(eids, str) else list(eids or []))])


def relink(options: dict, old: Optional[str], new: Optional[str]) -> dict:
    """The options with every link to the named load ``old`` following it to
    ``new`` - or dropped, when the name was cleared."""
    if not old or old == new:
        return options
    links = {}
    for eid, targets in (options.get("input_links") or {}).items():
        moved = [(LOAD_PREFIX + new if new else None) if t == LOAD_PREFIX + old else t for t in targets]
        links[eid] = list(dict.fromkeys(t for t in moved if t))
    return {**options, "input_links": links}


def scoped(signals, links: dict, within, stat: Optional[str]) -> list:
    """The signals the forecast of ``stat`` takes (2026-10-03). An
    input linked to nothing acts on every forecast. One linked to devices
    acts on those, on every meter they sit inside - ``within(target)``, the
    walk the nowcast's links take - and on the site (``stat`` None), which
    every meter sits inside; on nothing else, the unmetered remainder
    included. Unscoped, the workshop boiler's tank temperature was fitted
    on Blaz's PC and engaged there."""
    return [g for g in signals
            if stat is None or not links.get(g.entity) or any(stat in within(t) for t in links[g.entity])]


def own_scoped(signals, links: dict, within, stat: Optional[str]) -> list:
    """The signals the OWN part of ``stat`` takes (its series less its mature
    parts): the inputs linked to nothing, and those linked to ``stat`` itself.
    One linked to a part belongs to that part. For the site (None) that is
    the unlinked ones alone, as the unmetered remainder always took them."""
    return [g for g in signals
            if not links.get(g.entity) or (stat is not None and any(within(t)[:1] == [stat] for t in links[g.entity]))]


def suggest_inputs(targets: dict, candidates: dict, inputs, own: Optional[dict] = None,
                   target_devices: Optional[dict] = None) -> list:
    """(entity, target) for every candidate sharing an area with a target -
    a dashboard device or a named load - that is not an input yet. Both map
    an id to its area; without one nothing matches (2026-09-29).

    A candidate in ``own`` (entity -> device) is a metering device's own
    reading - a boiler's tank, a battery's cells, a car's cabin - and goes
    only to a target on that same device (``target_devices``, target ->
    device): the boiler's tank temperature is the boiler's best input and
    says nothing about the room it stands in."""
    taken, own, target_devices = set(inputs or ()), own or {}, target_devices or {}
    out = []
    for eid, where in candidates.items():
        if eid in taken:
            continue
        for t, there in targets.items():
            if eid in own:
                if target_devices.get(t) is not None and target_devices.get(t) == own[eid]:
                    out.append((eid, t))
            elif where and there == where:
                out.append((eid, t))
    return out


def add_inputs(options: dict, picks) -> dict:
    """The options with each picked (entity, target): the entity an input,
    linked to the target. Everything else exactly as it was."""
    if not picks:
        return options
    inputs = list(options.get("input_entities") or [])
    links = {k: list(v) for k, v in (options.get("input_links") or {}).items()}
    for eid, target in picks:
        if eid not in inputs:
            inputs.append(eid)
        if target not in links.setdefault(eid, []):
            links[eid].append(target)
    return {**options, "input_entities": inputs, "input_links": links}
