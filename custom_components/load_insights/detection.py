"""Runs the detector on the recorder's raw states, incrementally."""
from __future__ import annotations

import asyncio
import bisect
import functools
import logging
import os
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder import statistics as rec_stats
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    COMPRESSED_STATE_LAST_UPDATED,
    COMPRESSED_STATE_STATE,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_RECORDER_HOURLY_STATISTICS_GENERATED,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.json import load_json

from .const import (
    GRID_PREFIX,
    BASE_LOAD_GRID,
    CONF_BASE_LOAD,
    CONF_DETECTION,
    CONF_SINGLE_DEVICE,
    CONF_CHANGE_METERS,
    CONF_IGNORE_METERS,
    CONF_PERIODIC_METERS,
    CONF_HORIZON_SKIP,
    CONF_INPUT_ENTITIES,
    CONF_INVERTERS,
    CONF_INV_DEVICE,
    CONF_INV_INPUT_PREFIX,
    DEFAULT_MIN_EVIDENCE,
    NAMING_MIN_ROWS,
    NAMING_ROWS_PER_NAME,
    NAMING_START_ROWS,
    DETECTION_BACKFILL_DAYS,
    CONF_METER_WAIT,
    DETECTION_SLICE_HOURS,
    STREAM_SETTLE_S,
    STREAM_TICK_S,
    HISTORY_SAVE_INTERVAL_S,
    SAVE_MAX_INTERVAL_S,
    DOMAIN,
)
from .insights.detect import (
    HOURLY_KEEP_S,
    input_rows,
    METER_WAIT_CAP_S,
    SWITCH_MEMORY_S,
    combine,
    COMBINE_SETTLE_S,
    PHASES,
    SYSTEM_PHASES,
    Detector,
    Fleet,
    LivePower,
    LIVE_WRITE_MIN_S,
    carries_generation,
    _align,
    _reactive,
    SIGNED_VAR_SLACK_S,
    _median,
    _sum_series,
    without_window_start,
    names_in_store,
    dump_items,
    carries_load,
    unit_scale,
    exports_positive,
    drop_stale_load_override,
    quantum_of_steps,
    clears_for_naming,
    offer_for_naming,
)
from .insights.phases import beside, match_meter_entities, share_split, voltage_votes
from .insights.model import SiteModel
from .insights.solar import array_rows
from .autonaming import AutoNamer
from .insights.named import (first_change, metered_device, plan_carry, plan_rewrite,
                             vanished_into)

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


def base_load_entities(hass: HomeAssistant, entry_id: str) -> frozenset:
    """The entity ids of this entry's base load sensors (base_load_uid). They go on the Energy dashboard so its
    Sankeys show them, but they are a meter's floor worked out here, not a device: the forecasts leave them
    out - a forecast, a room to absorb and an absorption rate of a base load never had a value (the owner,
    2026-10-31)."""
    prefix = f"{entry_id}_baseload_"
    return frozenset(e.entity_id for e in er.async_get(hass).entities.values()
                     if e.platform == DOMAIN and (e.unique_id or "").startswith(prefix))


def base_load_uid(entry_id: str, key: str, phase: Optional[str], kind: str) -> str:
    """A meter's base load entity's unique id: ``key`` its energy statistic (BASE_LOAD_GRID for the grid),
    ``phase`` its phase or None for the whole, ``kind`` "power" or "energy"."""
    return f"{entry_id}_baseload_{kind}_{key.replace('.', '_')}_{phase or 'all'}"


def base_load_label(fleet, meter: str, phase: Optional[str]) -> str:
    """What a meter's base load is called - its device and its dashboard entry."""
    label = "Base load" if not meter else f"{meter} base load"
    if phase is not None:
        house = fleet.phase_map(meter).get(phase, phase) if meter else phase
        label += f" {SYSTEM_PHASES.get(house, phase.upper())}"
    return label


@dataclass
class RuntimeData:
    """What a loaded entry runs - its ConfigEntry.runtime_data."""
    coordinator: "InsightsCoordinator"
    runner: "DetectionRunner"
    site_device_id: str


def loaded(entry: Optional[ConfigEntry]) -> Optional[RuntimeData]:
    """The entry's runtime data, or None while it is not loaded."""
    return getattr(entry, "runtime_data", None)


def runner_of(entry: Optional[ConfigEntry]) -> Optional["DetectionRunner"]:
    """The entry's detection runner, or None while it is not loaded."""
    data = loaded(entry)
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
#     sessions and pairs exist at all.
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
#     pair a matched session joins - so which sessions share a pair,
#     and where pairs are placed, both change.
# 15 = edges are clustered by where their sizes pile up, pairs and links are
#     accepted above chance, and runs are filed by device rather than by how
#     alike their power looks: every cluster, pair and pair differs.
# 16 = a start is an all-phase event: rises on several phases within the
#     window are one cluster carrying a phase pattern, a device is its start
#     cluster (the link table is gone), and a run is filed on its own phases
#     only - every start cluster, device and pair differs.
# 17 = a step's place is part of its kind (the innermost meter that saw all
#     of it), a house step its meters stepped with is booked as their shares,
#     a run bigger than the whole reading is closed, and noise is learned from
#     how the reading moves - every cluster, run and pair differs.
# 18 = a meter's step is its own detector's, weighed against the grid's over
#     both spans; a level is confirmed, a silence held and a span started over
#     three of the meter's cadence (how often it writes while its value
#     moves); only a start is placed; a step no meter placed never joins a
#     meter that held its value - which steps are declared, and where every
#     start cluster sits, differ.
# 19 = one gap measure for every meter (its interval is its moving gaps'
#     tenth percentile, persisted), an unseen stop fitted only to what fell
#     short since a run started, a meter's step the other way split out of
#     a grid step that netted it, a held meter's pairs avoided when
#     filing, and the level left on the reading after a multi-close - which
#     steps are declared, and how runs close and file, differ.
# 20 = a pass is only a pause: every meter read on one clock, the grid its
#     meters' learned lag behind them; every decision on that clock and each
#     session filed when it becomes decidable, not at a pass's end; a meter's
#     latency learned from the steps it shares with the grid; device merges
#     after every filing, never into a pair a run was kept out of - the
#     same history read at any slicing files the same library, which differs
#     from one built in passes.
# 23 = the run lifecycle (2026-10-28/29): a fall closes the run it most
#     likely ends (size likeness x pair evidence x age, a probability; legs
#     of a multi-phase run only on a fall across them; a second rise never
#     ends the first without a fall or the readings), steps grouped by the
#     mixture only (joins at 2 widths), runs filed and merged by their start
#     and stop groups' pair (pair_home) - groups, devices and pairs all
#     differ from 22's.
DETECTOR_GENERATION = 23      # the run lifecycle (2026-10-29). 22: a value that came once and stayed no longer splits a load (2026-10-25). Signatures (2026-10-26) did not bump it: a stored library loads into signatures (Detector._assign_signatures), and a rebuild now reads only the recorder's 4 days, which would drop the older library. Before that: steps and claims (2026-10-23): states key the step groups, falls placed, Run.placed
MIN_COUNT_TO_NAME = 2          # a load seen once is not offered for naming
# What a load has actually USED is the reason to bother naming it: a
# pair worth 30 Wh over ten days is noise with a shape, and a list full
# of those is why the naming page ran to a hundred and eighty rows.
NAMING_MIN_WH = 50.0
# How many of a current reading's own changes to remember while confirming
# what it can resolve. A pass covers one minute, so the evidence has to be
# gathered across them or it is never gathered at all.
AMP_STEP_MEMORY = 600
# How soon the next pass follows one that is not caught up - a backfill slice
CATCH_UP_PASS_S = 2.0
# A pass reads the recorder up to `now`; a reading stamped before `now` but
# committed after the read is never read, because the next pass starts at the
# end of this one. So a pass waits for the recorder's queue to drain first. A
# pass that cannot get it in this long reads nothing and tries again at the
# same start a minute later.
RECORDER_FLUSH_S = 30.0
# Every this many passes an input's kept rows (_input_states) are held to a full read of its window; on any
# difference the full read is used and the kept rows start again (input_cache_check in the diagnostics).
# Once a day, each input on a pass of its own (by its id): every 30 passes, all at once, made one pass in 30
# read every input's window again - 13.8 s of a 25.3 s pass at Home - after 108 audits in three deploys found
# no difference (2026-11-09)
INPUT_AUDIT_PASSES = 1440
# ...and each pass reads again this far back from the last read's end, for a row committed after that read
INPUT_OVERLAP_S = 60.0


def energy_site(hass: HomeAssistant, prefs) -> SiteModel:
    """The Energy dashboard's site AS DETECTION READS IT: less anything this
    integration publishes.

    A detected load's energy sensor can be listed there - the kiln, under
    the meter it sits in - and the forecasts welcome it like any device. But
    detection takes every listed device as a meter, and read back as one the
    kiln's estimate would be fed in as a measurement, with the kiln then
    detected inside it (2026-09-28: "make sure that the generated
    detected entities do not back feed to any detection as if they were
    actual measurements"). Read back as a sub-meter a load's estimate became
    its own truth - and naming a load after it made the load "that metered
    device", so it got no sensors at all (Home and Kozolec, 2026-09-30, after
    the full reset left Peč za Glino, Kompresor, Fridges... on the dashboard)."""
    own = frozenset(e.entity_id for e in er.async_get(hass).entities.values() if e.platform == DOMAIN)
    return SiteModel.from_prefs(prefs, ignore=own)


def meter_devices(hass: HomeAssistant, device_id: str) -> List[str]:
    """The device and the devices that hang off it (via_device_id) - see
    device_rows.

    Looked for among the devices of the meter's own config entries:
    `registry.devices` used as a mapping (`.values()` too) is deprecated from
    Home Assistant 2026.9 and breaks in 2027.9, and iterating it yields device
    ids rather than entries before 2026.9 - this helper reads the same on both.
    A device's `config_entries` (a set) is deprecated for its single
    `config_entry_id` and breaks in 2027.10; older versions have only the set."""
    ids = [device_id]
    try:
        devices = dr.async_get(hass)
        meter = devices.async_get(device_id)
        if meter is None:
            entry_ids = ()
        elif hasattr(type(meter), "config_entry_id"):   # checked on the class: no deprecation warning
            entry_ids = (meter.config_entry_id,) if meter.config_entry_id else ()
        else:
            entry_ids = meter.config_entries
        ids += dict.fromkeys(d.id for entry_id in entry_ids
                             for d in dr.async_entries_for_config_entry(devices, entry_id)
                             if d.via_device_id == device_id)
    except Exception:                        # a registry we cannot read is not fatal
        pass
    return ids


def area_id(hass: HomeAssistant, entity_id: Optional[str]) -> Optional[str]:
    """An entity's own area where it has one, else its device's."""
    entity = er.async_get(hass).async_get(entity_id) if entity_id else None
    if entity is None or entity.area_id or not entity.device_id:
        return entity.area_id if entity is not None else None
    device = dr.async_get(hass).async_get(entity.device_id)
    return device.area_id if device else None


def device_rows(hass: HomeAssistant, device_id: str) -> list:
    """A device's sensors in the shape the meter matcher reads, INCLUDING
    those of the devices that hang off it.

    A Shelly Pro 3EM is one device per PHASE plus a parent carrying the
    totals, each phase pointing at the parent with via_device_id. The
    Energy dashboard names the parent - that is where the energy
    statistic lives - so reading only the parent's own entities found a
    single total and nothing else, and a three-phase meter was taken for
    a one-phase one. It is the commonest three-phase meter there is
    (the owner's attic and grid meters are both this, 2026-09-22). The grid
    page reads a device through this too."""
    registry = er.async_get(hass)
    rows = []
    for e in [x for i in meter_devices(hass, device_id)
              for x in er.async_entries_for_device(registry, i, include_disabled_entities=False)]:
        if e.domain != "sensor":
            continue
        st = hass.states.get(e.entity_id)
        rows.append({
            "entity_id": e.entity_id,
            "device_class": e.device_class or e.original_device_class or (st.attributes.get("device_class") if st else None),
            "name": e.name or e.original_name or "",
        })
    return rows


def _key(rows) -> tuple:
    return tuple(tuple(sorted(r.items())) for r in rows)


@functools.lru_cache(maxsize=64)
def _matched_key(rows: tuple, role: str) -> Dict[str, str]:
    return match_meter_entities([dict(r) for r in rows], role)


@functools.lru_cache(maxsize=256)
def _beside_key(rows: tuple, reference: str, kind: str, phase: str) -> Optional[str]:
    return beside([dict(r) for r in rows], reference, kind, phase)


def match_rows(rows, role: str = "load") -> Dict[str, str]:
    """match_meter_entities, remembered by the rows themselves: every pass matched the same unchanged sensors
    again by their names (~0.5 s CPU a minute at Home on the CM4, 2026-11-10). A renamed or added sensor is
    another key, so nothing goes stale; phases.py stays the verbatim copy Load Juggler shares."""
    return dict(_matched_key(_key(rows), role))


def beside_of(rows, reference: str, kind: str, phase: str) -> Optional[str]:
    """phases.beside, remembered the same way as match_rows()."""
    return _beside_key(_key(rows), reference, kind, phase)


def _sum_phases(readings) -> Dict[str, list]:
    """Per phase, every reading's series summed (_sum_series), in order."""
    out: Dict[str, list] = {}
    for rows in readings:
        for p, series in rows.items():
            out[p] = _sum_series(out.get(p, []), series)
    return out


def _owner(path: str) -> str:
    """Whose code a profiled function is: the integration (custom or core), else the library."""
    for mark in ("/custom_components/", "/homeassistant/components/"):
        if mark in path:
            return path.split(mark, 1)[1].split("/", 1)[0]
    if "/site-packages/" in path:
        return "lib:" + path.split("/site-packages/", 1)[1].split("/", 1)[0]
    if "/homeassistant/" in path:
        return "ha-core"
    return "python"


def _owner_seconds(stats: dict) -> List[Tuple[str, float]]:
    """Own time (tottime) per owner, largest first, from pstats' raw table. A builtin ("~", dict.get,
    sum) has no file, so its time goes to its callers' owners, in their share of it."""
    out: Dict[str, float] = {}
    for (path, _, _), (_, _, tt, _, callers) in stats.items():
        if path != "~":
            out[_owner(path)] = out.get(_owner(path), 0.0) + tt
            continue
        shares = {c: v[2] for c, v in callers.items()}
        total = sum(shares.values())
        if not total:
            out["python"] = out.get("python", 0.0) + tt
            continue
        for (cpath, _, _), part in shares.items():
            who = "python" if cpath == "~" else _owner(cpath)
            out[who] = out.get(who, 0.0) + tt * part / total
    return sorted(out.items(), key=lambda kv: -kv[1])


def _profiled(prof, job):
    """Run ``job`` under ``prof`` in the calling (executor) thread."""
    prof.enable()
    try:
        return job()
    finally:
        prof.disable()


class DetectionRunner(DataUpdateCoordinator[None]):
    """The configured sensors' state changes, streamed into the detector (docs/specs/2026-11-11-streaming-pipeline.md,
    step 2): every second (_tick) the readings up to a second ago, assembled and run in one executor job (_step);
    once a minute (_minute) the detector settled, live power, the save, the entities. What the stream did not see -
    the backfill on a fresh start, the stretch a restart missed - the recorder fills, an hour's slice a step, through
    the same assembly. The detector's state, sessions and pairs persist in .storage so a restart resumes where it
    stopped.

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
        # per run: {name: {"fields": {...}, "parent": name|None, ...}}
        self.submeters: Dict[str, dict] = {}
        # ...and every meter the dashboard has, those left out of detection
        # on the settings page too (see CONF_IGNORE_METERS)
        self.all_submeters: Dict[str, dict] = {}
        self.fleet: Fleet = Fleet()
        # V x dI per phase: the apparent power one quantum of the amps
        # behind this role's power factor is worth. Measured, never set.
        self.q_quantum: Dict[str, float] = {}
        self._amp_steps: Dict[object, List[float]] = {}
        # ...and each meter's channel's, from its own amps, the same way
        self.sub_q_quantum: Dict[str, Dict[str, float]] = {}
        self.solar: List[Dict[str, str]] = []   # each array's power per phase
        # whether the configured reading actually includes the array, read
        # off the data per phase and remembered once it is conclusive
        self.pv_visible: Dict[str, bool] = {}
        # whether the grid meter reads export positive, as the last pass that could tell found (_grid_sign)
        self._exports_positive: Optional[bool] = None
        # what the inverters added to each phase of a house built from them
        # (_derive_load), as of each of its readings: the array IN the reading
        self.array_in_house: Dict[str, Dict[float, float]] = {}
        # how many meter readings the runs have actually had to work with,
        # so "no loads found" can be told from "no data"
        self.samples_read: int = 0
        # Watts per NAMED load as last written (LivePower, streaming spec step 3), and each one's entity's write
        self.live_power: Dict[str, float] = {}
        self._livepower = LivePower()
        self._live_due: Dict[str, float] = {}
        self._live_writers: Dict[str, Callable[[], None]] = {}
        self._dashboard_was: Optional[tuple] = None     # _dashboard_key as the Energy dashboard list was last written
        self._saved_at: Optional[float] = None
        self._history_saved_at: Optional[float] = None
        self._inputs_primed = False       # see _read_inputs
        self.last_processed: Optional[datetime] = None
        self.caught_up = False
        # re-reading history from scratch - after a reset, or with nothing
        # stored: what the library gains is old, not new energy
        self.refiling = False
        # name -> the statistic ids of the names that vanished into it
        # (_carry_vanished), whose history its meter is owed before the
        # window - once, as soon as it has an hour (async_backfill_statistics).
        # Stored, so a restart before then still carries it
        self.carries: Dict[str, List[str]] = {}
        self.last_run: Optional[datetime] = None
        # seconds the last pass spent reading the recorder and detecting, and
        # the hours it covered: where a backfill's time goes
        self.last_pass: Dict[str, float] = {}
        # load_insights.profile_passes: the passes still to profile, the profiler, and each pass's timings
        self._profile_left = 0
        self._profiler = None
        self._window = None          # ...and profile_seconds' whole-process profiler while it runs
        self.last_hourly_check: Dict[str, float] = {}
        self._active_memo: Optional[tuple] = None      # see active_by_name
        self._names_memo: Optional[tuple] = None       # see names_now
        self._input_cache: Dict[str, tuple] = {}       # see _input_states: (since, end, every row) per input
        self._input_passes: Dict[str, int] = {}        # passes read per input, for the audit (INPUT_AUDIT_PASSES)
        self._input_starts: Dict[str, list] = {}       # this pass's start rows, every input's (_start_rows)
        self._input_rows: Dict[str, tuple] = {}        # this pass's rows, every input's, read together (_batch_inputs)
        self.input_cache_check: Dict[str, object] = {}
        self._profile_rows: List[dict] = []
        self._listener_s: Dict[str, float] = {}
        # the window being assembled: (start, end, rows by entity) - from the stream's buffer live, the recorder
        # for a gap (_step)
        self._pass_rows: Optional[tuple] = None
        # streaming (step 2): the Energy dashboard's site as last read (_refresh_config); every entity the
        # assembly has asked for, listened to (_listen); those a window had no rows for (_read_raw)
        self._site = None
        self._wanted: set = set()
        self._missing: set = set()
        self._stream = _Stream()
        self._input_buf: Dict[str, list] = {}         # an input's State objects since its last read (_input_states)
        self._listening: set = set()
        self._unlisten = None
        self._live = False            # the stream holds everything since last_processed: windows come from it
        self._listen_from: Optional[float] = None     # when listening began: the recorder fills what is before it
        self._settled_to: Optional[float] = None      # the end the minute's settle ran to (_minute)
        # declared time minus step time, the last steps' (seconds) - see _note_latency
        self._lags: List[float] = []
        self._lag_seen: Dict[tuple, float] = {}
        self.step_latency: Dict[str, float] = {}
        self.stream_audit: Dict[str, object] = {}      # the daily comparison with the recorder (_audit_stream)
        self.last_minute: Dict[str, float] = {}         # what the minute's work took (_minute)
        self._audited_day: Optional[str] = None
        self.reactive_from: Dict[str, str] = {}      # phase -> what its reactive power was read from
        # the library: a Store's file, but written and read by _write_store / async_start,
        # not through Store - see _write_store
        self._store_key = f"{DOMAIN}.{entry.entry_id}.detection"
        self._store_path = hass.config.path(STORAGE_DIR, self._store_key)
        # the hours confident unnamed loads keep past the window
        # (Pair.older), apart from the library they would swell
        self._history: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.detection.history")
        # every grouped step of the last ten days, with its voltages (run lifecycle, Task 8c):
        # several MB at Home, so a store of its own, written as rarely as the older hours
        self._steps: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.detection.steps")   # only to remove it
        # held by a pass, and by whatever must not run under one: a reset, a
        # rename, a statistics rewrite - each waits for the pass to finish
        self._lock = asyncio.Lock()
        self._stopping = False
        # what detection named by itself and put on the Energy dashboard (INS-21)
        self.autonamer = AutoNamer(self)

    def energy_entity(self, name: str) -> Optional[str]:
        """The entity id of a named load's own energy sensor (None until it exists)."""
        return er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, load_uid(self.entry.entry_id, "energy", name))

    def power_entity(self, name: str) -> Optional[str]:
        """The entity id of a named load's own power sensor (None until it exists)."""
        return er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, load_uid(self.entry.entry_id, "power", name))

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
        ignored = set(self.config.get(CONF_IGNORE_METERS) or [])
        unwaited = set(self.config.get(CONF_HORIZON_SKIP) or [])
        forced = {**dict.fromkeys(self.config.get(CONF_PERIODIC_METERS) or [], False),
                  **dict.fromkeys(self.config.get(CONF_CHANGE_METERS) or [], True)}
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
                fields = match_rows(device_rows(self.hass, entry.device_id))
            if not any(fields.get(f"power_{p}") for p in PHASES):
                # no per-phase reading: the dashboard's own power sensor, one
                # channel whose label means nothing - the votes find its
                # phase (Fleet.phase_map). A lone per-phase reading is fed
                # under its own label, with its own reactive power
                if not dev.power:
                    continue
                fields = {"power_a": dev.power}
            parent = by_stat.get(dev.included_in or "")
            out[dev.label] = {"fields": fields,
                              "parent": parent.label if parent else None, "energy": dev.energy,
                              # None: not declared, the library's shape decides
                              "single": None if declared is None else dev.energy in declared,
                              "ignored": dev.energy in ignored, "horizon_skip": dev.energy in unwaited,
                              "cadence": forced.get(dev.energy)}
        return out

    async def _refresh_submeters(self) -> None:
        """Every meter the dashboard has (the settings page lists them all),
        and the ones detection reads: all but those left out of it."""
        self.all_submeters = await self._resolve_submeters()
        self.submeters = {n: m for n, m in self.all_submeters.items() if not m.get("ignored")}

    def _inverter_terms(self, start: datetime, end: datetime) -> List[tuple]:
        """Each inverter as (rows, sign) per phase: its output, less its input.

        An inverter contributes what it ADDS. A PV string inverter has no AC
        input and contributes its whole output; a hybrid with the grid
        flowing through it contributes the difference, so the grid it passed
        on is not counted a second time. Nothing here consults a wiring flag,
        because the sum telescopes either way.

        With no inverter set up, the Energy dashboard's solar sources stand in as inverters with no input (the
        owner, 2026-11-11, INS-23): without them the house was the grid meter alone, below zero whenever the
        sun exported (Andrej's site, 2026-10-21, until his inverter was entered by hand). A source that only
        has a total is spread over the grid meter's phases in equal shares, as an inverter's total is."""
        out: List[tuple] = []
        if not self.entry.options.get(CONF_INVERTERS):
            phases = [p for p in PHASES if self.config.get(f"{GRID_PREFIX}power_{p}")] or list(PHASES)
            for fields in self._dashboard_arrays():
                ids = {v for v in fields.values() if v}
                if len(ids) == 1:
                    cfg, share = {f"power_{p}": next(iter(ids)) for p in phases}, 1.0 / len(phases)
                else:
                    cfg, share = {k: v for k, v in fields.items() if v}, 1.0
                rows, _ = self._read(start, end, cfg)
                if rows:
                    out.append((rows, share))
            return out
        for k, inv in enumerate(self.entry.options.get(CONF_INVERTERS) or []):
            for prefix, sign in (("", 1.0), (CONF_INV_INPUT_PREFIX, -1.0)):
                cfg = {f"power_{p}": inv.get(f"{prefix}power_{p}") for p in PHASES}
                total = inv.get(f"{prefix}power")
                if total and not any(cfg.values()):
                    split = self._inverter_split(start, end, inv, total, prefix or f"inv{k}")
                    if split is not None:
                        out.append((split, sign))
                        continue
                    # a machine that publishes one figure and no volts or amps: a
                    # three-phase inverter is symmetric, so its legs are its total in
                    # thirds - which reproduced the owner's own templates exactly
                    cfg = {f"power_{p}": total for p in PHASES}
                    share = 1.0 / len(PHASES)
                else:
                    share = 1.0
                cfg = {k: v for k, v in cfg.items() if v}
                if not cfg:
                    continue
                rows, _ = self._read(start, end, cfg)
                out.append((rows, sign * share))
        return out

    def _inverter_split(self, start: datetime, end: datetime, inv: dict, total: str,
                              name: str) -> Optional[Dict[str, list]]:
        """A one-figure inverter's output on each GRID phase by its phases' V x I
        share, through the map its voltages vote against the grid meter's
        (phases.voltage_votes, Fleet.inverter_map); None without its per-phase
        volts and amps (the inverter page's fields, else the device's own
        sensors) or the grid meter's volts, or while the map is not yet clear.
        Home's SE17K is wired a <-> b against its M1 (2026-10-23): a third on
        each phase hid that, a share through the wrong map would not."""
        fields = {f"{kind}_{p}": inv.get(f"{kind}_{p}") for kind in ("voltage", "current") for p in PHASES}
        if not all(fields.values()) and inv.get(CONF_INV_DEVICE):
            memo = self.__dict__.setdefault("_resolve_memo", {})
            key = ("inv", inv[CONF_INV_DEVICE])
            if key not in memo:              # once per configuration, as the triples
                memo[key] = match_rows(device_rows(self.hass, inv[CONF_INV_DEVICE]))
            matched = memo[key]
            fields = {key: fields[key] or matched.get(key) for key in fields}
        grid_v = {f"voltage_{p}": self.config.get(f"{GRID_PREFIX}voltage_{p}") for p in PHASES}
        if not all(fields.values()) or not all(grid_v.values()):
            return None
        own = self._read_raw(start, end, {**fields, "power_a": total})
        grid = self._read_raw(start, end, grid_v)
        volts = {p: own.get(("voltage", p)) or [] for p in PHASES}
        amps = {p: own.get(("current", p)) or [] for p in PHASES}
        rows = own.get(("power", "a")) or []
        gv = {p: grid.get(("voltage", p)) or [] for p in PHASES}
        if not rows or not all(volts.values()) or not all(amps.values()) or not all(gv.values()):
            return None
        mapping = self.fleet.inverter_map(name, voltage_votes(volts, gv))
        if len(mapping) < len(PHASES):
            return None
        return share_split(rows, volts, amps, mapping, PHASES)

    def _derive_load(self, start: datetime, end: datetime) -> Dict[str, list]:
        """What the house drew, per phase, from the grid and the inverters.

            house = grid meter + SUM over inverters of (output - input)

        There is no load reading to configure because there is nothing to
        configure: given the grid and the inverters, the house is determined
        (2026-09-18 - "just have each inverter entry have both inputs
        and outputs configurable"). Whatever sits between the utility meter
        and an inverter's own AC input falls out of the same sum, because a
        hybrid measures that input itself.

        An explicit reading still wins where someone has one that already IS
        the house - a dedicated CT, or the template sensors the owner built before
        this existed."""
        self.array_in_house = {}
        override = {f"power_{p}": self.config.get(f"power_{p}") for p in PHASES}
        if any(override.values()):
            samples, _ = self._read(start, end, {k: v for k, v in override.items() if v})
            return samples

        terms: List[tuple] = []
        grid_cfg = {f"power_{p}": self.config.get(f"{GRID_PREFIX}power_{p}") for p in PHASES}
        grid_cfg = {k: v for k, v in grid_cfg.items() if v}
        grid_rows: Dict[str, list] = {}
        if grid_cfg:
            grid_rows, _ = self._read(start, end, grid_cfg)
        inverters = self._inverter_terms(start, end)
        if grid_rows:
            generation = _sum_phases(rows for rows, sign in inverters if sign > 0)
            sign = self._grid_sign(grid_rows, generation)
            terms.append((grid_rows, sign))
        terms.extend(inverters)
        if not terms:
            return {}

        out: Dict[str, list] = {}
        for p in PHASES:
            per_phase = [(rows[p], sign) for rows, sign in terms if rows.get(p)]
            if per_phase:
                out[p] = combine(per_phase, settle_s=COMBINE_SETTLE_S)
            added = [(rows[p], sign) for rows, sign in inverters if rows.get(p)]
            if grid_rows and added and out.get(p):
                # the detector learns its idle noise and floor only while this holds still
                self.array_in_house[p] = _align(combine(added), out[p])
        return {p: rows for p, rows in out.items() if rows}

    def _grid_sign(self, grid_rows: Dict[str, list],
                         generation: Optional[Dict[str, list]]) -> float:
        """+1 where the grid meter reads import positive, -1 where it does
        not - because "the house is the meter plus the inverter" is true only
        in the first convention, and the owner's SolarEdge M1 is the second.

        Declared beats derived: the Energy dashboard's grid source asks this
        outright (none, normal, inverted, or a pair of sensors), so where it
        has been answered we use the answer. The dashboard names a TOTAL
        power sensor while detection runs per phase, so taking its polarity
        for the phases is an assumption - a safe one on one device, and
        exports_positive stays as the cross-check for a site that has left
        the power config empty."""
        for spec in self._site_grid_power():
            if spec.polarity is not None:
                return float(spec.polarity)
        if generation:
            for phase, rows in grid_rows.items():
                verdict = exports_positive(rows, generation.get(phase) or [])
                if verdict is not None:
                    self._exports_positive = verdict
                    break
        # the last pass that could tell, where this one is too short to
        return -1.0 if self._exports_positive else 1.0

    def _site_grid_power(self) -> list:
        return list(self._site.grid_power) if self._site is not None else []

    def _resolve_solar(self) -> List[Dict[str, str]]:
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
        # view to find (2026-09-18).
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
        return self._dashboard_arrays()

    def _dashboard_arrays(self) -> List[Dict[str, str]]:
        """Each of the Energy dashboard's solar sources as an array's power per phase (a total in every phase
        where its device has no per-phase powers), never a meter's reading on the same device (array_rows)."""
        site = self._site
        if site is None or not site.solar:
            return []
        registry = er.async_get(self.hass)
        spare = list(site.solar_power)
        grid_power = [self.config.get(f"{GRID_PREFIX}power_{p}") for p in PHASES]
        out: List[Dict[str, str]] = []
        for stat in site.solar:
            found: Dict[str, str] = {}
            entry = registry.async_get(stat)
            if entry is not None and entry.device_id:
                # the inverter's device may carry the grid meter too (SolarEdge Modbus Multi's M1)
                memo = self.__dict__.setdefault("_resolve_memo", {})
                key = ("array", entry.device_id, tuple(grid_power))
                if key not in memo:          # once per configuration, as the triples
                    memo[key] = match_rows(array_rows(device_rows(self.hass, entry.device_id), grid_power))
                matched = memo[key]
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
        if not self._inputs_primed and self.fleet._now:
            # the first pass of a run: what a restart cannot get back from the store is read again, as far back as the
            # oldest run or session still to be placed asks (never past the recorder's ten days)
            oldest = dt_util.utc_from_timestamp(self.fleet._oldest_asked())
            since = max(min(since, oldest - timedelta(seconds=SWITCH_MEMORY_S)), start - timedelta(days=DETECTION_BACKFILL_DAYS))
        self._inputs_primed = True
        # every input the forecast is given, whatever it is linked to (the owner,
        # 2026-09-28: add the thermostat to the inputs first, and move it to
        # the device once the load it helped find is named)
        read_ids = [e for e in self.entry.options.get(CONF_INPUT_ENTITIES) or []
                    if e.split(".", 1)[0] in SWITCH_DOMAINS or e.split(".", 1)[0] in NUMBER_DOMAINS
                    or e.split(".", 1)[0] in STATE_DOMAINS]
        skip: set = set()
        if self._live:
            # streaming: only the inputs that changed since the last step (_input_dirty), one not read yet, or
            # one whose daily audit is due - read every second, all 19 of Home's cost 4.8 s of CPU per 2 min
            # (2026-11-12); an unchanged one would only hand the detector rows it has merged already
            dirty = self.__dict__.setdefault("_input_dirty", set())
            skip = {eid for eid in read_ids if eid not in dirty and eid in self._input_cache and not self._audit_due(eid)}
            dirty.difference_update(read_ids)
            # each input's start row from its kept rows (every row of any kind, as the recorder's start-time
            # statement takes), its new rows from the state changes (_input_states) - no query
            self._input_starts = {eid: [st for st in (self._input_cache.get(eid) or (0, 0, []))[2]
                                        if st.last_updated <= since][-1:] for eid in read_ids if eid not in skip}
            self._input_rows = {}
        else:
            # every input's start row in one query: one per input was ~0.55 s of a pass at Home (19 inputs, 2026-11-09)
            self._input_starts = await self._start_rows(read_ids, since)
            await self._batch_inputs(read_ids, since, end)
        for eid in self.entry.options.get(CONF_INPUT_ENTITIES) or []:
            domain = eid.split(".", 1)[0]
            if eid in skip:
                continue
            if domain not in SWITCH_DOMAINS and domain not in NUMBER_DOMAINS and domain not in STATE_DOMAINS:
                continue
            # A climate or a fan is read by an ATTRIBUTE - a thermostat's
            # hvac_action, a fan's speed - which changes while the state does
            # not: a thermostat stays "heat" all winter, and state changes only
            # gave the floor mat's thermostat not one switch-on in production
            # (2026-09-28). Kept between passes: _input_states.
            states = {eid: await self._input_states(eid, domain, since, end)}
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
                    inputs[eid] = input_rows(states.get(eid, []), domain)
                continue
            if domain in STATE_DOMAINS:
                inputs[eid] = input_rows(states.get(eid, []), domain)
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
        self._input_rows = {}
        return switches, numbers, inputs

    def holds_one_device(self, name: str) -> bool:
        """The user's answer for this meter, or else the default the settings
        page shows: a meter with others nested inside it on the Energy
        dashboard holds several by definition (Home's Delavnica, Kozolec's
        Inverter), anything else as its library suggests."""
        meter = self.submeters.get(name) or {}
        if meter.get("single") is not None:
            return meter["single"]
        return self.fleet.guess_one_device(name)     # nested meters included: Fleet.parents is set each pass

    def remainder_meter(self) -> Optional[Tuple[str, str, List[str]]]:
        """(name, energy statistic, the read meters beside it) of the one top-level Energy dashboard meter that
        detection does not read (ignore_meters): a meter worked out as the grid less the others - Home's
        Delavnica, grid + PV - Hiša - Mansarda - the EVBox, its timing spikes no measurement (the owner,
        2026-10-30). Its base load is the grid's less those meters' (remainder_base_load). None unless
        exactly one such meter."""
        top = [(n, m) for n, m in self.all_submeters.items()
               if m.get("ignored") and not m.get("parent") and m.get("energy")]
        if len(top) != 1:
            return None
        name, meter = top[0]
        return name, meter["energy"], sorted(n for n, m in self.submeters.items() if not m.get("parent"))

    def remainder_base_load(self) -> Tuple[Optional[float], float]:
        """(W, Wh) of the remainder meter's base load: what no read meter covers - the grid's base load
        (base_load("")), every read meter taken off, the ones inside the unread remainder too (the owner,
        2026-10-31: "the submeters should always be subtracted"; Delavnica's is the grid's, as he said).
        W None while the grid's is not learned, never below 0."""
        if self.remainder_meter() is None:
            return None, 0.0
        return self.base_load("")

    def _now_w(self, meter: str, phase: Optional[str] = None) -> Optional[float]:
        """What a meter ("" the grid) - or one phase of it - reads now: its phases' settled levels."""
        det = self.fleet.main if not meter else self.fleet.subs.get(meter)
        if det is None:
            return None
        levels = [st.level for p, st in det.phases.items() if (phase is None or p == phase) and st.level is not None]
        return sum(levels) if levels else None

    def _inner_now(self, meter: str, phase: Optional[str], inner: str) -> float:
        """What inner meter ``inner`` draws now of ``meter``'s phase ``phase`` (all of it with None), a channel on
        several phases shared out evenly."""
        if phase is None:
            return self._now_w(inner) or 0.0
        target = set(self.fleet.phase_map(meter).get(phase, phase) if meter else phase)
        det, out = self.fleet.subs.get(inner), 0.0
        for c in (det.phases if det is not None else {}):
            sys = self.fleet.phase_map(inner).get(c, "")
            if sys:
                out += (self._now_w(inner, c) or 0.0) * len(set(sys) & target) / len(sys)
        return out

    def base_load_cap(self, meter: str, phase: Optional[str] = None, remainder: bool = False) -> Optional[float]:
        """The most a base load may read now (the owner, 2026-10-31): what its meter draws now less what the
        meters inside it draw now - the remainder's: the grid's less the meters beside it and inside it. None
        while the meter has no reading."""
        if remainder:
            if self.remainder_meter() is None:
                return None
            meter, phase = "", None                    # the remainder is what no read meter covers: the grid's
        now = self._now_w(meter, phase)
        if now is None:
            return None
        return max(0.0, now - sum(self._inner_now(meter, phase, n) for n in self._inside(meter)))

    def _read_parent(self, name: str) -> str:
        """The nearest read meter ``name`` hangs under on the Energy dashboard ("" the grid): a meter inside one
        detection does not read (Delavnica) is inside that one's parent - the Server, Centralna and the rest of
        the workshop's meters are inside the grid's base load, not beside it."""
        parent, seen = (self.submeters.get(name) or {}).get("parent"), {name}
        while parent and parent not in self.submeters and parent not in seen:
            seen.add(parent)
            parent = (self.all_submeters.get(parent) or {}).get("parent")
        return parent if parent in self.submeters else ""

    def _inside(self, meter: str) -> List[str]:
        """The read meters directly inside ``meter`` ("" the grid), a meter inside an unread one counted under the
        nearest read meter above it (_read_parent)."""
        return sorted(n for n in self.submeters if n != meter and self._read_parent(n) == meter)

    def base_load(self, meter: str, phase: Optional[str] = None) -> Tuple[Optional[float], float]:
        """(W, Wh) of a meter's base load as a load of its own: its lowest reading (Fleet.base_load) less the
        base loads of the meters directly inside it, never below 0 - each of those is a device or a circuit
        of its own, and its always-on draw was counted twice: Delavnica's held the Server UPS's ~95 W and
        Centralna's pumps, Mansarda's the EcoFlow and the kitchen fridge (the owner, 2026-10-31: "the
        submeters should always be subtracted"). One phase of a meter (``phase``) less what each inner
        meter's channels carry on that phase, a channel on several phases shared out evenly."""
        w, wh = self.fleet.base_load(meter, phase)
        for inner in self._inside(meter):
            if phase is None:
                iw, iwh = self.fleet.base_load(inner)
            else:
                target = set(self.fleet.phase_map(meter).get(phase, phase) if meter else phase)
                det, iw, iwh = self.fleet.subs.get(inner), 0.0, 0.0
                for c in (det.phases if det is not None else {}):
                    sys = self.fleet.phase_map(inner).get(c, "")
                    if not sys:
                        continue
                    share = len(set(sys) & target) / len(sys)
                    cw, cwh = self.fleet.base_load(inner, c)
                    iw, iwh = iw + (cw or 0.0) * share, iwh + cwh * share
            w = None if w is None else w - (iw or 0.0)
            wh -= iwh
        return (None if w is None else max(w, 0.0)), wh

    def base_load_keys(self) -> set:
        """The meters whose base load is a load of its own, by energy statistic (BASE_LOAD_GRID the grid):
        the ticked ones, or until the page is saved the grid and every meter holding several loads - an end
        device's floor is its own standby, not a base load (the owner, 2026-10-30: Hiša, Mansarda and
        Delavnica by default, not Blaž PC, the Server or Centralna)."""
        chosen = self.config.get(CONF_BASE_LOAD)
        if chosen is not None:
            return set(chosen)
        rem = self.remainder_meter()
        return ({BASE_LOAD_GRID} | ({rem[1]} if rem is not None else set())
                | {m["energy"] for name, m in self.submeters.items() if m.get("energy") and not self.holds_one_device(name)})

    def metered_device(self, name: str) -> Optional[str]:
        """The meter a named load IS - named after one that holds a single
        device - or None. See insights.named."""
        return metered_device(name, self.device_meters())

    def device_meters(self) -> List[str]:
        """The meters detection reads that hold one device, by name."""
        return sorted(m for m in self.submeters if self.holds_one_device(m))

    async def async_first_statistic(self, statistic_id: str) -> Optional[float]:
        """When a statistic's first long-term row starts (epoch s), or None
        while it has none. Every row is read to find it: a named load's meter
        is weeks old, not years."""
        rows = await get_instance(self.hass).async_add_executor_job(
            rec_stats.statistics_during_period, self.hass, datetime.fromtimestamp(0, timezone.utc), None,
            {statistic_id}, "hour", None, {"sum"})
        rows = rows.get(statistic_id)
        return float(rows[0]["start"]) if rows else None

    async def async_backfill_statistics(self, name: str, force: bool = True) -> Optional[Tuple[int, float]]:
        """Write what detection saw of a named load, hour by hour, over its
        energy meter's statistics - long-term and five-minute alike - for the
        hours detection watched, about ten days: the days before it was named
        show on the Energy dashboard, and a reset or the reading's first
        appearance no longer shows as a day's worth in one hour (the owner,
        2026-09-29: "rewrite the long term and short term statistics for
        detected entities for the time it has the data for"). See
        plan_rewrite.

        Before that window, first, what the meter is owed from elsewhere,
        once: the history of the names that vanished into this one
        (``carries``) and the hours its pairs kept from before they were
        named (Pair.older) - see plan_carry.

        Not ``force``d - the hourly check, a named load's sensor added - it
        writes only where the meter is off what detection saw, from the first
        hour that is (first_change), and nothing while history is re-read:
        the re-read's end rewrites every name.

        Returns (hours written, kWh they differ from what was recorded) -
        (0, 0.0) when there is nothing to do - or None while the meter has no
        hour of its own yet, or not forced while history is re-read. Running
        it again writes the same."""
        meter = self.metered_device(name)
        if meter:
            _LOGGER.info("Not backfilling %s: it is the metered device %s, whose own readings are its history",
                         name, meter)
            return 0, 0.0
        if self.refiling and not force:
            return None
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, load_uid(self.entry.entry_id, "energy", name))
        if entity_id is None:
            return 0, 0.0
        async with self._lock:                # the library, between passes: a pass changes it
            recorder = get_instance(self.hass)

            def recorded(period, since, statistic_id=entity_id):
                return recorder.async_add_executor_job(
                    rec_stats.statistics_during_period, self.hass, dt_util.utc_from_timestamp(since), None,
                    {statistic_id}, period, None, {"state", "sum"})

            async def window():
                # the row before the window and every one since - one read an
                # hour; from the meter's first where it has none there:
                # younger than the window, or a gap in its rows
                rows = (await recorded("hour", covered - 3600)).get(entity_id) or []
                if rows and rows[0]["start"] < covered:
                    return rows
                return (await recorded("hour", 0)).get(entity_id) or []
            covered = self._covered_from()
            if covered is None:
                return None if await self.async_first_statistic(entity_id) is None else (0, 0.0)
            hours = await window()
            if not hours:
                return None
            meta = await recorder.async_add_executor_job(
                functools.partial(rec_stats.get_metadata, self.hass, statistic_ids={entity_id}))
            unit = meta[entity_id][1]["unit_of_measurement"] if entity_id in meta else None
            if unit != UnitOfEnergy.KILO_WATT_HOUR:
                _LOGGER.warning("Not backfilling %s: %s keeps its statistics in %s, not kWh", name, entity_id, unit)
                return 0, 0.0
            owed = self.carries.get(name) or []
            extra: Dict[int, float] = {}
            for statistic_id in owed:
                last = 0.0                    # a meter's sum starts at 0 before its first row
                for r in (await recorded("hour", 0, statistic_id)).get(statistic_id) or []:
                    if r["start"] >= covered:
                        break
                    extra[r["start"]] = extra.get(r["start"], 0.0) + r["sum"] - last
                    last = r["sum"]
            older = self.fleet.take_older(name)
            for hour, wh in older.items():
                extra[hour] = extra.get(hour, 0.0) + wh / 1000.0
            if owed or older:
                self.carries.pop(name, None)
                # taken before it is written: a restart in between loses it
                # rather than carrying it twice
                await self._persist(force=True)
            if extra:
                every = (await recorded("hour", 0)).get(entity_id) or []
                fives = (await recorded("5minute", min(extra))).get(entity_id) or []
                hour_rows, five_rows, shift = plan_carry(extra, covered, every, fives)
                await self._write_statistics(entity_id, unit, hour_rows, five_rows, covered, shift)
                _LOGGER.info("Carried %.2f kWh onto %s (%s) from before %s: %s", shift, name, entity_id,
                             dt_util.utc_from_timestamp(covered).isoformat(),
                             ", ".join(owed + ([f"{len(older)} hours kept from before it was named"] if older else [])))
                hours = await window()
            hourly = self.fleet.hourly_by_name(name)
            hour_rows, _, shift = plan_rewrite(hourly, covered, hours, [])
            start = hour_rows[0]["start"] if force and hour_rows else first_change(hour_rows, hours)
            if start is None:
                return 0, 0.0
            # five-minute rows from the first hour written: those before it
            # agree with their hours already
            fives = (await recorded("5minute", start)).get(entity_id) or []
            _, five_rows, _ = plan_rewrite(hourly, covered, hours, fives)
            hour_rows = [r for r in hour_rows if r["start"] >= start]
            # ...and the hour being recorded, and any compiled since the rows were read
            await self._write_statistics(entity_id, unit, hour_rows, five_rows, hour_rows[-1]["start"] + 3600, shift)
        _LOGGER.log(logging.INFO if force else logging.DEBUG,
                    "Rewrote %s (%s): %d hours and %d five-minute rows from %s, %.2f kWh %s than recorded",
                    name, entity_id, len(hour_rows), len(five_rows),
                    dt_util.utc_from_timestamp(hour_rows[0]["start"]).isoformat(), abs(shift),
                    "more" if shift >= 0 else "less")
        return len(hour_rows), shift

    def _covered_from(self) -> Optional[int]:
        """The first whole hour of the statistics' window: the first hour the
        library read, never further back than HOURLY_KEEP_S - before it some
        pairs' hours have aged out (Pair.age) and others not, and
        a name's meter would be written short of them, every hour. None
        before anything is filed."""
        seen = min((h for _, det in self.fleet.detectors() for pair in det.pairs for h in pair.hourly),
                   default=None)
        if seen is None:
            return None
        since = max(seen, dt_util.utcnow().timestamp() - HOURLY_KEEP_S)
        return int(since // 3600 * 3600) + 3600     # the first hour read is a part of one

    async def _write_statistics(self, entity_id: str, unit: str, hour_rows: List[dict], five_rows: List[dict],
                                shift_from: float, shift: float) -> None:
        """Write rows over a named load's meter's statistics, and raise every
        row from ``shift_from`` on by ``shift``: async_adjust_statistics moves
        the five-minute rows as well as the hours (recorder's
        adjust_statistics), so the two tables keep agreeing."""
        recorder = get_instance(self.hass)
        # exactly what the sensor's own statistics carry, so importing
        # changes nothing about them but the rows
        metadata = {"has_sum": True, "mean_type": StatisticMeanType.NONE, "name": None, "source": "recorder",
                    "statistic_id": entity_id, "unit_class": "energy", "unit_of_measurement": unit}
        for table, rows in ((Statistics, hour_rows), (StatisticsShortTerm, five_rows)):
            recorder.async_import_statistics(metadata, [
                {"start": dt_util.utc_from_timestamp(r["start"]), "state": r["state"], "sum": r["sum"]}
                for r in rows], table)
        recorder.async_adjust_statistics(entity_id, dt_util.utc_from_timestamp(shift_from), shift, unit)
        await recorder.async_block_till_done()   # written before another run reads

    async def _rewrite_named(self) -> None:
        for name in sorted(self.fleet.names()):
            await self.async_backfill_statistics(name)

    @callback
    def _hour_compiled(self, _event) -> None:
        self.entry.async_create_background_task(self.hass, self._check_named(), f"{DOMAIN} check statistics")

    async def _check_named(self) -> None:
        """Each hour Home Assistant has compiled, every named load's meter is
        held against what detection credits it with now, and rewritten where
        it is off (async_backfill_statistics, unforced): a name moved, joined,
        adopted or forgotten, detection merging a load into a named one, a run
        filed into the hours it ran in rather than the one it closed in - one
        check for them all, instead of a hook in each.

        Its window ends at the hour just compiled, read once the compile is in
        (the event fires before it commits). No margin is left before it: a
        run still open is in neither the meter's reading - which counts runs
        as they close - nor in hourly, so it never makes them differ. What
        can is a run counted after the window's end but filed inside it, and
        right after the hour there is next to none. A margin would make that
        the rule: every run across its edge, counted after it and filed
        before, would be written into the hours before and counted again
        after - then taken back an hour later, every hour. A check at another
        moment - a sensor added at a start or a reload, the service - counts
        twice what was counted since the hour began but filed before it,
        until this check the hour after."""
        began = time.monotonic()
        await get_instance(self.hass).async_block_till_done()
        drained = time.monotonic()
        try:
            await self.autonamer.async_check()
        except Exception:  # noqa: BLE001 - a fault in auto-naming must not stop the statistics check
            _LOGGER.exception("Auto-naming failed")
        named = time.monotonic()
        names = [n for n in sorted(self.fleet.names()) if not self.metered_device(n)]
        for name in names:
            await self.async_backfill_statistics(name, force=False)
        # what the hour's check costs (2026-11-09: the pass right after 12:00 at Home read for 64 s)
        self.last_hourly_check = {"drain_s": round(drained - began, 1), "autoname_s": round(named - drained, 1),
                                  "names": len(names), "statistics_s": round(time.monotonic() - named, 1)}
        _LOGGER.debug("Hourly statistics check: %s", self.last_hourly_check)

    def naming_groups(self) -> list:
        """What is waiting to be named, one group per meter: ``(meter, offered,
        waiting)``, each offered row ``((meter, id), pair)`` - the grid's
        id under "" - the loads under no meter first as ``"main"``, then the
        meters by how much their loads use.

        Split by where each load lives, so the page can be taken one circuit
        at a time (2026-09-25: "any way of splitting the detected loads
        early would help with organisation and naming"), and a meter's list
        is its own detector's pairs (Fleet.namable). Named loads have a
        page of their own (``named``). Each group gets the length
        offer_for_naming earns it, and ``waiting`` is how many more cleared
        the bar than that length fits."""
        named = len(self.fleet.names())
        out = []
        for where, rows in self.fleet.namable(self.holds_one_device, MIN_COUNT_TO_NAME, NAMING_MIN_WH).items():
            ref = {id(pair): r for r, pair in rows}
            heirs = {id(pair) for r, pair in rows if self.fleet.predecessor_of(r) is not None}
            worth = [pair for _, pair in rows]
            shown = offer_for_naming(worth, named, DEFAULT_MIN_EVIDENCE, NAMING_MIN_ROWS,
                                     NAMING_START_ROWS, NAMING_ROWS_PER_NAME, is_heir=lambda x: id(x) in heirs)
            if shown:
                clear = clears_for_naming(worth, DEFAULT_MIN_EVIDENCE, NAMING_MIN_ROWS, lambda x: id(x) in heirs)
                out.append((where, [(ref[id(x)], x) for x in shown], max(0, len(clear) - len(shown))))
        out.sort(key=lambda g: (g[0] != "main", -sum(x.energy_wh for _, x in g[1])))
        return out

    def active_by_name(self) -> Dict[str, float]:
        """Fleet.active_by_name now, worked out once for all the named loads' running sensors after a
        pass: each asks twice, which was 146 walks of every open run a pass at Home for one answer
        (2026-11-09). Kept while no pass has run since and within the same second; a rename reloads
        the entry, so a new runner. Shared: read it, never write."""
        now = dt_util.utcnow().timestamp()
        key = (self.last_processed, int(now))
        if self._active_memo is None or self._active_memo[0] != key:
            self._active_memo = (key, self.fleet.active_by_name(now))
        return self._active_memo[1]

    async def _input_states(self, eid: str, domain: str, since: datetime, end: datetime) -> list:
        """The input's recorder states over (since, end), as _read_inputs' own query returns them, from kept
        rows and only what came since the last read (2026-11-09): every pass re-read SWITCH_MEMORY_S (4 h)
        of every input's history - ~1 s of each pass at Home, 19 inputs.

        Every row is kept, attribute-only updates too (get_significant_states, all changes), because that
        is what the recorder's start row is made of: the newest row of ANY kind before the window, with its
        own time (history._get_single_entity_start_time_stmt, LazyState keeps last_updated). The window is
        then built as the recorder builds it - that start row, then the rows strictly inside it, and for
        everything but a climate or a fan (read by an attribute) only the state changes (last_changed ==
        last_updated, as state_changes_during_period). Read whole the first time, when the window has moved
        back (the first pass after a restart reads further) or past what is kept; and every
        INPUT_AUDIT_PASSES passes the old full read is made too and compared: on any difference it is the
        one used and the kept rows start again (input_cache_check in the diagnostics counts both)."""
        async def own(rows):
            # the start row asked of the recorder itself, over a window one microsecond wide - the same function
            # and start-row statement as the full read (rebuilt from kept rows it differed at Home in 35 of 36
            # audits, by seconds, 2026-11-09); the rows inside the window from the kept ones
            changes = domain not in ("climate", "fan")
            s, e = since.timestamp(), end.timestamp()
            start = [st for st in (self._input_starts or {}).get(eid, []) if st.last_updated.timestamp() <= s][:1]
            inside = [st for st in rows if s < st.last_updated.timestamp() < e
                      and (not changes or st.last_changed == st.last_updated)]
            return start + inside

        recorder = get_instance(self.hass)

        def read(frm, start_row):
            return recorder.async_add_executor_job(functools.partial(
                history.get_significant_states, self.hass, frm, end, [eid],
                include_start_time_state=start_row, significant_changes_only=False))

        kept = self._input_cache.get(eid)
        pre = self._input_rows.get(eid)              # this pass's batch read (_batch_inputs), when it covers this read
        if kept is None or kept[0] > since or kept[1] < since:
            rows = list(pre[1]) if pre is not None and pre[0] is None else list((await read(since, True)).get(eid, []))
        else:
            # read again from INPUT_OVERLAP_S before the last read's end: a reading stamped before that end but
            # committed after the read (Home's boiler temperature, one row in 1,369, 2026-11-09) is found
            # the next time, and the kept rows give way to what was read again from there
            frm = kept[1] - timedelta(seconds=INPUT_OVERLAP_S)
            edge = frm.timestamp()
            if self._live:
                # streaming: its state changes since, as the recorder would hand them back (< end), the buffer
                # trimmed to what the next read's overlap can still ask for
                buf = self._input_buf.get(eid) or []
                got = [st for st in buf if edge <= st.last_updated.timestamp() < end.timestamp()]
                # ...with the kept rows from the edge on: the first live read's edge can lie before listening
                # began, and what changed between is only in the rows the recorder handed the last gap read
                have = {(st.last_updated, st.state) for st in got}
                got = sorted([st for st in kept[2] if st.last_updated.timestamp() >= edge
                              and (st.last_updated, st.state) not in have] + got, key=lambda st: st.last_updated)
                self._input_buf[eid] = [st for st in buf if st.last_updated.timestamp() >= end.timestamp() - 2 * INPUT_OVERLAP_S]
            else:
                got = (pre[1] if pre is not None and pre[0] is not None and pre[0] <= frm - timedelta(microseconds=1)
                       else (await read(frm - timedelta(microseconds=1), False)).get(eid, []))
            new = [st for st in got if st.last_updated.timestamp() >= edge]
            rows = [st for st in kept[2] if st.last_updated.timestamp() < edge] + new
            # all after since, and the newest before it - the start row of this window and the next
            cut = next((i for i, st in enumerate(rows) if st.last_updated >= since), len(rows))
            rows = rows[max(0, cut - 1):]
        self._input_cache[eid] = (since, end, rows)
        got = await own(rows)
        # the audit once a day per input, at a minute of the day of its own (INPUT_AUDIT_PASSES minutes): by the
        # clock, now a step runs every second rather than a pass every minute
        if self._audit_due(eid):
            self._input_passes[eid] = int(time.time() // 60)
            full = await self._input_states_full(eid, domain, since, end)
            c = self.input_cache_check
            c["compared"] = c.get("compared", 0) + 1
            key = lambda st: (st.state, st.last_updated.timestamp(), st.attributes.get("hvac_action") if domain == "climate"  # noqa: E731
                              else st.attributes.get("percentage") if domain == "fan" else None)
            if [key(st) for st in got] != [key(st) for st in full]:
                c["different"] = c.get("different", 0) + 1
                c.setdefault("first", {"entity": eid, "cached": [str(key(st)) for st in got[:3]],
                                       "full": [str(key(st)) for st in full[:3]], "lengths": [len(got), len(full)]})
                self._input_cache.pop(eid, None)
                return full
        return got

    def _audit_due(self, eid: str) -> bool:
        """Whether an input's daily audit (_input_states_full) falls in this minute and has not run in it."""
        minute = int(time.time() // 60)
        return (minute % INPUT_AUDIT_PASSES == zlib.crc32(eid.encode()) % INPUT_AUDIT_PASSES
                and self._input_passes.get(eid) != minute)

    async def _start_rows(self, eids: List[str], since: datetime) -> Dict[str, list]:
        """Each input's start row at ``since`` - the newest row of any kind before it, as both full reads hand
        it back (the recorder's start-time statement) - for them all in one query over a window one
        microsecond wide."""
        if not eids:
            return {}
        return await get_instance(self.hass).async_add_executor_job(functools.partial(
            history.get_significant_states, self.hass, since, since + timedelta(microseconds=1), list(eids),
            include_start_time_state=True, significant_changes_only=False))

    async def _batch_inputs(self, eids: List[str], since: datetime, end: datetime) -> None:
        """This pass's input rows in at most two recorder queries instead of one an input (the owner, 2026-11-11:
        "lets read things in 1 query"; 19 inputs at Home, most of the ~88 get_significant_states calls in a
        two-minute profile): the inputs _input_states reads whole, from ``since`` with their start rows, in one;
        the ones it reads on from its kept rows in another, from the earliest of their INPUT_OVERLAP_S edges -
        each input then keeps only its rows from its own edge on, as its own read did. The same function and
        options as the reads they replace; the daily audit (_input_states_full) still reads each input alone."""
        self._input_rows = {}
        whole, on = [], []
        for eid in eids:
            kept = self._input_cache.get(eid)
            (whole if kept is None or kept[0] > since or kept[1] < since else on).append(eid)
        recorder = get_instance(self.hass)

        def read(frm, ids, start_row):
            return recorder.async_add_executor_job(functools.partial(
                history.get_significant_states, self.hass, frm, end, list(ids),
                include_start_time_state=start_row, significant_changes_only=False))

        if whole:
            got = await read(since, whole, True)
            self._input_rows.update({eid: (None, list(got.get(eid, []))) for eid in whole})
        if on:
            frm = (min(self._input_cache[eid][1] for eid in on) - timedelta(seconds=INPUT_OVERLAP_S)
                   - timedelta(microseconds=1))
            got = await read(frm, on, False)
            self._input_rows.update({eid: (frm, list(got.get(eid, []))) for eid in on})

    async def _input_states_full(self, eid: str, domain: str, since: datetime, end: datetime) -> list:
        """The input's states read whole, as every pass did until 2026-11-09 - the audit _input_states is held to."""
        if domain in ("climate", "fan"):
            states = await get_instance(self.hass).async_add_executor_job(functools.partial(
                history.get_significant_states, self.hass, since, end, [eid],
                include_start_time_state=True, significant_changes_only=False))
        else:
            states = await get_instance(self.hass).async_add_executor_job(
                history.state_changes_during_period, self.hass, since, end, eid, True, False, None, True,
            )
        return list(states.get(eid, []))

    def names_now(self) -> Dict[str, list]:
        """Fleet.names, worked out once per pass and second for every entity that reads it: each
        named load's running sensor walked every pair of every meter for it, 73 walks a pass at Home
        (2026-11-09). Shared: read it, never write."""
        key = (self.last_processed, int(dt_util.utcnow().timestamp()))
        if self._names_memo is None or self._names_memo[0] != key:
            self._names_memo = (key, self.fleet.names())
        return self._names_memo[1]

    def named(self) -> list:
        """Every named load as ``((meter, id), pair)``, biggest first, wherever it was seen."""
        return sorted((((m, s.id), s) for m, det in self.fleet.detectors() for s in det.pairs if s.name),
                      key=lambda r: -r[1].energy_wh)

    async def async_adopt(self, ref: Tuple[str, int]) -> Optional[str]:
        """Move a predecessor's name onto the pair (meter, id) - the
        grid's under "" - and persist, between passes, as async_rename."""
        async with self._lock:
            before = self.fleet.names()
            name = self.fleet.adopt(ref)
            if name is None:
                return None
            self._carry_vanished(before)
            await self._persist(force=True)
        self.async_update_listeners()
        return name

    async def async_rename(self, ref: Tuple[str, int], name: Optional[str], signature: bool = False) -> bool:
        """Name the pair (meter, id) - the grid's under "" - or clear it (``signature``: ref's id is a
        signature's), and persist at once; the caller bumps the entry so the entities follow. Either way the
        name goes to the whole signature: a signature is named, not a pair.

        Without telling the entities: the bump's reload is what makes the
        name's sensors. Told here, the sensor platform added them too, and
        name_load's reload began while that add was still running; an add
        that finished after the unload left a live twin the new platform
        refused ("ID ..._load_energy_kiln already exists"), frozen on the
        old runner (37 % of name_load calls in a scratch HA test,
        2026-10-02).

        Between passes, never under one: a pass works on the library in an
        executor thread, and a rename landing then changed it under the pass
        (AGENTS.md, 2026-10-02)."""
        async with self._lock:
            before = self.fleet.names()
            if not (self.fleet.rename_signature(ref, name) if signature else self.fleet.rename(ref, name)):
                return False
            self._carry_vanished(before)
            await self._persist(force=True)      # a user action, written at once
        return True

    def _carry_vanished(self, before: Dict[str, list]) -> None:
        """After a rename or an adoption: a name that vanished into another
        (vanished_into) leaves that name's meter its own meter's history, by
        statistic id, and whatever it was owed itself - carried before the
        window once (async_backfill_statistics). The meter it leaves keeps its
        statistics as they are. A name only told apart by case or spaces is
        the same meter (load_uid), which owes itself nothing."""
        after = self.fleet.names()
        registry = er.async_get(self.hass)
        for into, gone in vanished_into(before, after).items():
            for name in gone:
                uid = load_uid(self.entry.entry_id, "energy", name)
                eid = registry.async_get_entity_id("sensor", DOMAIN, uid)
                own = [eid] if eid and uid != load_uid(self.entry.entry_id, "energy", into) else []
                self.carries[into] = self.carries.get(into, []) + self.carries.pop(name, []) + own
        self.carries = {n: ids for n, ids in self.carries.items() if n in after and ids}

    async def _persist(self, force: bool = False) -> None:
        """Write the state, but not on every pass.

        The snapshot is the whole library and runs to well over a hundred
        kilobytes; written every five minutes that is tens of megabytes a day,
        and it is the single thing that would multiply if detection ran every
        minute instead (2026-09-18, whose suggestion this is: keep it in
        memory and write it at most hourly).

        async_delay_save alone will not do it. Called again before its delay
        expires it POSTPONES the pending write rather than letting it run, so
        a pass every minute against an hourly delay would never write at all
        until shutdown - and an ungraceful one would lose everything since the
        last write. So the delayed save is the safety net (it also registers
        Home Assistant's final-write listener, which is what makes a clean
        shutdown durable) and the elapsed check is the floor. The library
        itself is no Store's any more (_write_store): its safety net is
        _final_write, the elapsed check its floor as before.

        Every caller holds _lock or runs when no pass can (async_stop), so
        nothing changes the library while _write_store walks it.
        """
        now = dt_util.utcnow().timestamp()
        due = self._saved_at is None or now - self._saved_at >= SAVE_MAX_INTERVAL_S
        # the older hours first: a reset's empty ones are never left beside
        # the library they would no longer match. Daily, not hourly - a year
        # of them is ~1.5 MB at Home, and a crash loses only hours that would
        # have been carried, never carries one twice (a carry forces a write)
        if force or self._history_saved_at is None or now - self._history_saved_at >= HISTORY_SAVE_INTERVAL_S:
            await self._history.async_save(self._older())
            self._history_saved_at = now
        else:
            self._history.async_delay_save(self._older, HISTORY_SAVE_INTERVAL_S)
        if force or due:
            await self.hass.async_add_executor_job(self._write_store)
            self._saved_at = now

    def _snapshot_items(self):
        yield "fleet", self.fleet.dict_items()
        yield "last_processed", self.last_processed.isoformat() if self.last_processed else None
        yield "refiling", self.refiling
        yield "carry", self.carries
        yield "generation", DETECTOR_GENERATION

    def _write_store(self) -> None:
        """The library straight to its file, a meter's detector at a time, in
        an executor thread (2026-11-09). Through Store it was one ~21 MB dict
        (Home), built in the event loop and then serialised whole: on Home's
        2 GB Yellow, already in swap, that froze Home Assistant for 50-130 s
        once an hour ("Finished fetching load_insights detection data in 51.4
        seconds" against 5-11 s for an ordinary pass). The file is the Store's
        own - same path, same envelope, Home Assistant's own encoder, compact
        instead of indented - so a library saved either way loads either way;
        it is replaced atomically, as Store does. Not through Store because the
        Store manager remembers which files existed when Home Assistant started:
        a file it did not write would read as missing at the next reload."""
        began = time.monotonic()
        tmp = f"{self._store_path}.tmp"
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as fh:
            dump_items(fh.write, iter([("version", STORAGE_VERSION), ("minor_version", 1), ("key", self._store_key),
                                       ("data", self._snapshot_items())]), json_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._store_path)
        _LOGGER.debug("Library saved: %.1f MB in %.1f s", os.path.getsize(self._store_path) / 1e6,
                      time.monotonic() - began)

    async def _final_write(self, _event) -> None:
        """Home Assistant is stopping: write the library once more (a stop does
        not unload the entry, so async_stop does not run)."""
        async with self._lock:
            await self._persist(force=True)

    def profile_passes(self, passes: int) -> None:
        """Profile the detection of the next ``passes`` passes (load_insights.profile_passes,
        2026-11-09): what a pass costs on the site's own hardware, which a replay on a
        laptop cannot say (Home's Yellow: 2.6-9.5 s a pass against 71 ms replayed).
        cProfile in the executor thread fleet.process runs in - Home Assistant's own
        profiler sees only the event loop's thread."""
        import cProfile  # noqa: PLC0415 - only when asked for
        self._profiler = cProfile.Profile()
        self._profile_left = passes
        self._profile_rows = []
        self._listener_s = {}

    def profile_seconds(self, seconds: int) -> None:
        """Profile the whole process for ``seconds`` (load_insights.profile_passes with ``seconds``,
        2026-11-10): what every integration costs on the site's own hardware, not only what runs
        while detection does - the pass profile catches another integration only in the second or
        two a pass takes, so a per-minute figure from it is a guess (SolarEdge's "2.4 s a minute")."""
        import cProfile  # noqa: PLC0415 - only when asked for
        prof = cProfile.Profile()
        prof.enable()                 # sys.monitoring: every thread, not only the event loop's
        self._window = prof           # one profiler at a time (__init__.py refuses a second)

        async def done(_now) -> None:
            prof.disable()
            self._window = None
            path, report = await self.hass.async_add_executor_job(self._profile_report, prof)
            _LOGGER.warning("Load Insights profile of %d s of the whole process, written to %s\n%s",
                            seconds, path, report)

        async_call_later(self.hass, seconds, done)

    def _timed_update_listeners(self) -> None:
        """async_update_listeners, each entity timed (load_insights.profile_passes): which of
        the many entities fed by a pass costs the event loop its time."""
        for update_callback, _ in list(self._listeners.values()):
            began = time.perf_counter()
            try:
                update_callback()
            except Exception:  # noqa: BLE001 - as async_update_listeners
                _LOGGER.exception("Unexpected error updating a listener of %s", self.name)
            who = getattr(getattr(update_callback, "__self__", None), "entity_id", None) or repr(update_callback)
            self._listener_s[who] = self._listener_s.get(who, 0.0) + time.perf_counter() - began

    async def _profile_pass_done(self) -> None:
        self._profile_rows.append(dict(self.last_pass))
        self._profile_left -= 1
        if self._profile_left > 0:
            return
        prof, passes, listeners = self._profiler, self._profile_rows, self._listener_s
        self._profiler, self._profile_rows, self._listener_s = None, [], {}
        path, report = await self.hass.async_add_executor_job(self._profile_report, prof)
        slowest = sorted(listeners.items(), key=lambda kv: -kv[1])[:15]
        _LOGGER.warning("Load Insights profile of %d passes, written to %s\nEach pass: %s\n"
                        "Slowest entities (s over all %d passes, %d entities): %s\n%s",
                        len(passes), path, passes, len(passes), len(listeners),
                        [(who, round(s, 3)) for who, s in slowest], report)

    def _profile_report(self, prof) -> tuple:
        import io  # noqa: PLC0415
        import pstats  # noqa: PLC0415
        path = self.hass.config.path(f"load_insights_profile_{dt_util.utcnow():%Y%m%dT%H%M%S}.pstats")
        prof.dump_stats(path)
        out = io.StringIO()
        # full paths: the profile sees every thread, and a bare "sensor.py:330" (23.7 s in Home's fourth
        # profile, 2026-11-09) cannot say which integration's it is
        stats = pstats.Stats(prof, stream=out)
        owners = _owner_seconds(stats.stats)
        out.write("CPU seconds by owner: " + ", ".join(f"{o} {s:.2f}" for o, s in owners[:30]) + "\n\n")
        stats.sort_stats("tottime").print_stats(25)
        stats.print_callers(8)                      # who calls the hottest: a count alone does not say where to cut
        stats.sort_stats("cumulative").print_stats(30)
        return path, out.getvalue()

    def _older(self) -> dict:
        return {"older": self.fleet.older_to_dict(), "generation": DETECTOR_GENERATION}

    @property
    def behind_s(self) -> Optional[float]:
        """How far behind now the detector has read, in seconds.

        None before it has read anything at all. The backfill walks ten days
        in six-hour slices, so a library part way through that is a library
        of whatever happened to be in the first few days - which is not a
        thing to offer anyone for naming (2026-09-18)."""
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
        if any(self.config.get(f"{GRID_PREFIX}power_{p}") for p in PHASES):
            return True
        return any(inv.get("power") or any(inv.get(f"power_{p}") for p in PHASES)
                   for inv in (self.entry.options.get(CONF_INVERTERS) or []))

    async def async_start(self) -> None:
        # the file _write_store writes, or a Store wrote before it: the same envelope
        stored = await self.hass.async_add_executor_job(load_json, self._store_path, {})
        raw = (stored.get("data") if isinstance(stored, dict) else None) or {}
        orphans: list = []
        if raw and raw.get("generation") != DETECTOR_GENERATION:
            # The library was learned by a detector that no longer exists, so
            # it goes. The NAMES do not: they are the user's, not ours, and
            # dropping them behind a log line would take their devices, their
            # energy meters and their place on the Energy dashboard with them
            # - on every installation at once, the first time this constant
            # moves (the owner asked what an update does to a named load,
            # 2026-09-22). Read defensively: the whole point is that the old
            # shape may not be one we still understand.
            orphans = names_in_store(raw)
            _LOGGER.info(
                "Load detection was learned by an older detector; starting its "
                "library again, keeping %d name(s) to re-attach", len(orphans)
            )
            raw = {}
        await self.autonamer.async_load()
        self.fleet = Fleet.from_dict(raw.get("fleet"))
        if orphans:
            self.fleet.carry_names(orphans)
        self.fleet.main.tz_offset_s = dt_util.now().utcoffset().total_seconds()
        # before the platforms, which leave out the loads that ARE a metered
        # device; resolved again once a minute and on a registry change (_refresh_config)
        await self._refresh_config()
        lp = raw.get("last_processed")
        self.last_processed = dt_util.parse_datetime(lp) if lp else None
        self.refiling = bool(raw.get("refiling")) or self.last_processed is None
        self.carries = {n: list(ids) for n, ids in (raw.get("carry") or {}).items()}
        if raw:                                  # this library, not one dropped for an older generation
            older = await self._history.async_load() or {}
            if older.get("generation") == DETECTOR_GENERATION:
                self.fleet.load_older(older.get("older"))
            # the steps store of its own is gone: a step group keeps its steps
            # (StepGroup.recent, 2026-10-30) - the old file removed
            await self._steps.async_remove()
        self.entry.async_on_unload(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_FINAL_WRITE, self._final_write))
        if not self.enabled:
            return
        self.entry.async_on_unload(
            self.hass.bus.async_listen(EVENT_RECORDER_HOURLY_STATISTICS_GENERATED, self._hour_compiled))
        # a caught-up pass starts on the clock's minute (the owner, 2026-11-09), not a minute after
        # the last one ended, which made every pass's own length drift the schedule (65-80 s at Home)
        self.entry.async_on_unload(async_track_time_change(self.hass, self._on_minute, second=0))
        # streaming: a step every second (_tick), the stream's listener (_listen), a registry change resolved
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._tick, timedelta(seconds=STREAM_TICK_S)))
        self.entry.async_on_unload(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed))
        self.entry.async_on_unload(self._stop_listening)
        self.hass.async_create_task(self.async_refresh())

    @callback
    def _stop_listening(self) -> None:
        if self._unlisten is not None:
            self._unlisten()
            self._unlisten = None

    @callback
    def _registry_changed(self, _event) -> None:
        if not self._stopping:
            self.hass.async_create_task(self._refresh_config_locked())

    async def _refresh_config_locked(self) -> None:
        async with self._lock:                   # never under a step: it changes the detector's meters
            await self._refresh_config()

    async def _refresh_config(self) -> None:
        """What the assembly reads, resolved again: the Energy dashboard's site and its meters (which detection
        reads, how they nest, which are waited for). At the start, once a minute (_minute) and on an entity
        registry change - the minute pass did it every pass."""
        try:
            manager = await async_get_manager(self.hass)
            self._site = energy_site(self.hass, manager.data)
        except Exception:  # noqa: BLE001 - a missing dashboard is not an error
            self._site = None
        await self._refresh_submeters()
        self._resolve_memo = {}                      # the triples resolved again (_coherent_triple)
        for name, meter in self.all_submeters.items():
            if meter.get("ignored"):
                self.fleet.subs.pop(name, None)     # left out on the settings page: as if never read
        self.fleet.parents = {n: m.get("parent") for n, m in self.submeters.items()}
        # not waited for: as the settings page says, and any meter the
        # dashboard no longer resolves, whose detector is kept unread
        self.fleet.horizon_skip = ({n for n, m in self.submeters.items() if m.get("horizon_skip")}
                                   | (set(self.fleet.subs) - set(self.submeters)))
        self.fleet.cadence_forced = {n: m["cadence"] for n, m in self.submeters.items() if m.get("cadence") is not None}

    @callback
    def _on_minute(self, _now) -> None:
        """Every minute of the clock: the minute's work for the live stream (_minute)."""
        if self._stopping or self.entry.pref_disable_polling:
            return
        self.hass.async_create_task(self._minute())

    async def async_reset(self, forget_names: bool = False) -> None:
        """Forget everything learned and start the backfill again.

        Everything except the NAMES, unless ``forget_names``. They are the
        one thing in the library the user put there by hand, and the backfill
        re-learns everything else in minutes. Each is carried across as a
        description and handed back to the first rebuilt pair that looks
        like it; where the site really has changed - the reason to do this by
        hand - nothing matches and the name does not return. The owner
        (2026-09-30) would rather drop them than have a stale one land on the
        wrong load, so the service can forget them too.

        Between passes, never under one: a pass running when this lands
        finishes by recording that it has processed up to now, and the ten
        days are never re-read (Home, 2026-09-29: one pair, from the
        minute of the reset)."""
        async with self._lock:
            orphans = self.fleet.name_descriptors() if self.fleet and not forget_names else []
            if forget_names:
                self.carries = {}               # owed to names that are gone
            self.autonamer.reset(forget_names)
            self.fleet = Fleet()
            self._livepower = LivePower()
            self.fleet.carry_names(orphans)
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
        self._stopping = True                    # nor does the clock start one
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
        """One step of detection (_step): the coordinator's update, called every STREAM_TICK_S by _tick. Never
        two at once: a step still running skips the tick."""
        if self._lock.locked() or not self.enabled:
            return
        async with self._lock:
            try:
                await self._step()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Load detection run failed")

    async def _step(self) -> None:
        """The readings since last_processed into the detector (streaming pipeline, step 2 - the minute pass is
        gone, the owner 2026-11-11). Live - the stream has listened since before last_processed - the window is
        everything up to STREAM_SETTLE_S ago, from the stream's buffer, ingested and run (advance) in one
        executor job; the minute timer settles it (_minute). Otherwise a gap - a fresh start, the stretch a
        restart missed - read from the recorder a slice at a time and closed like a pass (process), as the
        backfill always was. Both assemble their window with the same code (_assemble)."""
        began = time.monotonic()
        now = dt_util.utcnow()
        start = self.last_processed or (now - timedelta(days=DETECTION_BACKFILL_DAYS))
        live = self._listen_from is not None and start.timestamp() >= self._listen_from
        self._live = live
        if live:
            end = now - timedelta(seconds=STREAM_SETTLE_S)
            end = dt_util.utc_from_timestamp(self._stream.quiet_end(start.timestamp(), end.timestamp(),
                                                                    self._wanted, COMBINE_SETTLE_S))
            if end <= start:
                return
            rows = self._stream.window(start.timestamp(), end.timestamp(), self._wanted)
        else:
            try:
                await asyncio.wait_for(get_instance(self.hass).async_block_till_done(), RECORDER_FLUSH_S)
            except asyncio.TimeoutError:
                _LOGGER.debug("The recorder did not drain in %.0f s; the gap waits", RECORDER_FLUSH_S)
                return
            await self._refresh_config()             # every slice, as every pass did: a reset's new fleet too
            end = min(now, start + timedelta(hours=DETECTION_SLICE_HOURS))
            rows = await self._recorder_rows(start, end, self._wanted)
        read = time.monotonic()
        switches, numbers, inputs = await self._read_inputs(start, end)
        for _ in range(3):
            self._pass_rows = (start, end, rows)
            self._missing = set()
            self.fleet.wait_cap_s = self.meter_wait_s
            job = functools.partial(self._hot, start, end, switches, numbers, inputs, settle=not live)
            if self._profiler is not None:
                job = functools.partial(_profiled, self._profiler, job)
            fed = await self.hass.async_add_executor_job(job)
            if fed:
                break
            # an entity the window has no rows for: the recorder's, for this window, and listened to from now on
            rows = {**rows, **await self._recorder_rows(start, end, self._missing)}
            self._listen()
        self._pass_rows = None
        self._write_live()
        detected = time.monotonic()
        self.last_pass = {"live": live, "read_s": round(read - began, 3), "detect_s": round(detected - read, 3),
                          "hours": round((end - start).total_seconds() / 3600.0, 4),
                          "late_dropped": sum(self._stream.late.values())}
        self.last_processed = end
        self.last_run = now
        if live:
            self.caught_up = True
            if self._profiler is not None:
                await self._profile_pass_done()
            return
        # a gap slice closes like a pass did: the live figures, the save, the entities, the dashboard list
        self.caught_up = end >= now - timedelta(minutes=1)
        if self._listen_from is None:
            self._listen()
        await self._persist()
        self.async_update_listeners()
        if self.caught_up and self.refiling:
            self.refiling = False
            # the ten days re-read are the named loads' history now
            self.entry.async_create_background_task(self.hass, self._rewrite_named(), f"{DOMAIN} rewrite statistics")
        if self._profiler is not None:
            await self._profile_pass_done()

    def _hot(self, start: datetime, end: datetime, switches, numbers, inputs, settle: bool) -> bool:
        """The executor job of a step: the window assembled (_assemble) and fed to the detector (Fleet.ingest),
        the stream run up to its end (Fleet.advance) - and for a gap, closed like a pass (Fleet.settle). False,
        and nothing fed, when the window lacked an entity's rows (_missing): _step reads them and calls again."""
        got = self._assemble(start, end)
        if self._missing:
            return False
        samples, sub_samples, q, sub_q, pv = got
        single = {n: self.holds_one_device(n) for n in self.submeters}
        self.samples_read += sum(len(rows) for rows in samples.values())
        until = self.fleet.ingest(samples, sub_samples, q, sub_q, end.timestamp(), pv, dict(self.q_quantum), single,
                                  switches or None, numbers or None, inputs or None,
                                  {n: dict(v) for n, v in self.sub_q_quantum.items()}, self.volts or None)
        self.fleet.advance(until)
        # every step, live too (streaming spec step 4): each part of it runs only when its input changed - the
        # successors when a filing moved the library, the edges' trim and the pairs' ageing once an hour
        self.fleet.settle(until)
        # live power by energy since each load's last write, on the data's clock; a gap slice writes them all.
        # Accrued and checked every LIVE_WRITE_MIN_S of data, as often as a write can come anyway: an open run's
        # energy is its level times the time, and a filed run brings all of its own, either way - every step it
        # walked the named loads for 2.4 s of CPU per 2 min at Home (2026-11-12)
        t = end.timestamp()
        last = self.__dict__.get("_live_checked")
        if settle or last is None or t - last >= LIVE_WRITE_MIN_S or t < last:
            self._livepower.accrue(self.fleet, t)
            self._live_due = self._livepower.due(self.fleet, t, force=settle)
            self._live_checked = t
        self._note_latency()
        return True

    def _assemble(self, start: datetime, end: datetime):
        """L0: the window's readings as the detector takes them - the house per phase (grid + inverters), the
        arrays' power at its readings, the reactive power, each meter's own readings - from _pass_rows, the same
        for a live window and a gap. Returns (samples, sub_samples, q, sub_q, pv); _missing says what it lacked."""
        samples = self._derive_load(start, end)
        # the arrays, for telling a cloud from a load switching
        self.solar = self._resolve_solar()
        generation = _sum_phases([self._read(start, end, fields)[0] for fields in self.solar])
        # after the sum, not before: where the inverter is added back the
        # grid meter is the circuit every household watt flows through,
        # so its VAr is the one that steps when a load switches
        q = self._reactive_series(start, end, samples)
        pv: Dict[str, Dict[float, float]] = {}
        for p, target in samples.items():
            if generation.get(p):
                pv[p] = _align(generation[p], target)
        for p, rows in samples.items():
            if p in self.fleet.main.phases:
                # a reading that never exports is the house alone, and
                # the house cannot draw less than nothing. Kept, and
                # saved, where a pass is too short to say: "can't tell"
                # read as "no" switched the guard off on every live pass.
                # Not a meter's, though it is as house-side: what the
                # guard brings - runs the reading cannot carry end
                # unseen - assumes a reading many loads share, and Home's
                # office plug, its computer's draw sagging under its
                # runs, filed 364 sessions of its own in ten days for
                # 827 (the unify audit's R6, 2026-10-03)
                verdict = carries_generation(rows)
                if verdict is not None:
                    self.fleet.main.phases[p].floor_zero = verdict is False
        for p in list(pv):
            verdict = carries_generation(samples[p])
            if verdict is not None:
                self.pv_visible[p] = verdict
            if self.pv_visible.get(p) is False:
                pv.pop(p)         # this reading never sees the sun; leave its steps alone
        for p, arr in self.array_in_house.items():
            if p in samples:
                pv[p] = arr       # ...but a house built with the inverter added has it in
        sub_samples, sub_q = {}, {}
        for name, meter in self.submeters.items():
            ss, sq = self._read(start, end, meter["fields"], name)
            if ss:
                sub_samples[name], sub_q[name] = ss, sq
        # the recorder's start-of-window row is a copy, not a reading - see
        # without_window_start; the sums above needed it, the detector must not
        samples = without_window_start(samples, start.timestamp())
        sub_samples = {n: without_window_start(v, start.timestamp()) for n, v in sub_samples.items()}
        return samples, sub_samples, q, sub_q, pv

    def _note_latency(self) -> None:
        """Step latency: when each step was declared (now, at the end of the job that declared it) less when it
        happened, over the last 2,000 steps of every detector; median, 90th percentile and most in step_latency."""
        if not self._live:
            return
        wall, grew = time.time(), False
        for meter, det in self.fleet.detectors():
            for ph, st in det.phases.items():
                seen = self._lag_seen.get((meter, ph))
                # from the newest back to the last one seen: only what is new, not the whole list (up to 4,000)
                # every second - that scan was 0.6 s of CPU per 2 min at Home (2026-11-12)
                new = []
                for d in reversed(st.declared):
                    if seen is not None and d[0] <= seen:
                        break
                    new.append(d[0])
                    if seen is None:
                        break                        # the first job only marks where it starts
                if new:
                    self._lag_seen[(meter, ph)] = new[0]
                    if seen is not None:
                        self._lags.extend(wall - t for t in new)
                        grew = True
        if not grew:
            return
        del self._lags[:-2000]
        if self._lags:
            lags = sorted(self._lags)
            self.step_latency = {"steps": len(lags), "p50_s": round(lags[len(lags) // 2], 1),
                                 "p90_s": round(lags[int(len(lags) * 0.9)], 1), "max_s": round(lags[-1], 1)}

    @callback
    def _listen(self) -> None:
        """Listen to every entity the assembly reads, and every input: their state changes go into the stream
        (_on_state). Called again whenever the set grows; an entity first listened to now is seeded with its
        current state, its value before the first window. Live once the recorder has filled everything before
        listening began (_step)."""
        inputs = set(self.entry.options.get(CONF_INPUT_ENTITIES) or [])
        want = set(self._wanted) | inputs
        if want == self._listening and self._unlisten is not None:
            return
        if self._unlisten is not None:
            self._unlisten()
        for eid in want - self._listening:
            st = self.hass.states.get(eid)
            if st is not None and eid not in inputs:
                self._stream.seed(eid, st.last_updated_timestamp, st.state)
        self._listening = want
        self._unlisten = async_track_state_change_event(self.hass, sorted(want), self._on_state)
        if self._listen_from is None:
            self._listen_from = time.time()
            self._stream.ran_to = self.last_processed.timestamp() if self.last_processed else None

    @callback
    def _on_state(self, event) -> None:
        st = event.data.get("new_state")
        if st is None:
            return
        eid = st.entity_id
        if eid in self._input_ids:
            self._input_buf.setdefault(eid, []).append(st)
            self.__dict__.setdefault("_input_dirty", set()).add(eid)      # read at the next step (_read_inputs)
        if eid in self._wanted:
            self._stream.add(eid, st.last_updated_timestamp, st.state)

    @property
    def _input_ids(self) -> set:
        return set(self.entry.options.get(CONF_INPUT_ENTITIES) or [])

    @callback
    def _tick(self, _now) -> None:
        if self._stopping or self.entry.pref_disable_polling or self._lock.locked():
            return
        self._live = self._listen_from is not None and self.last_processed is not None \
            and self.last_processed.timestamp() >= self._listen_from
        # the step itself, not the coordinator's refresh around it: its timing, logging and debouncing every
        # second for an update that never has data to hand (always_update=False; _run tells the entities itself)
        self.hass.async_create_task(self._run())

    async def _minute(self) -> None:
        """Once a minute, the cold path of the live stream (streaming spec, step 4): the configuration, the save
        (_persist, throttled as before), the entities but the named loads' power (written by each step, _write_live;
        the library and detected-loads sensors' large attributes at most every LIBRARY_REFRESH_S, sensor.py), the
        Energy dashboard list when what it lists changed (_dashboard_key; the hourly check writes it too), and
        once a day the stream's audit. The detector settles in every step since step 4 (_hot)."""
        if not self._live or self._lock.locked():
            return
        async with self._lock:
            try:
                end = self.last_processed
                if end is None:
                    return
                await self._refresh_config()
                began = time.monotonic()
                await self._persist()
                saved = time.monotonic()
                # the entities hear once a minute what the steps since the last time did: the hours a base load's
                # energy may gain over (BaseLoadEnergy) are those since then, not the last one-second step's
                prev = self._settled_to if self._settled_to is not None else end.timestamp() - 60.0
                self.last_pass = {**self.last_pass, "hours": round(max(0.0, end.timestamp() - prev) / 3600.0, 4)}
                self._settled_to = end.timestamp()
                self.async_update_listeners()
                key = self._dashboard_key()
                if key != self._dashboard_was:
                    try:
                        await self.autonamer.async_dashboard()      # a new name is on the Energy dashboard now
                        self._dashboard_was = key
                    except Exception:  # noqa: BLE001 - the dashboard list must not stop detection
                        _LOGGER.exception("Energy dashboard list failed")
                self.last_minute = {"persist_s": round(saved - began, 2), "total_s": round(time.monotonic() - began, 2)}
                await self._audit_stream()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Load detection's minute work failed")

    def _dashboard_key(self) -> tuple:
        """What the Energy dashboard list is made of (AutoNamer.async_dashboard): every name with its energy and
        power entities - one made after the name changes it again - the base loads, and whether detection has
        caught up (the list waits for that)."""
        names = self.fleet.entity_names()
        return (tuple((n, self.energy_entity(n), self.power_entity(n)) for n in names),
                tuple(sorted(self.base_load_keys(), key=str)), self.caught_up, self.refiling)

    async def _audit_stream(self) -> None:
        """Once a day (the first minute after 03:00 local), one hour of every entity the stream reads compared
        with the recorder's rows for it (the owner, 2026-11-11): changes the recorder has and the stream never
        handed on (missing), ones it handed on that the recorder does not have (extra), and the late ones it
        dropped - counts in stream_audit, nothing corrected."""
        local = dt_util.now()
        day = local.date().isoformat()
        if local.hour < 3 or self._audited_day == day:
            return
        end = dt_util.utcnow() - timedelta(minutes=5)
        start = end - timedelta(hours=1)
        if self._listen_from is None or start.timestamp() < self._listen_from:
            return          # the hour must be one the stream listened to all of (the first, at Home, compared the
                            # hour before the restart: 27,481 of 27,481 "missing", 2026-11-11)
        self._audited_day = day
        rec = await self._recorder_rows(start, end, self._wanted)
        a, b = start.timestamp(), end.timestamp()
        missing = extra = compared = 0
        worst: Dict[str, int] = {}
        for eid, rows in rec.items():
            theirs = {round(float(r[COMPRESSED_STATE_LAST_UPDATED]), 3) for r in rows
                      if a < float(r[COMPRESSED_STATE_LAST_UPDATED]) < b}
            ours = {round(t, 3) for t in self._stream.fed.get(eid, []) if a < t < b}
            compared += len(theirs)
            m, x = len(theirs - ours), len(ours - theirs)
            missing, extra = missing + m, extra + x
            if m or x:
                worst[eid] = m + x
        self.stream_audit = {"at": end.isoformat(), "entities": len(rec), "compared": compared, "missing": missing,
                             "extra": extra, "late_dropped": dict(self._stream.late),
                             "worst": dict(sorted(worst.items(), key=lambda kv: -kv[1])[:5])}

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
        volts and amps - and it cost 78 phantom pairs in a single day.

        A power-factor entity needs no current, so it counts as coherent on
        its own as long as it sits with the power."""
        # resolved once per configuration (_refresh_config forgets it): registry and device lookups every step
        # were 3.5 s of CPU per 2 min at Home (2026-11-12, 5,100 device_rows calls)
        memo = self.__dict__.setdefault("_resolve_memo", {})
        key = ("coherent", _triple_key(cfg, phase), phase)
        if key not in memo:
            memo[key] = self._coherent_triple_resolved(cfg, phase)
        return memo[key]

    def _coherent_triple_resolved(self, cfg: dict, phase: str) -> Optional[dict]:
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
        return beside_of(device_rows(self.hass, device), reference, "var", phase)

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
        memo = self.__dict__.setdefault("_resolve_memo", {})
        key = ("beside", _triple_key(cfg, phase), phase)
        if key not in memo:
            memo[key] = self._triple_beside_the_amps_resolved(cfg, phase)
        return memo[key]

    def _triple_beside_the_amps_resolved(self, cfg: dict, phase: str) -> Optional[dict]:
        volts, amps = cfg.get(f"voltage_{phase}"), cfg.get(f"current_{phase}")
        if not (volts and amps):
            return None
        device = self._device_of(amps)
        if device is None or device != self._device_of(volts):
            return None
        power = beside_of(device_rows(self.hass, device), amps, "power", phase)
        if not power:
            return None
        return {f"power_{phase}": power, f"voltage_{phase}": volts,
                f"current_{phase}": amps, f"pf_{phase}": None, f"var_{phase}": self._var_on(device, phase, power)}

    def _reactive_series(self, start: datetime, end: datetime,
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
        self.volts = {}
        for phase, rows in targets.items():
            if not rows:
                continue
            for prefix in ("", GRID_PREFIX):
                cfg = {k[len(prefix):]: v for k, v in self.config.items()
                       if k.startswith(prefix)} if prefix else dict(self.config)
                triple = self._coherent_triple(cfg, phase) or self._triple_beside_the_amps(cfg, phase)
                if triple is None:
                    continue
                series = self._read_raw(start, end, triple)
                power_rows = series.get(("power", phase)) or []
                if not carries_load(power_rows):
                    continue          # coherent, but nothing flows through it
                if series.get(("voltage", phase)):
                    self.volts.setdefault("", {})[phase] = series[("voltage", phase)]   # the edges' voltage - see Fleet.volts
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
                    got = self._learn_q_quantum(phase, series, phase)
                    if got:
                        self.q_quantum[phase] = got
                break
        return out

    def _read_raw(self, start: datetime, end: datetime, cfg: dict) -> Dict[tuple, list]:
        """Every configured field of one role as (kind, phase) -> rows, from the window's rows (_window_rows: the
        stream's buffer live, the recorder for a gap). An entity the window has no rows for at all is asked for
        (``_missing``), and the window is assembled again once it has them (_step); every entity asked for is
        listened to from then on (``_wanted``)."""
        entities = {}
        for p in PHASES:
            for kind in ("power", "pf", "va", "current", "voltage", "var"):
                eid = cfg.get(f"{kind}_{p}")
                if eid:
                    entities[(kind, p)] = eid
        if not entities:
            return {}
        ids = list(dict.fromkeys(entities.values()))
        self._wanted.update(ids)
        pre = self._pass_rows
        states = pre[2] if pre is not None and pre[0] == start and pre[1] == end else {}
        self._missing.update(e for e in ids if e not in states)
        series: Dict[tuple, list] = {}
        for key, eid in entities.items():
            # History is fetched with no_attributes, so the unit comes from the
            # live state: a meter publishing kW is otherwise read as watts and
            # its 2 kW session becomes the number 2.
            scale = unit_scale(self._unit_of(eid))
            rows = []
            for st in states.get(eid, []):
                try:
                    rows.append((float(st[COMPRESSED_STATE_LAST_UPDATED]), float(st[COMPRESSED_STATE_STATE]) * scale))
                except (TypeError, ValueError, KeyError):
                    continue
            rows.sort()
            series[key] = rows
        return series

    async def _recorder_rows(self, start: datetime, end: datetime, ids) -> Dict[str, list]:
        """The recorder's rows of ``ids`` over the window, in ONE query (_fetch), every id present - one with
        no rows at all as an empty list, so it is not asked for again."""
        ids = sorted(set(ids))
        if not ids:
            return {}
        got = await get_instance(self.hass).async_add_executor_job(_fetch, self.hass, start, end, ids)
        return {e: list(got.get(e, [])) for e in ids}

    def _unit_of(self, entity_id: str) -> Optional[str]:
        state = self.hass.states.get(entity_id)
        if state is not None:
            unit = state.attributes.get("unit_of_measurement")
            if unit:
                return unit
        entry = er.async_get(self.hass).async_get(entity_id)
        return getattr(entry, "unit_of_measurement", None) if entry else None

    @callback
    def listen_live(self, name: str, write: Callable[[], None]) -> Callable[[], None]:
        """A named load's power entity is written when its live power is (_write_live), not with the others."""
        self._live_writers[name] = write
        return lambda: self._live_writers.pop(name, None)

    @callback
    def _write_live(self) -> None:
        """The live powers a step found due (LivePower.due) into live_power and their entities."""
        due, self._live_due = self._live_due, {}
        for name, watts in due.items():
            self.live_power[name] = watts
            write = self._live_writers.get(name)
            if write is not None:
                write()

    def _learn_q_quantum(self, key, series: Dict[tuple, list], phase: str) -> Optional[float]:
        """The apparent power one quantum of the amps behind a channel's
        reactive power is worth - V x dI - the error bar on every power factor
        derived from them; the same for the grid's channels (``key`` its
        phase) and each meter's ((meter, channel)). A power-factor entity
        needs no amps and carries its own precision, so it is left ungated.
        ACCUMULATED across passes, never re-measured from one. A pass reads a
        single minute of history, which holds nowhere near enough changes to
        confirm a lattice - so measuring per pass returned nothing and,
        because it cleared first, threw away what the six-hour backfill
        slices HAD learned. Resolution is a property of the instrument; it
        does not expire between passes."""
        amps = series.get(("current", phase)) or []
        volts = series.get(("voltage", phase)) or []
        if not (amps and volts):
            return None
        steps = self._amp_steps.setdefault(key, [])
        new = [abs(b - a) for (_, a), (_, b) in zip(amps, amps[1:]) if b != a]
        memo = self.__dict__.setdefault("_dq_memo", {})
        if new or key not in memo:
            # worked out again only when the amps stepped: a one-second window rarely holds a step, and every
            # channel's quantum from its whole memory every step was 0.8 s of CPU per 2 min at Home (2026-11-12)
            steps.extend(new)
            del steps[:-AMP_STEP_MEMORY]
            memo[key] = quantum_of_steps(steps)
        dq = memo[key]
        return dq * _median([v for _, v in volts]) if dq else None

    def _read(self, start: datetime, end: datetime, cfg: dict, meter: Optional[str] = None):
        """(watts per phase, reactive VAr per phase) over the window - and,
        for a ``meter`` below the grid, what its amps resolve, as the grid's
        (_learn_q_quantum).

        The VAr here is only ever derived from readings that sit on one
        device; a role whose watts and amps come from different meters gets
        none, and ``_reactive_series`` finds it a proper source instead."""
        series = self._read_raw(start, end, cfg)
        samples = {p: series[("power", p)] for p in PHASES if series.get(("power", p))}
        if not samples:
            return {}, {}
        if meter is not None:
            own = {p: series[("voltage", p)] for p in samples if series.get(("voltage", p))}
            if own:
                self.volts[meter] = own        # the meter's own voltage - see Fleet.volts
        q: Dict[str, Dict[float, float]] = {}
        for p in samples:
            if self._coherent_triple(cfg, p) is None:
                continue          # different meters; not this power's VAr
            var = _reactive(samples[p], series.get(("voltage", p)),
                            series.get(("current", p)), series.get(("pf", p)),
                            series.get(("var", p)), series.get(("va", p)))
            if var:
                q[p] = var
            got = self._learn_q_quantum((meter, p), series, p) if meter is not None else None
            if got:
                self.sub_q_quantum.setdefault(meter, {})[p] = got
        return samples, q


def _fetch(hass, start, end, entity_ids):
    """Every entity's state changes over the window, in ONE query.

    This looped over state_changes_during_period, which takes a single
    entity_id, so a pass cost one SQL round trip per entity - about twenty at
    The owner's home once the submeters are counted, and that is per pass rather
    than per sample, so it is exactly the cost that multiplies if detection
    runs more often (2026-09-18). get_significant_states takes the whole
    list; significant_changes_only=False is what keeps it every change rather
    than the dashboard's thinned-out view, and minimal_response=False keeps
    every row. Rows come as the recorder's compressed dicts - the state and a
    float timestamp - not State objects: a six-hour backfill slice is ~100,000
    grid readings at Home, and as State objects (datetimes, a context each)
    they were Core's startup burst, ~300 MiB kept by Python after it (Home on
    the CM4, 2026-10-30).
    """
    if not entity_ids:
        return {}
    return history.get_significant_states(
        hass, start, end, list(entity_ids),
        include_start_time_state=True,
        significant_changes_only=False,
        minimal_response=False,
        no_attributes=True,
        compressed_state_format=True,
    )


def _triple_key(cfg: dict, phase: str) -> tuple:
    """What a phase's triple is resolved from: its own fields' entity ids. Not the whole configuration - it holds
    lists (Home's detection options), and a key of it could not be hashed: every step failed, 2026-11-12."""
    return tuple(cfg.get(f"{kind}_{phase}") for kind in ("power", "pf", "va", "voltage", "current", "var"))


class _Stream:
    """The configured sensors' state changes as they come (streaming pipeline, step 2), kept until their window
    has run. A window reads as the recorder answers _fetch - its rows in time order, and the newest before start
    stamped at start (the start-of-window row the sums need and without_window_start drops) - so a window from
    here and one from the recorder assemble the same readings; but over (start, end], not the recorder's open
    (start, end): a row stamped exactly at a boundary belonged to neither window there and was lost. A change
    stamped at or before the end of the last window run is too late for it: dropped and counted (``late``)."""

    def __init__(self) -> None:
        self.rows: Dict[str, list] = {}         # entity -> [(ts, state)], in time order, after the last window
        self.last: Dict[str, tuple] = {}        # entity -> (ts, state) as of the last window's end
        self.ran_to: Optional[float] = None     # the end of the last window run
        self.late: Dict[str, int] = {}          # entity -> changes dropped for coming in behind their window
        self.fed: Dict[str, list] = {}          # entity -> the stamps of the rows windows handed on (the audit's)

    def seed(self, eid: str, ts: float, state: str) -> None:
        """What an entity reads as listening to it starts: its value before the first window."""
        if eid not in self.last and not self.rows.get(eid):
            self.last[eid] = (ts, state)

    def add(self, eid: str, ts: float, state: str) -> None:
        if self.ran_to is not None and ts <= self.ran_to:
            self.late[eid] = self.late.get(eid, 0) + 1
            return
        rows = self.rows.setdefault(eid, [])
        if rows and ts < rows[-1][0]:
            bisect.insort(rows, (ts, state))      # a reading stamped behind one already here: put in order
        else:
            rows.append((ts, state))

    def quiet_end(self, start: float, end: float, ids, settle_s: float) -> float:
        """``end``, or just before it where it would cut an update in two: readings less than ``settle_s`` apart
        are one update (a meter's phases and its inverter in one poll), and the house sum keeps only its last
        (combine's settle_s) - which it can only do with the whole update in one window. A window ending inside
        one summed its first half alone (the grid meter's reading without the inverter's, 14 ms apart). The
        update goes whole into the next window; ``start`` where no end before it is quiet."""
        stamps = sorted(t for eid in ids for t, _ in (self.rows.get(eid) or []) if end - settle_s < t <= end + settle_s)
        if not stamps:
            return end
        cut = stamps[0]
        earlier = sorted((t for eid in ids for t, _ in (self.rows.get(eid) or []) if start < t < cut), reverse=True)
        for t in earlier:
            if cut - t >= settle_s:
                break
            cut = t
        return max(start, cut - 1e-6)

    def window(self, start: float, end: float, ids, keep_fed_s: float = 7200.0) -> Dict[str, list]:
        """Every id's rows over (start, end) as compressed states - a start row first where one is known - and
        the window marked run: what is in it moves into ``last``."""
        out: Dict[str, list] = {}
        for eid in ids:
            rows = self.rows.get(eid) or []
            k = j = 0
            while k < len(rows) and rows[k][0] <= end:
                k += 1
            before = self.last.get(eid)
            while j < k and rows[j][0] <= start:
                before = rows[j]
                j += 1
            got = []
            if before is not None and before[0] <= start:
                got.append({COMPRESSED_STATE_LAST_UPDATED: start, COMPRESSED_STATE_STATE: before[1]})
            got += [{COMPRESSED_STATE_LAST_UPDATED: t, COMPRESSED_STATE_STATE: v} for t, v in rows[j:k]]
            out[eid] = got
            if k:
                self.last[eid] = rows[k - 1]
                fed = self.fed.setdefault(eid, [])
                fed.extend(t for t, _ in rows[j:k])
                del fed[:bisect.bisect_left(fed, end - keep_fed_s)]
                self.rows[eid] = rows[k:]
        self.ran_to = end if self.ran_to is None else max(self.ran_to, end)
        return out
