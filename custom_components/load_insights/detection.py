"""Runs the detector on the recorder's raw states, incrementally."""
from __future__ import annotations

import asyncio
import functools
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder import statistics as rec_stats
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ROLE_PREFIX,
    CONF_DETECTION,
    CONF_SINGLE_DEVICE,
    CONF_INPUT_ENTITIES,
    CONF_INVERTERS,
    CONF_INV_INPUT_PREFIX,
    DEFAULT_MIN_EVIDENCE,
    NAMING_MIN_ROWS,
    NAMING_ROWS_PER_NAME,
    NAMING_START_ROWS,
    DETECTION_BACKFILL_DAYS,
    CONF_METER_WAIT,
    DETECTION_INTERVAL_MINUTES,
    DETECTION_SLICE_HOURS,
    SAVE_MAX_INTERVAL_S,
    DOMAIN,
)
from .insights.detect import (
    METER_WAIT_CAP_S,
    SAME_UPDATE_S,
    SWITCH_MEMORY_S,
    combine,
    COMBINE_SETTLE_S,
    PHASES,
    Detector,
    Fleet,
    carries_generation,
    MIN_NOISE_W,
    _as_of,
    _median,
    _sum_series,
    without_window_start,
    names_in_store,
    carries_load,
    unit_scale,
    exports_positive,
    drop_stale_load_override,
    mean_power,
    most_specific,
    quantum_of_steps,
    clears_for_naming,
    offer_for_naming,
)
from .insights.phases import beside, match_meter_entities
from .insights.model import SiteModel
from .insights.named import metered_device, one_device_meters, plan_rewrite

if TYPE_CHECKING:
    from .coordinator import InsightsCoordinator   # which imports this module

_LOGGER = logging.getLogger(__name__)

# the inputs detection reads: what says when a load runs, and what a load's
# runs may follow
SWITCH_DOMAINS = ("binary_sensor", "switch", "input_boolean", "fan", "light", "climate")
NUMBER_DOMAINS = ("sensor", "number", "input_number")
# ...and what reports a device's setting: a select, a fan's speed, and any
# sensor whose states are words (a washer's cycle phase)
STATE_DOMAINS = ("select", "input_select", "fan")


def load_uid(entry_id: str, kind: str, name: str) -> str:
    """A named load's entity's unique id - ``kind`` "power" or "energy"."""
    return f"{entry_id}_load_{kind}_{name.lower().replace(' ', '_')}"


def device_uid(entry_id: str, energy: str) -> str:
    """A dashboard device's forecast sensor's unique id, from its statistic id."""
    return f"{entry_id}_device_{energy.replace('.', '_')}"


@dataclass
class RuntimeData:
    """What a loaded entry runs - its ConfigEntry.runtime_data."""
    coordinator: "InsightsCoordinator"
    runner: "DetectionRunner"
    site_device_id: str


def runner_of(entry: Optional[ConfigEntry]) -> Optional["DetectionRunner"]:
    """The entry's detection runner, or None while it is not loaded."""
    data = getattr(entry, "runtime_data", None)
    return data.runner if data is not None else None


def named_load_energy(hass: HomeAssistant, entry: ConfigEntry, name: str) -> Optional[str]:
    """The entity - and so the statistic - a named load's energy is under: a
    load that IS a metered device is under that device's own meter."""
    runner = runner_of(entry)
    meter = runner.metered_device(name) if runner is not None else None
    if meter:
        return runner.submeters[meter]["energy"]
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, load_uid(entry.entry_id, "energy", name))
STORAGE_VERSION = 1
# The DETECTOR's generation, separate from the store's format version: when
# the algorithm changes shape, what it learned before is not comparable with
# what it learns now, so the library is dropped and the backfill re-run.
# 2 = sessions are paired edges rather than excursions above the idle floor.
# 3 = the hour and weekday histograms hold ENERGY, not counts of starts.
# 4 = reactive power comes from one meter's own power, voltage and current,
#     and the step is measured against a median rather than a slow EMA, so
#     every stored power factor was derived differently from today's.
# 5 = readings are scaled by their UNIT, so a meter publishing kW is no
#     longer read as watts - which changes what every submeter saw, and so
#     where loads are placed and what they were classified as.
# 6 = every reading now carries its measured RESOLUTION as well as its
#     noise, and nothing is derived past what a sensor can express: the
#     step floor, the power factor and the energy answer are all gated on
#     it, so steps, factors and placements all differ from generation 5.
# 7 = a power factor carries how far wrong it could be, and that error bar -
#     not a threshold - decides whether it constrains a match or reaches the
#     classifier. Every stored factor was kept under the old rule.
# 8 = power_mad is PER PHASE, not the deviation of the total, so every
#     stored spread is three times too large on a three-phase load - and the
#     relative-noise floor is derived from each phase's measured noise
#     rather than a flat 300 W, which changes which steps were seen at all.
# 9 = a level must hold for SUSTAIN_INTERVALS of the reading's own measured
#     sample interval, so transitional samples no longer found levels, and a
#     merge may admit ALIKE_MAD_SHARE of the pair's spread. Both change which
#     sessions and signatures exist at all.
# 10 = a summed house reading keeps only the last of each burst of readings
#     (COMBINE_SETTLE_S), so a third of Home's sessions - built on phantom
#     sums against a stale partner - no longer exist.
# 11 = a reading's interval is the MEDIAN of its recent gaps, its cadence,
#     not a running mean of the gaps between recorded changes - which gave
#     one meter's quiet phase a longer interval than its busy ones.
# 12 = phases are walked in time order and one leg of a multi-phase load may
#     vouch for another's stop, so short off-gaps no longer glue one leg's
#     pulses together - which changes which sessions exist.
# 13 = a new level is the readings that AGREE, dated at the first one past
#     half-way; a leg's start that swallowed a coincident load is split when
#     its partner leg closes; and the recorder's start-of-window copy is no
#     longer fed in as a reading, once a minute on every phase.
# 14 = a three-phase sub-meter's channels are mapped onto the grid
#     connection's phases from the data, and a one-device meter decides which
#     signature a matched session joins - so which sessions share a signature,
#     and where signatures are placed, both change.
# 15 = edges are clustered by where their sizes pile up, pairs and links are
#     accepted above chance, and runs are filed by device rather than by how
#     alike their power looks: every cluster, pair and signature differs.
# 16 = a start is an all-phase event: rises on several phases within the
#     window are one cluster carrying a phase pattern, a device is its start
#     cluster (the link table is gone), and a run is filed on its own phases
#     only - every start cluster, device and signature differs.
# 17 = a step's place is part of its kind (the innermost meter that saw all
#     of it), a house step its meters stepped with is booked as their shares,
#     a run bigger than the whole reading is closed, and noise is learned from
#     how the reading moves - every cluster, run and signature differs.
# 18 = a meter's step is its own detector's, weighed against the grid's over
#     both spans; a level is confirmed, a silence held and a span started over
#     three of the meter's cadence (how often it writes while its value
#     moves); only a start is placed; a step no meter placed never joins a
#     meter that held its value - which steps are declared, and where every
#     start cluster sits, differ.
DETECTOR_GENERATION = 18
MIN_COUNT_TO_NAME = 2          # a load seen once is not offered for naming
# What a load has actually USED is the reason to bother naming it: a
# signature worth 30 Wh over ten days is noise with a shape, and a list full
# of those is why the naming page ran to a hundred and eighty rows.
NAMING_MIN_WH = 50.0
# How many of a current reading's own changes to remember while confirming
# what it can resolve. A pass covers one minute, so the evidence has to be
# gathered across them or it is never gathered at all.
AMP_STEP_MEMORY = 600
# How soon the next pass follows one that is not caught up - a backfill slice
CATCH_UP_PASS_S = 2.0


def energy_site(hass: HomeAssistant, prefs) -> SiteModel:
    """The Energy dashboard's site AS DETECTION READS IT: less anything this
    integration publishes.

    A detected load's energy sensor can be listed there - the kiln, under
    the meter it sits in - and the forecasts welcome it like any device. But
    detection takes every listed device as a meter, and read back as one the
    kiln's estimate would be fed in as a measurement, with the kiln then
    detected inside it (Anze, 2026-09-28: "make sure that the generated
    detected entities do not back feed to any detection as if they were
    actual measurements"). Read back as a sub-meter a load's estimate became
    its own truth - and naming a load after it made the load "that metered
    device", so it got no sensors at all (Home and Kozolec, 2026-09-30, after
    the full reset left Peč za Glino, Kompresor, Fridges... on the dashboard)."""
    own = frozenset(e.entity_id for e in er.async_get(hass).entities.values() if e.platform == DOMAIN)
    return SiteModel.from_prefs(prefs, ignore=own)


class DetectionRunner(DataUpdateCoordinator[None]):
    """Every DETECTION_INTERVAL_MINUTES, read what the meter has recorded since
    the last processed instant and feed it to the detector. The first run
    backfills the recorder's window in slices, one per call, so no single
    query is large; the detector's state, sessions and signatures persist in
    .storage so a restart resumes where it stopped.

    A coordinator for its schedule and its listeners, never for data: a pass
    tells the entities itself (async_update_listeners) at the moment it has
    something to say - before a finished re-read stops being one, which the
    named loads' energy reads - and always_update=False keeps the
    coordinator from telling them again after every refresh."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} detection",
                         update_method=self._run, always_update=False)
        self.entry = entry
        # Meters below the main one come from the Energy dashboard, resolved once
        # per run: {name: {"fields": {...}, "agnostic": bool, "parent": name|None}}
        self.submeters: Dict[str, dict] = {}
        self.fleet: Fleet = Fleet()
        # V x dI per phase: the apparent power one quantum of the amps
        # behind this role's power factor is worth. Measured, never set.
        self.q_quantum: Dict[str, float] = {}
        self._amp_steps: Dict[str, List[float]] = {}
        self.solar: List[Dict[str, str]] = []   # each array's power per phase
        # whether the configured reading actually includes the array, read
        # off the data per phase and remembered once it is conclusive
        self.pv_visible: Dict[str, bool] = {}
        # how many meter readings the runs have actually had to work with,
        # so "no loads found" can be told from "no data"
        self.samples_read: int = 0
        # Mean watts per NAMED load over the last stretch of data processed,
        # from the energy that stretch added. See _update_average_power.
        self.average_power: Dict[str, float] = {}
        self._energy_mark: Dict[str, float] = {}
        self._mark_ts: Optional[float] = None
        self._saved_at: Optional[float] = None
        self.last_processed: Optional[datetime] = None
        self.caught_up = False
        # re-reading history from scratch - after a reset, or with nothing
        # stored: what the library gains is old, not new energy
        self.refiling = False
        self.last_run: Optional[datetime] = None
        # seconds the last pass spent reading the recorder and detecting, and
        # the hours it covered: where a backfill's time goes
        self.last_pass: Dict[str, float] = {}
        self.reactive_from: Dict[str, str] = {}      # phase -> what its reactive power was read from
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.detection")
        # held by a pass, and by whatever must not run under one: a reset, a
        # rename, a statistics rewrite - each waits for the pass to finish
        self._lock = asyncio.Lock()

    @property
    def config(self) -> dict:
        # Cleaned on the way out rather than migrated in place: the stale copy
        # is harmless in storage and the fix has to hold for an entry written
        # by an older version that nobody re-saves. See
        # drop_stale_load_override for what it removes and why.
        return drop_stale_load_override(dict(self.entry.options.get(CONF_DETECTION) or {}))

    async def _resolve_submeters(self) -> Dict[str, dict]:
        """Every individually metered device the Energy dashboard lists, with
        the per-phase readings of the Home Assistant device behind it.

        The dashboard supplies the identity and the nesting
        (``included_in_stat``); it holds only an energy statistic and, at
        best, one total power sensor, so the PHASES have to come from the
        device registry - which is what the meter matcher does. A device
        whose hardware publishes no power at all cannot take part: hourly
        energy is far too coarse to line up with a session."""
        manager = await async_get_manager(self.hass)
        site = energy_site(self.hass, manager.data)
        registry = er.async_get(self.hass)
        by_stat = {d.energy: d for d in site.devices}
        declared = self.config.get(CONF_SINGLE_DEVICE)
        declared = None if declared is None else set(declared)
        out: Dict[str, dict] = {}
        for dev in site.devices:
            entry = registry.async_get(dev.energy)          # a recorder statistic id IS the entity id
            # Not an entity that no longer exists: its recorded history is only
            # what it was. (Not one of our own named-load sensors either - see
            # energy_site, which has already left those out.)
            if entry is None and self.hass.states.get(dev.energy) is None:
                continue
            fields: Dict[str, str] = {}
            if entry is not None and entry.device_id:
                fields = match_meter_entities(self._device_rows(registry, entry.device_id))
            phases = [p for p in PHASES if fields.get(f"power_{p}")]
            agnostic = False
            if len(phases) < 2:
                # no per-phase breakdown: the dashboard's own power sensor (or
                # the single one found) stands in, and matching ignores phases
                total = dev.power or (fields.get(f"power_{phases[0]}") if phases else None)
                if not total:
                    continue
                fields = {"power_a": total}
                agnostic = True
            parent = by_stat.get(dev.included_in or "")
            out[dev.label] = {"fields": fields, "agnostic": agnostic,
                              "parent": parent.label if parent else None, "energy": dev.energy,
                              # None: not declared, the library's shape decides
                              "single": None if declared is None else dev.energy in declared}
        return out

    def _device_rows(self, registry, device_id: str) -> list:
        """A device's sensors in the shape the meter matcher reads, INCLUDING
        those of the devices that hang off it.

        A Shelly Pro 3EM is one device per PHASE plus a parent carrying the
        totals, each phase pointing at the parent with via_device_id. The
        Energy dashboard names the parent - that is where the energy
        statistic lives - so reading only the parent's own entities found a
        single total and nothing else, and a three-phase meter was taken for
        a one-phase one. It is the commonest three-phase meter there is
        (Anze's attic and grid meters are both this, 2026-09-22)."""
        ids = [device_id]
        try:
            dev_reg = dr.async_get(self.hass)
            ids += [d.id for d in dev_reg.devices.values() if d.via_device_id == device_id]
        except Exception:                        # a registry we cannot read is not fatal
            pass
        rows = []
        for e in [x for i in ids
                  for x in er.async_entries_for_device(registry, i, include_disabled_entities=False)]:
            if e.domain != "sensor":
                continue
            st = self.hass.states.get(e.entity_id)
            rows.append({
                "entity_id": e.entity_id,
                "device_class": e.device_class or e.original_device_class or (st.attributes.get("device_class") if st else None),
                "name": e.name or e.original_name or "",
            })
        return rows

    async def _inverter_terms(self, start: datetime, end: datetime) -> List[tuple]:
        """Each inverter as (rows, sign) per phase: its output, less its input.

        An inverter contributes what it ADDS. A PV string inverter has no AC
        input and contributes its whole output; a hybrid with the grid
        flowing through it contributes the difference, so the grid it passed
        on is not counted a second time. Nothing here consults a wiring flag,
        because the sum telescopes either way."""
        out: List[tuple] = []
        for inv in self.entry.options.get(CONF_INVERTERS) or []:
            for prefix, sign in (("", 1.0), (CONF_INV_INPUT_PREFIX, -1.0)):
                cfg = {f"power_{p}": inv.get(f"{prefix}power_{p}") for p in PHASES}
                total = inv.get(f"{prefix}power")
                if total and not any(cfg.values()):
                    # a machine that publishes one figure: a three-phase
                    # inverter is symmetric, so its legs are its total in
                    # thirds - which reproduced Anze's own templates exactly
                    cfg = {f"power_{p}": total for p in PHASES}
                    share = 1.0 / len(PHASES)
                else:
                    share = 1.0
                cfg = {k: v for k, v in cfg.items() if v}
                if not cfg:
                    continue
                rows, _ = await self._read(start, end, cfg)
                out.append((rows, sign * share))
        return out

    async def _derive_load(self, start: datetime, end: datetime) -> Dict[str, list]:
        """What the house drew, per phase, from the grid and the inverters.

            house = grid meter + SUM over inverters of (output - input)

        There is no load reading to configure because there is nothing to
        configure: given the grid and the inverters, the house is determined
        (Anze, 2026-09-18 - "just have each inverter entry have both inputs
        and outputs configurable"). Whatever sits between the utility meter
        and an inverter's own AC input falls out of the same sum, because a
        hybrid measures that input itself.

        An explicit reading still wins where someone has one that already IS
        the house - a dedicated CT, or the template sensors Anze built before
        this existed."""
        override = {f"power_{p}": self.config.get(f"power_{p}") for p in PHASES}
        if any(override.values()):
            samples, _ = await self._read(start, end, {k: v for k, v in override.items() if v})
            return samples

        terms: List[tuple] = []
        grid_cfg = {f"power_{p}": self.config.get(f"{ROLE_PREFIX['grid']}power_{p}") for p in PHASES}
        grid_cfg = {k: v for k, v in grid_cfg.items() if v}
        grid_rows: Dict[str, list] = {}
        if grid_cfg:
            grid_rows, _ = await self._read(start, end, grid_cfg)
        inverters = await self._inverter_terms(start, end)
        if grid_rows:
            generation = {}
            for rows, sign in inverters:
                if sign <= 0:
                    continue
                for p, series in rows.items():
                    generation[p] = _sum_series(generation.get(p, []), series)
            sign = await self._grid_sign(grid_rows, generation)
            terms.append((grid_rows, sign))
        terms.extend(inverters)
        if not terms:
            return {}

        out: Dict[str, list] = {}
        for p in PHASES:
            per_phase = [(rows[p], sign) for rows, sign in terms if rows.get(p)]
            if per_phase:
                out[p] = combine(per_phase, settle_s=COMBINE_SETTLE_S)
        return {p: rows for p, rows in out.items() if rows}

    async def _grid_sign(self, grid_rows: Dict[str, list],
                         generation: Optional[Dict[str, list]]) -> float:
        """+1 where the grid meter reads import positive, -1 where it does
        not - because "the house is the meter plus the inverter" is true only
        in the first convention, and Anze's SolarEdge M1 is the second.

        Declared beats derived: the Energy dashboard's grid source asks this
        outright (none, normal, inverted, or a pair of sensors), so where it
        has been answered we use the answer. The dashboard names a TOTAL
        power sensor while detection runs per phase, so taking its polarity
        for the phases is an assumption - a safe one on one device, and
        exports_positive stays as the cross-check for a site that has left
        the power config empty."""
        for spec in await self._site_grid_power():
            if spec.polarity is not None:
                return float(spec.polarity)
        if generation:
            for phase, rows in grid_rows.items():
                verdict = exports_positive(rows, generation.get(phase) or [])
                if verdict is not None:
                    self._exports_positive = verdict
                    break
        # the last pass that could tell, where this one is too short to
        exports = getattr(self, "_exports_positive", None)
        return -1.0 if exports else 1.0

    async def _site_grid_power(self) -> list:
        try:
            manager = await async_get_manager(self.hass)
            return list(energy_site(self.hass, manager.data).grid_power)
        except Exception:  # noqa: BLE001 - a missing dashboard is not an error
            return []

    async def _resolve_solar(self) -> List[Dict[str, str]]:
        """Each array's power, per phase where the inverter publishes it.

        A grid meter carries the house MINUS the array, so every cloud is a
        step on it and would be filed as a load switching. An inverter
        usually gives its own per-phase power, which makes that test exact;
        failing that the dashboard's solar power sensor, or the inverter's
        total, stands in for every phase.

        One entry PER SOURCE, because a site with two trackers has two of
        them (Kozolec has two MPPTs) and reading only the first would leave
        half of every cloud unexplained."""
        # An inverter set up on the Inverters page WINS: the Energy dashboard
        # lists what produces energy, not where it is wired, and a DC-coupled
        # array charging the battery through an MPPT never shows on the AC
        # side at all - so at Kozolec there is nothing for the dashboard's
        # view to find (Anze, 2026-09-18).
        configured = self.entry.options.get(CONF_INVERTERS) or []
        if configured:
            out: List[Dict[str, str]] = []
            for inv in configured:
                per_phase = {f"power_{p}": inv.get(f"power_{p}") for p in PHASES
                             if inv.get(f"power_{p}")}
                if len(per_phase) >= 2:
                    fields = per_phase
                elif inv.get("power") or per_phase:
                    total = inv.get("power") or next(iter(per_phase.values()))
                    fields = {f"power_{p}": total for p in PHASES}
                else:
                    continue
                if fields not in out:
                    out.append(fields)
            if out:
                return out
        manager = await async_get_manager(self.hass)
        site = energy_site(self.hass, manager.data)
        if not site.solar:
            return []
        registry = er.async_get(self.hass)
        spare = list(site.solar_power)
        out: List[Dict[str, str]] = []
        for stat in site.solar:
            found: Dict[str, str] = {}
            entry = registry.async_get(stat)
            if entry is not None and entry.device_id:
                matched = match_meter_entities(self._device_rows(registry, entry.device_id))
                found = {k: v for k, v in matched.items() if k.startswith("power_")}
            if len([p for p in PHASES if found.get(f"power_{p}")]) >= 2:
                fields = found
            else:
                total = found.get("power_a") or (spare.pop(0) if spare else None)
                fields = {f"power_{p}": total for p in PHASES} if total else {}
            if fields and fields not in out:
                out.append(fields)
        return out

    @property
    def detector(self) -> Detector:
        return self.fleet.main

    @property
    def parents(self) -> Dict[str, Optional[str]]:
        """Meter -> the meter it sits inside, from included_in_stat."""
        return {name: m["parent"] for name, m in self.submeters.items()}

    async def _read_inputs(self, start: datetime, end: datetime):
        """The pass's inputs, with SWITCH_MEMORY_S before it so a run that
        started a slice ago still has its start. A switch as its on-periods,
        [(on, off or None while still on)]: a climate entity is on while its
        hvac_action says it is doing something (a thermostat's heating),
        anything else while its state is "on", and a period only open because
        the window starts there is not a switch-on. A number as its readings,
        [(ts, value)], and a setting as its values, [(ts, "Wash")] - a fan's
        by its speed - each with the one in force at the window's start."""
        switches: Dict[str, list] = {}
        numbers: Dict[str, list] = {}
        inputs: Dict[str, list] = {}
        since = start - timedelta(seconds=SWITCH_MEMORY_S)
        # every input the forecast is given, whatever it is linked to (Anze,
        # 2026-09-28: add the thermostat to the inputs first, and move it to
        # the device once the load it helped find is named)
        for eid in self.entry.options.get(CONF_INPUT_ENTITIES) or []:
            domain = eid.split(".", 1)[0]
            if domain not in SWITCH_DOMAINS and domain not in NUMBER_DOMAINS and domain not in STATE_DOMAINS:
                continue
            if domain in ("climate", "fan"):
                # Read by an ATTRIBUTE - a thermostat's hvac_action, a fan's
                # speed - which changes while the state does not: a thermostat
                # stays "heat" all winter. state_changes_during_period returns
                # state changes only, and so gave the floor mat's thermostat
                # not one switch-on in production (2026-09-28).
                states = await get_instance(self.hass).async_add_executor_job(functools.partial(
                    history.get_significant_states, self.hass, since, end, [eid],
                    include_start_time_state=True, significant_changes_only=False))
            else:
                states = await get_instance(self.hass).async_add_executor_job(
                    history.state_changes_during_period, self.hass, since, end, eid, True, False, None, True,
                )
            rows = [st for st in states.get(eid, []) if st.state not in ("unknown", "unavailable")]
            if domain in NUMBER_DOMAINS:
                values = []
                for st in rows:
                    try:
                        values.append((st.last_updated.timestamp(), float(st.state)))
                    except ValueError:
                        pass
                if values:
                    numbers[eid] = values
                elif domain == "sensor" and rows:
                    inputs[eid] = [(st.last_updated.timestamp(), st.state) for st in rows]
                continue
            if domain in STATE_DOMAINS:
                inputs[eid] = [(st.last_updated.timestamp(),
                                st.state if domain != "fan" else
                                (f"{st.attributes.get('percentage')} %" if st.state == "on" else "off"))
                               for st in rows]
            if domain not in SWITCH_DOMAINS:
                continue
            spans, on = [], None
            for st in rows:
                is_on = (st.attributes.get("hvac_action") not in (None, "idle", "off")) if domain == "climate" else st.state == "on"
                t = st.last_updated.timestamp()
                if is_on and on is None:
                    on = t
                elif not is_on and on is not None:
                    spans.append((on, t))
                    on = None
            if on is not None:
                spans.append((on, None))
            switches[eid] = [(a, b) for a, b in spans if a > since.timestamp() + 1.0]
        return switches, numbers, inputs

    def holds_one_device(self, name: str) -> bool:
        """The user's answer for this meter, or else the default the settings
        page shows: a meter with others nested inside it on the Energy
        dashboard holds several by definition (Home's Delavnica, Kozolec's
        Inverter), anything else as its library suggests."""
        meter = self.submeters.get(name) or {}
        if meter.get("single") is not None:
            return meter["single"]
        return self.guess_one_device(name)

    def metered_device(self, name: str) -> Optional[str]:
        """The meter a named load IS - named after one that holds a single
        device - or None. See insights.named."""
        return metered_device(name, self.device_meters())

    def device_meters(self) -> List[str]:
        """The meters detection reads that hold one device, by name."""
        return one_device_meters({m: self.holds_one_device(m) for m in self.submeters})

    async def async_first_statistic(self, statistic_id: str) -> Optional[float]:
        """When a statistic's first long-term row starts (epoch s), or None
        while it has none. Every row is read to find it: a named load's meter
        is weeks old, not years."""
        rows = await get_instance(self.hass).async_add_executor_job(
            rec_stats.statistics_during_period, self.hass, datetime.fromtimestamp(0, timezone.utc), None,
            {statistic_id}, "hour", None, {"sum"})
        rows = rows.get(statistic_id)
        return float(rows[0]["start"]) if rows else None

    async def async_backfill_statistics(self, name: str) -> Optional[Tuple[int, float]]:
        """Write what detection saw of a named load, hour by hour, over its
        energy meter's statistics - long-term and five-minute alike - for the
        hours detection watched, about ten days: the days before it was named
        show on the Energy dashboard, and a reset or the reading's first
        appearance no longer shows as a day's worth in one hour (Anze,
        2026-09-29: "rewrite the long term and short term statistics for
        detected entities for the time it has the data for"). See
        plan_rewrite.

        Returns (hours written, kWh they differ from what was recorded) -
        (0, 0.0) when there is nothing to do - or None while the meter has no
        hour of its own yet. Running it again writes the same."""
        meter = self.metered_device(name)
        if meter:
            _LOGGER.info("Not backfilling %s: it is the metered device %s, whose own readings are its history",
                         name, meter)
            return 0, 0.0
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, load_uid(self.entry.entry_id, "energy", name))
        if entity_id is None:
            return 0, 0.0
        async with self._lock:                # the library, between passes: a pass changes it
            if await self.async_first_statistic(entity_id) is None:
                return None
            seen = min((h for sig in self.detector.signatures for h in sig.hourly), default=None)
            if seen is None:
                return 0, 0.0
            covered = int(seen // 3600 * 3600) + 3600     # the first hour read is a part of one
            recorder = get_instance(self.hass)
            meta = await recorder.async_add_executor_job(
                functools.partial(rec_stats.get_metadata, self.hass, statistic_ids={entity_id}))
            unit = meta[entity_id][1]["unit_of_measurement"] if entity_id in meta else None
            if unit != UnitOfEnergy.KILO_WATT_HOUR:
                _LOGGER.warning("Not backfilling %s: %s keeps its statistics in %s, not kWh", name, entity_id, unit)
                return 0, 0.0

            def recorded(period, since):
                return recorder.async_add_executor_job(
                    rec_stats.statistics_during_period, self.hass, dt_util.utc_from_timestamp(since), None,
                    {entity_id}, period, None, {"state", "sum"})
            hours = (await recorded("hour", 0)).get(entity_id) or []
            fives = (await recorded("5minute", covered)).get(entity_id) or []
            hour_rows, five_rows, shift = plan_rewrite(self.detector.hourly_by_name(name), covered, hours, fives)
            if not hour_rows:
                return 0, 0.0
            # exactly what the sensor's own statistics carry, so importing
            # changes nothing about them but the rows
            metadata = {"has_sum": True, "mean_type": StatisticMeanType.NONE, "name": None, "source": "recorder",
                        "statistic_id": entity_id, "unit_class": "energy", "unit_of_measurement": unit}
            for table, rows in ((Statistics, hour_rows), (StatisticsShortTerm, five_rows)):
                recorder.async_import_statistics(metadata, [
                    {"start": dt_util.utc_from_timestamp(r["start"]), "state": r["state"], "sum": r["sum"]}
                    for r in rows], table)
            # the hour being recorded, and any compiled since the rows were read
            recorder.async_adjust_statistics(
                entity_id, dt_util.utc_from_timestamp(hour_rows[-1]["start"] + 3600), shift, unit)
            await recorder.async_block_till_done()   # written before another run reads
        _LOGGER.info("Rewrote %s (%s): %d hours and %d five-minute rows from %s, %.2f kWh %s than recorded",
                     name, entity_id, len(hour_rows), len(five_rows),
                     dt_util.utc_from_timestamp(hour_rows[0]["start"]).isoformat(), abs(shift),
                     "more" if shift >= 0 else "less")
        return len(hour_rows), shift

    async def _rewrite_named(self) -> None:
        for name in sorted(self.detector.names()):
            await self.async_backfill_statistics(name)

    def guess_one_device(self, name: str) -> bool:
        if any(m.get("parent") == name for m in self.submeters.values()):
            return False
        return self.fleet.guess_one_device(name)

    def naming_groups(self) -> list:
        """What is waiting to be named, one group per meter: ``(meter, offered,
        waiting)``, the loads under no meter first as ``"main"``, then the
        meters by how much their loads use.

        Split by the deepest meter that saw each load, so the page can be
        taken one circuit at a time (Anze, 2026-09-25: "any way of splitting
        the detected loads early would help with organisation and naming").
        Until then only the loads under no meter were offered at all, and a
        circuit meter's - Hiša's, Mansarda's - could not be named. A load seen
        a single time may not be a load at all, and naming it teaches the
        library nothing (Anze, 2026-09-17: "as for signatures only seen once,
        dont show them"). Named loads have a page of their own (``named``).
        Each group gets the length offer_for_naming earns it, and ``waiting``
        is how many more cleared the bar than that length fits."""
        is_heir = lambda i: self.detector.predecessor_of(i) is not None  # noqa: E731
        named = len(self.detector.names())
        out = []
        for where, worth in self._worth().items():
            if where != "main" and self.holds_one_device(where):
                # its own readings ARE that device: nothing in it to name
                # (Anze, 2026-09-28: "we just use the measured data off it")
                continue
            shown = offer_for_naming(worth, named, DEFAULT_MIN_EVIDENCE, NAMING_MIN_ROWS,
                                     NAMING_START_ROWS, NAMING_ROWS_PER_NAME, is_heir=is_heir)
            if shown:
                clear = clears_for_naming(worth, DEFAULT_MIN_EVIDENCE, NAMING_MIN_ROWS, is_heir)
                out.append((where, shown, max(0, len(clear) - len(shown))))
        out.sort(key=lambda g: (g[0] != "main", -sum(x.energy_wh for x in g[1])))
        return out

    def named(self) -> list:
        """Every named load, biggest first, wherever it was seen."""
        return sorted((x for x in self.detector.signatures if x.name), key=lambda x: -x.energy_wh)

    def _worth(self) -> Dict[str, list]:
        """Signatures a person could name, by the meter they belong to: not
        named yet, seen more than once, and having used enough to be worth
        the trouble."""
        parents = self.parents
        # biggest first, by energy: what a load COSTS is the reason to name
        # it, and it puts the ones worth the trouble at the top
        # Energy alone put an anonymous 600 W something above a machine that
        # runs every Saturday at noon. Rank by what a person can actually act
        # on: what it costs, weighted by whether the row says enough to
        # recognise it (Anze, 2026-09-18).
        def rank(s):
            return -(s.energy_wh * (0.45 + 0.55 * s.recognisable))
        groups: Dict[str, list] = {}
        for s in sorted(self.detector.signatures, key=lambda x: (rank(x), -x.evidence)):
            if s.name:
                continue
            if ((s.count >= MIN_COUNT_TO_NAME and s.energy_wh >= NAMING_MIN_WH)
                    # a load that may be what a NAMED one became belongs on
                    # the list whatever its size: the offer to move the name
                    # is the whole reason to open it
                    or self.detector.predecessor_of(s.id) is not None):
                groups.setdefault(most_specific(s.locations, s.count, parents), []).append(s)
        return groups

    async def async_adopt(self, signature_id: int) -> Optional[str]:
        """Move a predecessor's name onto this signature, and persist."""
        name = self.detector.adopt(signature_id)
        if name is None:
            return None
        await self._persist(force=True)
        self.async_update_listeners()
        return name

    async def async_rename(self, signature_id: int, name: Optional[str]) -> bool:
        """Name a signature (or clear it) and persist at once - the caller
        bumps the entry so the entities follow."""
        if not self.detector.rename(signature_id, name):
            return False
        await self._persist(force=True)          # a user action, written at once
        self.async_update_listeners()
        return True

    async def _persist(self, force: bool = False) -> None:
        """Write the state, but not on every pass.

        The snapshot is the whole library and runs to well over a hundred
        kilobytes; written every five minutes that is tens of megabytes a day,
        and it is the single thing that would multiply if detection ran every
        minute instead (Anze, 2026-09-18, whose suggestion this is: keep it in
        memory and write it at most hourly).

        async_delay_save alone will not do it. Called again before its delay
        expires it POSTPONES the pending write rather than letting it run, so
        a pass every minute against an hourly delay would never write at all
        until shutdown - and an ungraceful one would lose everything since the
        last write. So the delayed save is the safety net (it also registers
        Home Assistant's final-write listener, which is what makes a clean
        shutdown durable) and the elapsed check is the floor.
        """
        now = dt_util.utcnow().timestamp()
        due = self._saved_at is None or now - self._saved_at >= SAVE_MAX_INTERVAL_S
        if force or due:
            await self._store.async_save(self._snapshot())
            self._saved_at = now
        else:
            self._store.async_delay_save(self._snapshot, SAVE_MAX_INTERVAL_S)

    def _snapshot(self) -> dict:
        return {"fleet": self.fleet.to_dict(),
                "last_processed": self.last_processed.isoformat() if self.last_processed else None,
                "refiling": self.refiling,
                "generation": DETECTOR_GENERATION}

    @property
    def behind_s(self) -> Optional[float]:
        """How far behind now the detector has read, in seconds.

        None before it has read anything at all. The backfill walks ten days
        in six-hour slices, so a library part way through that is a library
        of whatever happened to be in the first few days - which is not a
        thing to offer anyone for naming (Anze, 2026-09-18)."""
        if self.last_processed is None:
            return None
        return max(0.0, (dt_util.utcnow() - self.last_processed).total_seconds())

    @property
    def meter_wait_s(self) -> float:
        """At most how long the grid's steps wait for the meters below it -
        see METER_WAIT_CAP_S; as configured or defaulted, never negative."""
        try:
            value = float(self.config.get(CONF_METER_WAIT, METER_WAIT_CAP_S))
        except (TypeError, ValueError):
            return METER_WAIT_CAP_S
        return max(0.0, value)

    @property
    def enabled(self) -> bool:
        """Enough to work out what the house draws: a reading that already is
        the house, or a grid meter, or an inverter."""
        if any(self.config.get(f"power_{p}") for p in PHASES):
            return True
        if any(self.config.get(f"{ROLE_PREFIX['grid']}power_{p}") for p in PHASES):
            return True
        return any(inv.get("power") or any(inv.get(f"power_{p}") for p in PHASES)
                   for inv in (self.entry.options.get(CONF_INVERTERS) or []))

    async def async_start(self) -> None:
        raw = await self._store.async_load() or {}
        orphans: list = []
        if raw and raw.get("generation") != DETECTOR_GENERATION:
            # The library was learned by a detector that no longer exists, so
            # it goes. The NAMES do not: they are the user's, not ours, and
            # dropping them behind a log line would take their devices, their
            # energy meters and their place on the Energy dashboard with them
            # - on every installation at once, the first time this constant
            # moves (Anze asked what an update does to a named load,
            # 2026-09-22). Read defensively: the whole point is that the old
            # shape may not be one we still understand.
            orphans = names_in_store(raw)
            _LOGGER.info(
                "Load detection was learned by an older detector; starting its "
                "library again, keeping %d name(s) to re-attach", len(orphans)
            )
            raw = {}
        if raw.get("fleet"):
            self.fleet = Fleet.from_dict(raw.get("fleet"))
        else:                                   # a store written before downstream meters existed
            self.fleet = Fleet(main=Detector.from_dict(raw.get("detector")))
        if orphans:
            self.fleet.main.carry_names(orphans)
        self.fleet.main.tz_offset_s = dt_util.now().utcoffset().total_seconds()
        # before the platforms, which leave out the loads that ARE a metered
        # device; every pass resolves them again
        self.submeters = await self._resolve_submeters()
        lp = raw.get("last_processed")
        self.last_processed = dt_util.parse_datetime(lp) if lp else None
        self.refiling = bool(raw.get("refiling")) or self.last_processed is None
        if not self.enabled:
            return
        self.update_interval = timedelta(minutes=DETECTION_INTERVAL_MINUTES)
        self.hass.async_create_task(self.async_refresh())

    async def async_reset(self, forget_names: bool = False) -> None:
        """Forget everything learned and start the backfill again.

        Everything except the NAMES, unless ``forget_names``. They are the
        one thing in the library the user put there by hand, and the backfill
        re-learns everything else in minutes. Each is carried across as a
        description and handed back to the first rebuilt signature that looks
        like it; where the site really has changed - the reason to do this by
        hand - nothing matches and the name does not return. Anze
        (2026-09-30) would rather drop them than have a stale one land on the
        wrong load, so the service can forget them too.

        Between passes, never under one: a pass running when this lands
        finishes by recording that it has processed up to now, and the ten
        days are never re-read (Home, 2026-09-29: one signature, from the
        minute of the reset)."""
        async with self._lock:
            orphans = self.fleet.main.name_descriptors() if self.fleet and not forget_names else []
            self.fleet = Fleet()
            self.fleet.main.carry_names(orphans)
            self.fleet.main.tz_offset_s = dt_util.now().utcoffset().total_seconds()
            self.last_processed = None
            self.caught_up = False
            self.refiling = True
            self.samples_read = 0
            self._saved_at = None
            await self._persist(force=True)          # a reset must survive a crash
        self.async_update_listeners()
        self.hass.async_create_task(self.async_refresh())

    async def async_stop(self) -> None:
        await self.async_shutdown()              # no pass starts after this
        # A reload - every options change is one - used to drop whatever was
        # learned since the last write, up to SAVE_MAX_INTERVAL_S of it; and
        # the pending delayed write then landed after the next runner's own.
        if not self._lock.locked():
            await self._persist(force=True)

    async def async_follow_renames(self, renames: Dict[str, str]) -> None:
        """Rename entities in what the detector learned - between passes,
        never under one, and written at once: the options change that follows
        reloads the entry, and the new runner reads the store."""
        async with self._lock:
            self.fleet.rename_entities(renames)
            await self._persist(force=True)

    async def _run(self) -> None:
        if self._lock.locked() or not self.enabled:
            return
        async with self._lock:
            try:
                self.update_interval = timedelta(minutes=DETECTION_INTERVAL_MINUTES)
                began = time.monotonic()
                now = dt_util.utcnow()
                start = self.last_processed or (now - timedelta(days=DETECTION_BACKFILL_DAYS))
                end = min(now, start + timedelta(hours=DETECTION_SLICE_HOURS))
                samples = await self._derive_load(start, end)
                # the arrays, for telling a cloud from a load switching
                self.solar = await self._resolve_solar()
                generation: Dict[str, list] = {}
                for fields in self.solar:
                    rows, _ = await self._read(start, end, fields)
                    for p, series in rows.items():
                        generation.setdefault(p, [])
                        generation[p] = _sum_series(generation[p], series)
                # after the sum, not before: where the inverter is added back the
                # grid meter is the circuit every household watt flows through,
                # so its VAr is the one that steps when a load switches
                q = await self._reactive_series(start, end, samples)
                pv: Dict[str, Dict[float, float]] = {}
                for p, target in samples.items():
                    if generation.get(p):
                        pv[p] = _align(generation[p], target)
                for p, rows in samples.items():
                    if p in self.fleet.main.phases:
                        # a fixed floor since 2026-09-23 - see _meter_wait_field in
                        # config_flow; a value stored by an older version is not read
                        self.fleet.main.phases[p].min_noise = MIN_NOISE_W
                        # a reading that never exports is the house alone, and
                        # the house cannot draw less than nothing. Kept, and
                        # saved, where a pass is too short to say: "can't tell"
                        # read as "no" switched the guard off on every live pass.
                        verdict = carries_generation(rows)
                        if verdict is not None:
                            self.fleet.main.phases[p].floor_zero = verdict is False
                for p in list(pv):
                    verdict = carries_generation(samples[p])
                    if verdict is not None:
                        self.pv_visible[p] = verdict
                    if self.pv_visible.get(p) is False:
                        pv.pop(p)         # this reading never sees the sun; leave its steps alone
                self.submeters = await self._resolve_submeters()
                self.fleet.parents = {n: m.get("parent") for n, m in self.submeters.items()}
                sub_samples, sub_q, agnostic = {}, {}, {}
                for name, meter in self.submeters.items():
                    ss, sq = await self._read(start, end, meter["fields"])
                    if ss:
                        sub_samples[name], sub_q[name] = ss, sq
                        agnostic[name] = meter["agnostic"]
                # the recorder's start-of-window row is a copy, not a reading - see
                # without_window_start; the sums above needed it, the detector must not
                samples = without_window_start(samples, start.timestamp())
                sub_samples = {n: without_window_start(s, start.timestamp()) for n, s in sub_samples.items()}
                single = {n: self.holds_one_device(n) for n in self.submeters}
                switches, numbers, inputs = await self._read_inputs(start, end)
                read = time.monotonic()
                self.fleet.wait_cap_s = self.meter_wait_s
                await self.hass.async_add_executor_job(
                    self.fleet.process, samples, sub_samples, q, sub_q, end.timestamp(), agnostic, pv,
                    dict(self.q_quantum), single, switches or None, numbers or None,
                    inputs or None,
                )
                self.last_pass = {"read_s": round(read - began, 1), "detect_s": round(time.monotonic() - read, 1),
                                  "hours": round((end - start).total_seconds() / 3600.0, 2)}
                self.samples_read += sum(len(rows) for rows in samples.values())
                self._update_average_power(end.timestamp())
                self.last_processed = end
                self.caught_up = end >= now - timedelta(minutes=1)
                self.last_run = now
                await self._persist()
                self.async_update_listeners()
                if self.caught_up and self.refiling:
                    self.refiling = False
                    # the ten days re-read are the named loads' history now
                    self.entry.async_create_background_task(
                        self.hass, self._rewrite_named(), f"{DOMAIN} rewrite statistics")
                if not self.caught_up:
                    # keep slicing without waiting the minute
                    self.update_interval = timedelta(seconds=CATCH_UP_PASS_S)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Load detection run failed")

    def _device_of(self, entity_id: Optional[str]) -> Optional[str]:
        """Which device publishes this entity, or None."""
        if not entity_id:
            return None
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry.device_id if entry else None

    def _coherent_triple(self, cfg: dict, phase: str) -> Optional[dict]:
        """One role's power, voltage and current for a phase - but only when
        they come off the SAME device.

        V x I is the apparent power OF THE CIRCUIT THE METER IS IN. Pair one
        meter's amps with another meter's watts and the root of S squared
        minus P squared is not a reactive power, it is the two circuits'
        difference wearing the units of one. Home was configured exactly that
        way - house-consumption templates for the watts, grid meter for the
        volts and amps - and it cost 78 phantom signatures in a single day.

        A power-factor entity needs no current, so it counts as coherent on
        its own as long as it sits with the power."""
        power = cfg.get(f"power_{phase}")
        home = self._device_of(power)
        if not power:
            return None
        def with_power(key):
            eid = cfg.get(key)
            # no device on either side means a template or a helper, and we
            # cannot prove they belong together - so we do not assume it
            return eid if eid and home is not None and self._device_of(eid) == home else None
        pf, va = with_power(f"pf_{phase}"), with_power(f"va_{phase}")
        volts, amps = with_power(f"voltage_{phase}"), with_power(f"current_{phase}")
        var = with_power(f"var_{phase}") or self._var_on(home, phase, power)
        if not pf and not va and not (volts and amps) and not var:
            return None
        return {f"power_{phase}": power, f"pf_{phase}": pf, f"va_{phase}": va,
                f"voltage_{phase}": volts, f"current_{phase}": amps, f"var_{phase}": var}

    def _var_on(self, device: Optional[str], phase: str, reference: str) -> Optional[str]:
        """The device's own reactive power for this phase, if it publishes one
        - found beside the power, so a site configured before it was enabled
        uses it without being set up again."""
        if device is None:
            return None
        return beside(self._device_rows(er.async_get(self.hass), device), reference, "var", phase)

    def _triple_beside_the_amps(self, cfg: dict, phase: str) -> Optional[dict]:
        """A coherent triple built from the meter the VOLTS AND AMPS are on.

        Home is the case this exists for. Its load reading is a template of
        house consumption, which belongs to no device, while its voltage and
        current were matched to the SolarEdge meter - so no role offers a
        triple and there would be no power factor at all. But that meter
        publishes watts too, and those watts ARE the circuit its amps are in.

        The power entity is chosen by the longest name it shares with the
        current entity, which is what keeps an inverter's output amps with
        its output watts rather than pairing them across its input.
        """
        volts, amps = cfg.get(f"voltage_{phase}"), cfg.get(f"current_{phase}")
        if not (volts and amps):
            return None
        device = self._device_of(amps)
        if device is None or device != self._device_of(volts):
            return None
        power = beside(self._device_rows(er.async_get(self.hass), device), amps, "power", phase)
        if not power:
            return None
        return {f"power_{phase}": power, f"voltage_{phase}": volts,
                f"current_{phase}": amps, f"pf_{phase}": None, f"var_{phase}": self._var_on(device, phase, power)}

    async def _reactive_series(self, start: datetime, end: datetime,
                               targets: Dict[str, list]) -> Dict[str, Dict[float, float]]:
        """Reactive VAr at each of ``targets``' sample times.

        Taken from whichever role publishes a coherent triple - the load's
        own first, since that is the circuit the loads are in, and the grid
        meter after it. At home only the grid meter has volts and amps, and
        that is fine: every watt the house draws flows through it, so the
        CHANGE in its reactive power when something switches is the load's
        own, even though the level is mostly the inverter's grid support.
        Kozolec has the triple on the load side itself.

        Nothing is returned when no role can offer one, which is honest: no
        power factor beats a fabricated one."""
        out: Dict[str, Dict[float, float]] = {}
        for phase, rows in targets.items():
            if not rows:
                continue
            for prefix in ("", "grid_"):
                cfg = {k[len(prefix):]: v for k, v in self.config.items()
                       if not prefix or k.startswith(prefix)} if prefix else dict(self.config)
                triple = self._coherent_triple(cfg, phase) or self._triple_beside_the_amps(cfg, phase)
                if triple is None:
                    continue
                series = await self._read_raw(start, end, triple)
                power_rows = series.get(("power", phase)) or []
                if not carries_load(power_rows):
                    continue          # coherent, but nothing flows through it
                signed = series.get(("var", phase))
                var = _reactive(power_rows, series.get(("voltage", phase)),
                                series.get(("current", phase)), series.get(("pf", phase)), signed,
                                series.get(("va", phase)))
                if var:
                    covers = bool(signed) and signed[0][0] <= power_rows[0][0] + SIGNED_VAR_SLACK_S
                    self.reactive_from[phase] = (f"signed {triple.get(f'var_{phase}')}" if covers
                                                 else f"apparent {triple.get(f'va_{phase}')}" if series.get(("va", phase))
                                                 else f"V x I {triple.get(f'power_{phase}')}")
                    # the reference samples at its own moments; hold each
                    # value forward onto the load's
                    out[phase] = _align(sorted(var.items()), rows)
                    # ...and what those amps could actually resolve, which is
                    # the error bar on every factor derived from them. A
                    # power-factor entity needs no amps and carries its own
                    # precision, so it is left ungated.
                    # ACCUMULATED across passes, never re-measured from one.
                    # A pass reads a single minute of history, which holds
                    # nowhere near enough changes to confirm a lattice - so
                    # measuring per pass returned nothing and, because it
                    # cleared first, threw away what the six-hour backfill
                    # slices HAD learned. Resolution is a property of the
                    # instrument; it does not expire between passes.
                    amps = series.get(("current", phase)) or []
                    volts = series.get(("voltage", phase)) or []
                    if amps and volts:
                        steps = self._amp_steps.setdefault(phase, [])
                        steps.extend(abs(b - a) for (_, a), (_, b)
                                     in zip(amps, amps[1:]) if b != a)
                        del steps[:-AMP_STEP_MEMORY]
                        dq = quantum_of_steps(steps)
                        if dq:
                            self.q_quantum[phase] = dq * _median([v for _, v in volts])
                break
        return out

    async def _read_raw(self, start: datetime, end: datetime, cfg: dict) -> Dict[tuple, list]:
        """Every configured field of one role as (kind, phase) -> rows."""
        entities = {}
        for p in PHASES:
            for kind in ("power", "pf", "va", "current", "voltage", "var"):
                eid = cfg.get(f"{kind}_{p}")
                if eid:
                    entities[(kind, p)] = eid
        if not entities:
            return {}
        states = await get_instance(self.hass).async_add_executor_job(
            _fetch, self.hass, start, end, list(dict.fromkeys(entities.values()))
        )
        series: Dict[tuple, list] = {}
        for key, eid in entities.items():
            # History is fetched with no_attributes, so the unit comes from the
            # live state: a meter publishing kW is otherwise read as watts and
            # its 2 kW session becomes the number 2.
            scale = unit_scale(self._unit_of(eid))
            rows = []
            for st in states.get(eid, []):
                try:
                    rows.append((st.last_updated.timestamp(), float(st.state) * scale))
                except (TypeError, ValueError):
                    continue
            rows.sort()
            series[key] = rows
        return series

    def _unit_of(self, entity_id: str) -> Optional[str]:
        state = self.hass.states.get(entity_id)
        if state is not None:
            unit = state.attributes.get("unit_of_measurement")
            if unit:
                return unit
        entry = er.async_get(self.hass).async_get(entity_id)
        return getattr(entry, "unit_of_measurement", None) if entry else None

    def _update_average_power(self, processed_to: float) -> None:
        """Mean watts each named load drew over the data just processed.

        Anze, 2026-09-18: rather than report the instantaneous power of a
        step whose matching step down has not arrived, report the ENERGY that
        arrived divided by the time it covers. Everything that made the
        instantaneous reading unreliable goes away - it counts only sessions
        that CLOSED, so a 44-second kiln run contributes whether or not
        anyone was looking at the right moment, and a step up whose partner
        never came contributes nothing instead of sitting high for a day.

        The denominator is the span of DATA processed, not wall-clock time.
        During the backfill a single pass covers six hours of history in a
        few seconds, and dividing that energy by the few seconds would report
        megawatts.
        """
        energy = self.detector.energy_by_name()
        previous, since = self._energy_mark, self._mark_ts
        self._energy_mark, self._mark_ts = dict(energy), processed_to
        if since is None or processed_to <= since:
            return
        self.average_power = mean_power(previous, energy, processed_to - since)

    async def _read(self, start: datetime, end: datetime, cfg: dict):
        """(watts per phase, reactive VAr per phase) over the window.

        The VAr here is only ever derived from readings that sit on one
        device; a role whose watts and amps come from different meters gets
        none, and ``_reactive_series`` finds it a proper source instead."""
        series = await self._read_raw(start, end, cfg)
        samples = {p: series[("power", p)] for p in PHASES if series.get(("power", p))}
        if not samples:
            return {}, {}
        q: Dict[str, Dict[float, float]] = {}
        for p in samples:
            if self._coherent_triple(cfg, p) is None:
                continue          # different meters; not this power's VAr
            var = _reactive(samples[p], series.get(("voltage", p)),
                            series.get(("current", p)), series.get(("pf", p)),
                            series.get(("var", p)), series.get(("va", p)))
            if var:
                q[p] = var
        return samples, q


def _align(source: list, target_rows: list) -> Dict[float, float]:
    """``source`` read as of each of ``target_rows``' moments."""
    out: Dict[float, float] = {}
    i = 0
    for ts, _ in target_rows:
        i = _as_of(source, ts, i)
        if i >= 0:
            out[ts] = source[i][1]
    return out


# How far into a window the meter's own reactive power may start and still be
# taken for all of it: history reaches back past its first recorded day.
SIGNED_VAR_SLACK_S = 120.0


# A 3EM's apparent power read "as of" the power's stamp still held the reading
# BEFORE a kiln leg switched off - 34.5 W against 3505 VA, a 3.5 kvar spike at
# the very step (Home, 2026-09-30): the same update - see SAME_UPDATE_S.


def _with_update(rows: list, ts: float, i: int) -> int:
    """``i`` (as of ``ts``), or the next row when it is part of the same update."""
    if i + 1 < len(rows) and rows[i + 1][0] - ts <= SAME_UPDATE_S:
        return i + 1
    return i


def _reactive(power_rows: list, volts: Optional[list], amps: Optional[list],
              pfs: Optional[list], signed: Optional[list] = None,
              vas: Optional[list] = None) -> Dict[float, float]:
    """Reactive VAr at each power sample.

    Every entity updates at its own moment, so the other readings are taken
    as of the power sample's time - sample and hold - rather than looked up
    at the same instant, which almost never matches.

    The meter's own reactive power, where it publishes one and it covers the
    window, is taken as it is: signed, so a step's change is the load's own.
    The root of (V x I) squared minus P squared has no sign, and a load whose
    reactive power runs against the floor's reads the wrong size (Anze,
    2026-09-30). Else the meter's own apparent power, then V x I, then the
    power factor: finest first. Each paired with the power reading of the
    same update - see SAME_UPDATE_S."""
    if signed and power_rows and signed[0][0] <= power_rows[0][0] + SIGNED_VAR_SLACK_S:
        return _align(signed, power_rows)
    volts, amps, pfs, vas = volts or [], amps or [], pfs or [], vas or []
    out: Dict[float, float] = {}
    vi = ai = fi = si = 0
    for ts, p in power_rows:
        vi, ai, fi, si = _as_of(volts, ts, vi), _as_of(amps, ts, ai), _as_of(pfs, ts, fi), _as_of(vas, ts, si)
        v, a, f, s = (_with_update(volts, ts, vi), _with_update(amps, ts, ai),
                      _with_update(pfs, ts, fi), _with_update(vas, ts, si))
        apparent = None
        if s >= 0:
            apparent = vas[s][1]
        elif v >= 0 and a >= 0:
            apparent = volts[v][1] * amps[a][1]
        elif f >= 0 and pfs[f][1]:
            apparent = abs(p) / abs(pfs[f][1])
        if apparent is None:
            continue
        out[ts] = math.sqrt(max(0.0, apparent * apparent - p * p))
    return out


def _fetch(hass, start, end, entity_ids):
    """Every entity's state changes over the window, in ONE query.

    This looped over state_changes_during_period, which takes a single
    entity_id, so a pass cost one SQL round trip per entity - about twenty at
    Anze's home once the submeters are counted, and that is per pass rather
    than per sample, so it is exactly the cost that multiplies if detection
    runs more often (2026-09-18). get_significant_states takes the whole
    list; significant_changes_only=False is what keeps it every change rather
    than the dashboard's thinned-out view, and minimal_response=False keeps
    State objects rather than the compressed dicts, which is what the callers
    read.
    """
    if not entity_ids:
        return {}
    return history.get_significant_states(
        hass, start, end, list(entity_ids),
        include_start_time_state=True,
        significant_changes_only=False,
        minimal_response=False,
        no_attributes=True,
    )
