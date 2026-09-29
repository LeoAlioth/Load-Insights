"""The site, as the Energy dashboard already describes it.

Home Assistant's energy preferences are the one place a user has already said
what their site consists of: grid import/export, PV production, battery in/out,
individual metered devices - and, from 2025.12 on, a POWER sensor beside each
energy one (``stat_rate``), from 2026.3 a ``PowerConfig`` for grid and battery
(one signed sensor, an inverted one, or a from/to pair), from 2026.6 the
battery's SOC and capacity. This module turns that dict into a ``SiteModel``
and nothing else: no lookups, no defaults invented, no Home Assistant imports.

Sign convention throughout is the dashboard's: positive = import / discharge,
negative = export / charge. Consumption is
    grid_in - grid_out + solar + battery_out - battery_in
and the unmetered REMAINDER is consumption minus every listed device whose
total is not already inside another listed device (``included_in_stat``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PowerSpec:
    """How a signed power figure is read for a source (grid or battery).

    Exactly the dashboard's ``PowerConfig``: ``rate`` is one signed sensor,
    ``rate_inverted`` the same with the opposite polarity, ``rate_from`` /
    ``rate_to`` a pair (import / export, discharge / charge). A source may
    also carry a bare ``stat_rate`` outside a PowerConfig (2025.12 shape);
    that is read as ``rate``.
    """

    rate: Optional[str] = None
    rate_inverted: Optional[str] = None
    rate_from: Optional[str] = None
    rate_to: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return not any((self.rate, self.rate_inverted, self.rate_from, self.rate_to))

    @property
    def polarity(self) -> Optional[int]:
        """+1 when a positive reading means import, -1 when it means export.

        The dashboard already asks this - none, normal, inverted, or a pair
        of sensors - so it is DECLARED here and should never be guessed at
        (Anze, 2026-09-18, about a SolarEdge M1 that reports export
        positive). A pair needs no polarity: each sensor names its own
        direction, so it answers +1 for the import half.

        None when the source declares no power reading at all, which is when
        the reading's own behaviour has to settle it instead."""
        if self.rate_inverted:
            return -1
        if self.rate or self.rate_from or self.rate_to:
            return 1
        return None

    @property
    def rate_entity(self) -> Optional[str]:
        """The single signed sensor, whichever way up it was declared."""
        return self.rate or self.rate_inverted or None

    @classmethod
    def from_source(cls, src: dict) -> Optional["PowerSpec"]:
        pc = src.get("power_config") or {}
        spec = cls(
            rate=pc.get("stat_rate") or src.get("stat_rate") or None,
            rate_inverted=pc.get("stat_rate_inverted") or None,
            rate_from=pc.get("stat_rate_from") or None,
            rate_to=pc.get("stat_rate_to") or None,
        )
        return None if spec.is_empty else spec


@dataclass(frozen=True)
class Device:
    """One individually metered device from ``device_consumption``."""

    energy: str
    name: Optional[str] = None
    power: Optional[str] = None
    included_in: Optional[str] = None

    @property
    def label(self) -> str:
        return self.name or self.energy


@dataclass(frozen=True)
class SiteModel:
    grid_import: tuple = ()
    grid_export: tuple = ()
    grid_power: tuple = ()
    solar: tuple = ()
    solar_power: tuple = ()
    solar_forecast_entries: tuple = ()
    battery_in: tuple = ()
    battery_out: tuple = ()
    battery_power: tuple = ()
    battery_soc: tuple = ()
    battery_capacity_kwh: Optional[float] = None
    devices: tuple = ()

    # ------------------------------------------------------------------ build
    @classmethod
    def from_prefs(cls, prefs: Optional[dict], ignore: frozenset = frozenset()) -> "SiteModel":
        """Build from ``EnergyManager.data`` (or None when nothing is set up).

        ``ignore`` names devices to leave out: this integration's own
        estimates, which a user may list on the dashboard to see them there,
        but which must never come back in as meters of what they estimate."""
        if not prefs:
            return cls()
        grid_in, grid_out, grid_pw = [], [], []
        solar, solar_pw, forecasts = [], [], []
        batt_in, batt_out, batt_pw, batt_soc = [], [], [], []
        capacity = None

        for src in prefs.get("energy_sources") or []:
            kind = src.get("type")
            if kind == "grid":
                if "flow_from" in src or "flow_to" in src:
                    # Pre-2026 shape: lists of flows, power in a sibling list.
                    grid_in += [f["stat_energy_from"] for f in src.get("flow_from") or [] if f.get("stat_energy_from")]
                    grid_out += [f["stat_energy_to"] for f in src.get("flow_to") or [] if f.get("stat_energy_to")]
                    for p in src.get("power") or []:
                        spec = PowerSpec.from_source(p)
                        if spec:
                            grid_pw.append(spec)
                else:
                    if src.get("stat_energy_from"):
                        grid_in.append(src["stat_energy_from"])
                    if src.get("stat_energy_to"):
                        grid_out.append(src["stat_energy_to"])
                    spec = PowerSpec.from_source(src)
                    if spec:
                        grid_pw.append(spec)
            elif kind == "solar":
                if src.get("stat_energy_from"):
                    solar.append(src["stat_energy_from"])
                if src.get("stat_rate"):
                    solar_pw.append(src["stat_rate"])
                forecasts += list(src.get("config_entry_solar_forecast") or [])
            elif kind == "battery":
                if src.get("stat_energy_to"):
                    batt_in.append(src["stat_energy_to"])
                if src.get("stat_energy_from"):
                    batt_out.append(src["stat_energy_from"])
                spec = PowerSpec.from_source(src)
                if spec:
                    batt_pw.append(spec)
                if src.get("stat_soc"):
                    batt_soc.append(src["stat_soc"])
                if src.get("capacity") is not None:
                    capacity = (capacity or 0.0) + float(src["capacity"])
            # gas / water: not electrical consumption, ignored on purpose

        devices = tuple(
            Device(
                energy=d["stat_consumption"],
                name=d.get("name") or None,
                power=d.get("stat_rate") or None,
                included_in=d.get("included_in_stat") or None,
            )
            for d in prefs.get("device_consumption") or []
            if d.get("stat_consumption") and d["stat_consumption"] not in ignore
        )
        return cls(
            grid_import=tuple(grid_in), grid_export=tuple(grid_out), grid_power=tuple(grid_pw),
            solar=tuple(solar), solar_power=tuple(solar_pw), solar_forecast_entries=tuple(forecasts),
            battery_in=tuple(batt_in), battery_out=tuple(batt_out), battery_power=tuple(batt_pw),
            battery_soc=tuple(batt_soc), battery_capacity_kwh=capacity, devices=devices,
        )

    # ------------------------------------------------------------------ query
    @property
    def has_sources(self) -> bool:
        """Consumption can be formed: at least a grid import figure exists."""
        return bool(self.grid_import)

    def consumption_terms(self) -> tuple:
        """(statistic_id, sign) pairs whose signed sum is site consumption."""
        terms = [(s, 1.0) for s in self.grid_import]
        terms += [(s, -1.0) for s in self.grid_export]
        terms += [(s, 1.0) for s in self.solar]
        terms += [(s, 1.0) for s in self.battery_out]
        terms += [(s, -1.0) for s in self.battery_in]
        return tuple(terms)

    def remainder_devices(self) -> tuple:
        """Devices to subtract for the unmetered remainder: every listed
        device except those whose consumption another LISTED device already
        contains, so a sub-metered heater inside a metered workshop is not
        subtracted twice."""
        listed = {d.energy for d in self.devices}
        return tuple(d for d in self.devices if not (d.included_in and d.included_in in listed))

    def all_statistic_ids(self) -> set:
        ids = {s for s, _ in self.consumption_terms()}
        ids |= {d.energy for d in self.devices}
        return ids

    def summary(self) -> dict:
        """Counts for a config-flow description; names for attributes."""
        return {
            "grid": len(self.grid_import) + len(self.grid_export),
            "solar": len(self.solar),
            "battery": len(self.battery_in) + len(self.battery_out),
            "devices": len(self.devices),
            "forecasts": len(self.solar_forecast_entries),
            "device_names": [d.label for d in self.devices],
        }


# An input's link to a named load rather than to an Energy dashboard device:
# a load is named before anyone could put it on the dashboard.
LOAD_PREFIX = "load:"


def migrate_inputs(options: dict) -> dict:
    """The options as they were before 2026-09-28 - the site's inputs, and
    each device's own under ``device_state_sensors`` - as ONE list of inputs
    in ``input_entities``, each with what it is linked to in ``input_links``
    (Anze, 2026-09-28: "a single storage with the tags only deciding to which
    things it connects to"). Every input counts for the whole site; a link
    adds a device, and that device's parents with it. Unchanged when there
    is nothing to move."""
    legacy = options.get("device_state_sensors")
    if legacy is None:
        return options
    inputs = list(options.get("input_entities") or [])
    links = {k: list(v) for k, v in (options.get("input_links") or {}).items()}
    for device, eids in legacy.items():
        for eid in ([eids] if isinstance(eids, str) else list(eids or [])):
            if eid not in inputs:
                inputs.append(eid)
            if device not in links.setdefault(eid, []):
                links[eid].append(device)
    out = {k: v for k, v in options.items() if k != "device_state_sensors"}
    out["input_entities"], out["input_links"] = inputs, links
    return out


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


def follow_renames(value, renames: dict):
    """``value`` - options, data, anything JSON-shaped - with every string
    that IS a renamed entity id, key or value, swapped for its new id. Home
    Assistant moves an entity's history on a rename but not the settings that
    name it: after Anze's renames of 2026-09-28 every input, link and meter
    here had to be picked again by hand."""
    if isinstance(value, str):
        return renames.get(value, value)
    if isinstance(value, list):
        return [follow_renames(v, renames) for v in value]
    if isinstance(value, tuple):
        return tuple(follow_renames(v, renames) for v in value)
    if isinstance(value, dict):
        return {follow_renames(k, renames): follow_renames(v, renames) for k, v in value.items()}
    return value


# The Energy dashboard's lists that name statistics - what
# EnergyManager.async_update takes.
ENERGY_PREF_KEYS = ("energy_sources", "device_consumption", "device_consumption_water")


def dashboard_renames(prefs: Optional[dict], renames: dict) -> tuple:
    """(the renames the Energy dashboard still names, each of its lists as
    following them leaves it - only the lists that change). Home Assistant
    moves a statistic on a rename but not the dashboard entry naming it, so
    the dashboard goes blank for that device (2026-09-29)."""
    lists = {k: (prefs or {})[k] for k in ENERGY_PREF_KEYS if k in (prefs or {})}
    named = {o: n for o, n in renames.items() if follow_renames(lists, {o: n}) != lists}
    moved = {k: follow_renames(v, named) for k, v in lists.items()}
    return named, {k: v for k, v in moved.items() if v != lists[k]}


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
