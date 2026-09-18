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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GRID_PREFIX,
    CONF_LAYOUT,
    LAYOUT_ALIASES,
    LAYOUT_AUTO,
    LAYOUT_PARALLEL,
    LAYOUT_SERIES,
    SOURCE_NONE,
    CONF_DETECTION,
    CONF_INVERTERS,
    DETECTION_BACKFILL_DAYS,
    CONF_DETECTION_INTERVAL,
    DETECTION_INTERVAL_MINUTES,
    DETECTION_SLICE_HOURS,
    SAVE_MAX_INTERVAL_S,
    DOMAIN,
)
from .insights.detect import (
    PHASES,
    Detector,
    Fleet,
    carries_generation,
    carries_load,
    classify_source,
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
DETECTOR_GENERATION = 4
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
        """A device's sensors in the shape the meter matcher reads."""
        rows = []
        for e in er.async_entries_for_device(registry, device_id, include_disabled_entities=False):
            if e.domain != "sensor":
                continue
            st = self.hass.states.get(e.entity_id)
            rows.append({
                "entity_id": e.entity_id,
                "device_class": e.device_class or e.original_device_class or (st.attributes.get("device_class") if st else None),
                "name": e.name or e.original_name or "",
            })
        return rows

    async def _add_the_grid(self, start: datetime, end: datetime, samples: dict,
                            generation: Optional[Dict[str, list]] = None) -> dict:
        """Complete the load signal with the grid connection, where it helps.

        A site whose inverter feeds the loads in PARALLEL with the grid has
        its house load split between the two readings - the meter carries the
        house minus what the inverter makes - so neither alone is what the
        detector wants and their sum is. Behind a transfer switch, or off
        grid, everything already comes through the inverter and adding the
        grid would count the pass-through twice.

        Which one a site has is read off the data, since it is a fact about
        the wiring rather than a preference, and the setting can override it.
        """
        cfg = {f"power_{p}": self.config.get(f"{CONF_GRID_PREFIX}{p}") for p in PHASES}
        cfg = {k: v for k, v in cfg.items() if v}
        if not cfg or not samples:
            return samples
        grid_rows, _ = await self._read(start, end, cfg)
        sign = await self._grid_sign(grid_rows, generation)
        for rows in grid_rows.values():
            seen = classify_source(rows)
            if seen is not None and (self.source_kind is None or seen != SOURCE_NONE):
                self.source_kind = seen
        mode = self.config.get(CONF_LAYOUT) or LAYOUT_AUTO
        mode = LAYOUT_ALIASES.get(mode, mode)
        out = dict(samples)
        for p, rows in samples.items():
            if not grid_rows.get(p):
                continue
            aligned = _align(grid_rows[p], rows)
            if mode == LAYOUT_AUTO:
                # the reading that goes negative is the one with generation
                # in it, and that is the one the inverter must be added back to
                verdict = carries_generation(rows)
                if verdict is not None:
                    self.layout[p] = LAYOUT_PARALLEL if verdict else LAYOUT_SERIES
                # unsure means DON'T add: a wrong sum corrupts every reading,
                # while leaving it out only keeps what we had before
                use = self.layout.get(p, LAYOUT_SERIES)
            else:
                use = mode
                self.layout[p] = mode
            if use == LAYOUT_PARALLEL:
                out[p] = [(ts, w + sign * aligned.get(ts, 0.0)) for ts, w in rows]
        return out

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
        return [s for s in sorted(self.detector.signatures, key=lambda x: (-x.energy_wh, -x.evidence))
                if (s.count >= MIN_COUNT_TO_NAME and s.energy_wh >= NAMING_MIN_WH
                    and most_specific(s.locations, s.count, parents) == "main")
                # a load that may be what a NAMED one became belongs on the
                # list whatever its size: the offer to move the name is the
                # whole reason to open it
                or self.detector.predecessor_of(s.id) is not None]

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
    def interval_minutes(self) -> int:
        """How often to re-read the recorder, as configured or defaulted."""
        try:
            value = int(self.config.get(CONF_DETECTION_INTERVAL) or DETECTION_INTERVAL_MINUTES)
        except (TypeError, ValueError):
            return DETECTION_INTERVAL_MINUTES
        return value if value > 0 else DETECTION_INTERVAL_MINUTES

    @property
    def enabled(self) -> bool:
        return any(self.config.get(f"power_{p}") for p in PHASES)

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    async def async_start(self) -> None:
        raw = await self._store.async_load() or {}
        if raw and raw.get("generation") != DETECTOR_GENERATION:
            _LOGGER.info(
                "Load detection was learned by an older detector; starting its library again"
            )
            raw = {}
        if raw.get("fleet"):
            self.fleet = Fleet.from_dict(raw.get("fleet"))
        else:                                   # a store written before downstream meters existed
            self.fleet = Fleet(main=Detector.from_dict(raw.get("detector")))
        self.fleet.main.tz_offset_s = dt_util.now().utcoffset().total_seconds()
        lp = raw.get("last_processed")
        self.last_processed = dt_util.parse_datetime(lp) if lp else None
        if not self.enabled:
            return
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(minutes=self.interval_minutes))
        self.hass.async_create_task(self._run())

    async def async_reset(self) -> None:
        """Forget everything learned and start the backfill again."""
        self.fleet = Fleet()
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
            samples, _ = await self._read(start, end, self.config)
            # the arrays first: their raw series is what says which way round
            # the grid meter is wired, and that decides whether completing the
            # load signal is an addition or a subtraction
            self.solar = await self._resolve_solar()
            generation: Dict[str, list] = {}
            for fields in self.solar:
                rows, _ = await self._read(start, end, fields)
                for p, series in rows.items():
                    generation.setdefault(p, [])
                    generation[p] = _sum_series(generation[p], series)
            samples = await self._add_the_grid(start, end, samples, generation)
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
            rows = []
            for st in states.get(eid, []):
                try:
                    rows.append((st.last_updated.timestamp(), float(st.state)))
                except (TypeError, ValueError):
                    continue
            rows.sort()
            series[key] = rows
        return series

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


def _sum_series(a: list, b: list) -> list:
    """Two arrays' power added together, each held forward onto the other's
    sample times - one site has two trackers and reading only the first
    would leave half of every cloud unexplained."""
    if not a:
        return list(b)
    if not b:
        return list(a)
    stamps = sorted({ts for ts, _ in a} | {ts for ts, _ in b})
    ia = ib = 0
    out = []
    for ts in stamps:
        ia, ib = _as_of(a, ts, ia), _as_of(b, ts, ib)
        out.append((ts, (a[ia][1] if ia >= 0 else 0.0) + (b[ib][1] if ib >= 0 else 0.0)))
    return out


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
