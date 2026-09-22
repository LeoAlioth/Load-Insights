"""Runs the detector on the recorder's raw states, incrementally."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance, history
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GRID_PREFIX,
    CONF_LAYOUT,
    LAYOUT_ALIASES,
    ROLE_PREFIX,
    SOURCE_NONE,
    CONF_DETECTION,
    CONF_INVERTERS,
    CONF_INV_INPUT_PREFIX,
    CONF_MIN_EVIDENCE,
    CONF_MIN_STEP_W,
    DEFAULT_MIN_EVIDENCE,
    NAMING_MIN_ROWS,
    DETECTION_BACKFILL_DAYS,
    CONF_DETECTION_INTERVAL,
    DETECTION_INTERVAL_MINUTES,
    DETECTION_SLICE_HOURS,
    SAVE_MAX_INTERVAL_S,
    DOMAIN,
)
from .insights.detect import (
    combine,
    PHASES,
    Detector,
    Fleet,
    carries_generation,
    MIN_NOISE_W,
    _sum_series,
    names_in_store,
    carries_load,
    classify_source,
    site_topology,
    unit_scale,
    exports_positive,
    mean_power,
    most_specific,
)
from .insights.discovery import closest_by_name, match_meter_entities
from .insights.model import SiteModel

_LOGGER = logging.getLogger(__name__)
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
DETECTOR_GENERATION = 5
MIN_COUNT_TO_NAME = 2          # a load seen once is not offered for naming
# What a load has actually USED is the reason to bother naming it: a
# signature worth 30 Wh over ten days is noise with a shape, and a list full
# of those is why the naming page ran to a hundred and eighty rows.
NAMING_MIN_WH = 50.0


class DetectionRunner:
    """Every interval_minutes, read what the meter has recorded since
    the last processed instant and feed it to the detector. The first run
    backfills the recorder's window in slices, one per call, so no single
    query is large; the detector's state, sessions and signatures persist in
    .storage so a restart resumes where it stopped."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.submeters = {}
        self.fleet: Fleet = Fleet()
        self.solar: List[Dict[str, str]] = []   # each array's power per phase
        # whether the configured reading actually includes the array, read
        # off the data per phase and remembered once it is conclusive
        self.pv_visible: Dict[str, bool] = {}
        # how many meter readings the runs have actually had to work with,
        # so "no loads found" can be told from "no data"
        self.samples_read: int = 0
        # how the grid reading relates to the load one, per phase, worked out
        # from the data unless the user says otherwise
        self.layout: Dict[str, str] = {}
        # What the AC input turned out to be, kept at the most
        # informative verdict seen: a generator that has not run this
        # window reads exactly like nothing connected, and falling back
        # to that every quiet day would flap the wording for no reason.
        self.source_kind: Optional[str] = None
        # Mean watts per NAMED load over the last stretch of data processed,
        # from the energy that stretch added. See _update_average_power.
        self.average_power: Dict[str, float] = {}
        self._energy_mark: Dict[str, float] = {}
        self._mark_ts: Optional[float] = None
        self._saved_at: Optional[float] = None
        self.last_processed: Optional[datetime] = None
        self.caught_up = False
        self.last_run: Optional[datetime] = None
        self.sessions_today = 0
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.detection")
        self._unsub = None
        self._listeners: List = []
        self._running = False

    @property
    def config(self) -> dict:
        return dict(self.entry.options.get(CONF_DETECTION) or {})

    # Meters below the main one come from the Energy dashboard, resolved once
    # per run: {name: {"fields": {...}, "agnostic": bool, "parent": name|None}}
    submeters: Dict[str, dict] = {}

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
        site = SiteModel.from_prefs(manager.data)
        registry = er.async_get(self.hass)
        by_stat = {d.energy: d for d in site.devices}
        out: Dict[str, dict] = {}
        for dev in site.devices:
            entry = registry.async_get(dev.energy)          # a recorder statistic id IS the entity id
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
                              "parent": parent.label if parent else None}
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
                out[p] = combine(per_phase)
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
                    return -1.0 if verdict else 1.0
        return 1.0

    async def _site_grid_power(self) -> list:
        try:
            manager = await async_get_manager(self.hass)
            return list(SiteModel.from_prefs(manager.data).grid_power)
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
        site = SiteModel.from_prefs(manager.data)
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

    def unlocated(self) -> list:
        """Signatures no device meter accounts for AND seen more than once -
        the ones worth naming.

        A load seen a single time may not be a load at all, and naming it
        teaches the library nothing (Anze, 2026-09-17: "as for signatures only
        seen once, dont show them"). It keeps its place in the library and
        appears here as soon as it happens again."""
        parents = self.parents
        # biggest first, by energy: what a load COSTS is the reason to name
        # it, and it puts the ones worth the trouble at the top
        # Energy alone put an anonymous 600 W something above a machine that
        # runs every Saturday at noon. Rank by what a person can actually act
        # on: what it costs, weighted by whether the row says enough to
        # recognise it (Anze, 2026-09-18).
        def rank(s):
            return -(s.energy_wh * (0.45 + 0.55 * s.recognisable))
        worth = [s for s in sorted(self.detector.signatures, key=lambda x: (rank(x), -x.evidence))
                 if (s.count >= MIN_COUNT_TO_NAME and s.energy_wh >= NAMING_MIN_WH
                     and most_specific(s.locations, s.count, parents) == "main")
                 # a load that may be what a NAMED one became belongs on the
                 # list whatever its size: the offer to move the name is the
                 # whole reason to open it
                 or self.detector.predecessor_of(s.id) is not None]
        return self._by_evidence(worth)

    def _by_evidence(self, worth: list) -> list:
        """Only the ones it is reasonably sure are real loads.

        A house makes far more shapes than it has appliances, and a list of
        two hundred is a list nobody reads. But a bar that hides everything
        is worse than one set too low, so when fewer than NAMING_MIN_ROWS
        clear it the best of the rest come along - which is the "lower it if
        we are not getting good hits" with nothing to decay."""
        bar = self.min_evidence
        clear = [s for s in worth if s.evidence >= bar or s.name
                 or self.detector.predecessor_of(s.id) is not None]
        if len(clear) >= NAMING_MIN_ROWS or len(clear) == len(worth):
            return clear
        rest = [s for s in worth if s not in clear]
        rest.sort(key=lambda s: -s.evidence)
        return clear + rest[:NAMING_MIN_ROWS - len(clear)]

    @property
    def min_step_w(self) -> float:
        try:
            return max(1.0, float(self.config.get(CONF_MIN_STEP_W, MIN_NOISE_W)))
        except (TypeError, ValueError):
            return MIN_NOISE_W

    @property
    def min_evidence(self) -> float:
        try:
            return max(0.0, min(1.0, float(self.config.get(CONF_MIN_EVIDENCE, DEFAULT_MIN_EVIDENCE))))
        except (TypeError, ValueError):
            return DEFAULT_MIN_EVIDENCE

    async def async_adopt(self, signature_id: int) -> Optional[str]:
        """Move a predecessor's name onto this signature, and persist."""
        name = self.detector.adopt(signature_id)
        if name is None:
            return None
        await self._persist(force=True)
        for cb in self._listeners:
            cb()
        return name

    async def async_rename(self, signature_id: int, name: Optional[str]) -> bool:
        """Name a signature (or clear it) and persist at once - the caller
        bumps the entry so the entities follow."""
        if not self.detector.rename(signature_id, name):
            return False
        await self._persist(force=True)          # a user action, written at once
        for cb in self._listeners:
            cb()
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
    def declared_layout(self) -> Optional[str]:
        """The wiring the user stated, from the Inverters page.

        It lived on the grid page as a site-wide dropdown, which was the
        wrong place: the topology is a fact about where an INVERTER sits, not
        about the grid - Anze said so and I agreed and then left both in
        place, one of them reading nothing (2026-09-18). The stored value is
        still honoured so a site that set it before keeps its answer."""
        stored = self.config.get(CONF_LAYOUT)
        stored = LAYOUT_ALIASES.get(stored, stored)
        return site_topology(self.entry.options.get(CONF_INVERTERS) or [], stored)

    @property
    def interval_minutes(self) -> int:
        """How often to re-read the recorder, as configured or defaulted."""
        try:
            value = int(self.config.get(CONF_DETECTION_INTERVAL) or DETECTION_INTERVAL_MINUTES)
        except (TypeError, ValueError):
            return DETECTION_INTERVAL_MINUTES
        return value if value > 0 else DETECTION_INTERVAL_MINUTES

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

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

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
        lp = raw.get("last_processed")
        self.last_processed = dt_util.parse_datetime(lp) if lp else None
        if not self.enabled:
            return
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(minutes=self.interval_minutes))
        self.hass.async_create_task(self._run())

    async def async_reset(self) -> None:
        """Forget everything learned and start the backfill again.

        Everything except the NAMES. They are the one thing in the library
        the user put there by hand, and the backfill re-learns everything
        else in minutes. Each is carried across as a description and handed
        back to the first rebuilt signature that looks like it; where the
        site really has changed - the reason to do this by hand - nothing
        matches and the name does not return."""
        orphans = self.fleet.main.name_descriptors() if self.fleet else []
        self.fleet = Fleet()
        self.fleet.main.carry_names(orphans)
        self.fleet.main.tz_offset_s = dt_util.now().utcoffset().total_seconds()
        self.last_processed = None
        self.caught_up = False
        self.samples_read = 0
        self._saved_at = None
        await self._persist(force=True)          # a reset must survive a crash
        for cb in self._listeners:
            cb()
        self.hass.async_create_task(self._run())

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _tick(self, _now) -> None:
        self.hass.async_create_task(self._run())

    async def _run(self) -> None:
        if self._running or not self.enabled:
            return
        self._running = True
        try:
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
                    self.fleet.main.phases[p].min_noise = self.min_step_w
                    # a reading that never exports is the house alone, and
                    # the house cannot draw less than nothing
                    self.fleet.main.phases[p].floor_zero = carries_generation(rows) is False
            for p in list(pv):
                verdict = carries_generation(samples[p])
                if verdict is not None:
                    self.pv_visible[p] = verdict
                if self.pv_visible.get(p) is False:
                    pv.pop(p)         # this reading never sees the sun; leave its steps alone
            self.submeters = await self._resolve_submeters()
            sub_samples, sub_q, agnostic = {}, {}, {}
            for name, meter in self.submeters.items():
                ss, sq = await self._read(start, end, meter["fields"])
                if ss:
                    sub_samples[name], sub_q[name] = ss, sq
                    agnostic[name] = meter["agnostic"]
            await self.hass.async_add_executor_job(
                self.fleet.process, samples, sub_samples, q, sub_q, end.timestamp(), agnostic, pv
            )
            self.samples_read += sum(len(rows) for rows in samples.values())
            self._update_average_power(end.timestamp())
            self.last_processed = end
            self.caught_up = end >= now - timedelta(minutes=1)
            self.last_run = now
            await self._persist()
            for cb in self._listeners:
                cb()
            if not self.caught_up:
                # keep slicing without waiting for the next tick
                async_call_later(self.hass, 2, self._tick)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Load detection run failed")
        finally:
            self._running = False

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
        pf = with_power(f"pf_{phase}")
        volts, amps = with_power(f"voltage_{phase}"), with_power(f"current_{phase}")
        if not pf and not (volts and amps):
            return None
        return {f"power_{phase}": power, f"pf_{phase}": pf,
                f"voltage_{phase}": volts, f"current_{phase}": amps}

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
        registry = er.async_get(self.hass)
        candidates = [r["entity_id"] for r in self._device_rows(registry, device)
                      if (r.get("device_class") or "") == "power"
                      and match_meter_entities([r]).get(f"power_{phase}")]
        if not candidates:
            return None
        power = closest_by_name(candidates, amps)
        if not power:
            return None
        return {f"power_{phase}": power, f"voltage_{phase}": volts,
                f"current_{phase}": amps, f"pf_{phase}": None}

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
                var = _reactive(power_rows, series.get(("voltage", phase)),
                                series.get(("current", phase)), series.get(("pf", phase)))
                if var:
                    # the reference samples at its own moments; hold each
                    # value forward onto the load's
                    out[phase] = _align(sorted(var.items()), rows)
                break
        return out

    async def _read_raw(self, start: datetime, end: datetime, cfg: dict) -> Dict[tuple, list]:
        """Every configured field of one role as (kind, phase) -> rows."""
        entities = {}
        for p in PHASES:
            for kind in ("power", "pf", "current", "voltage"):
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

    async def _read(self, start: datetime, end: datetime, cfg: dict, derive_q: bool = True):
        """(watts per phase, reactive VAr per phase) over the window.

        The VAr here is only ever derived from readings that sit on one
        device; a role whose watts and amps come from different meters gets
        none, and ``_reactive_series`` finds it a proper source instead."""
        series = await self._read_raw(start, end, cfg)
        samples = {p: series[("power", p)] for p in PHASES if series.get(("power", p))}
        if not samples:
            return {}, {}
        q: Dict[str, Dict[float, float]] = {}
        if derive_q:
            for p in samples:
                if self._coherent_triple(cfg, p) is None:
                    continue          # different meters; not this power's VAr
                var = _reactive(samples[p], series.get(("voltage", p)),
                                series.get(("current", p)), series.get(("pf", p)))
                if var:
                    q[p] = var
        return samples, q


def _as_of(rows: list, ts: float, i: int) -> int:
    """Index of the last row at or before ``ts``, walking forward from ``i``;
    -1 when the series has not started yet."""
    if not rows or rows[0][0] > ts:
        return -1
    i = max(i, 0)
    while i + 1 < len(rows) and rows[i + 1][0] <= ts:
        i += 1
    return i


def _align(source: list, target_rows: list) -> Dict[float, float]:
    """``source`` read as of each of ``target_rows``' moments."""
    out: Dict[float, float] = {}
    i = 0
    for ts, _ in target_rows:
        i = _as_of(source, ts, i)
        if i >= 0:
            out[ts] = source[i][1]
    return out


def _reactive(power_rows: list, volts: Optional[list], amps: Optional[list],
              pfs: Optional[list]) -> Dict[float, float]:
    """Reactive VAr at each power sample.

    Every entity updates at its own moment, so the other readings are taken
    as of the power sample's time - sample and hold - rather than looked up
    at the same instant, which almost never matches."""
    volts, amps, pfs = volts or [], amps or [], pfs or []
    out: Dict[float, float] = {}
    vi = ai = fi = 0
    for ts, p in power_rows:
        vi, ai, fi = _as_of(volts, ts, vi), _as_of(amps, ts, ai), _as_of(pfs, ts, fi)
        apparent = None
        if vi >= 0 and ai >= 0:
            apparent = volts[vi][1] * amps[ai][1]
        elif fi >= 0 and pfs[fi][1]:
            apparent = abs(p) / abs(pfs[fi][1])
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
