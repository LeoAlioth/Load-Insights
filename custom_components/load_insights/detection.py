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
    CONF_DETECTION,
    DETECTION_BACKFILL_DAYS,
    DETECTION_INTERVAL_MINUTES,
    DETECTION_SLICE_HOURS,
    DOMAIN,
)
from .insights.detect import PHASES, Detector, Fleet, most_specific
from .insights.discovery import match_meter_entities
from .insights.model import SiteModel

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
# The DETECTOR's generation, separate from the store's format version: when
# the algorithm changes shape, what it learned before is not comparable with
# what it learns now, so the library is dropped and the backfill re-run.
# 2 = sessions are paired edges rather than excursions above the idle floor.
DETECTOR_GENERATION = 2
MIN_COUNT_TO_NAME = 2          # a load seen once is not offered for naming


class DetectionRunner:
    """Every DETECTION_INTERVAL_MINUTES, read what the meter has recorded since
    the last processed instant and feed it to the detector. The first run
    backfills the recorder's window in slices, one per call, so no single
    query is large; the detector's state, sessions and signatures persist in
    .storage so a restart resumes where it stopped."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.submeters = {}
        self.fleet: Fleet = Fleet()
        self.solar: Dict[str, str] = {}      # the array's power per phase, when it has one
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

    async def _resolve_solar(self) -> Dict[str, str]:
        """The array's power, per phase where the inverter publishes it.

        A grid meter carries the house MINUS the array, so every cloud is a
        step on it and would be filed as a load switching. An inverter
        usually gives its own per-phase power, which makes that test exact;
        failing that the dashboard's solar power sensor, or the inverter's
        total, stands in for every phase."""
        manager = await async_get_manager(self.hass)
        site = SiteModel.from_prefs(manager.data)
        if not site.solar:
            return {}
        registry = er.async_get(self.hass)
        found: Dict[str, str] = {}
        entry = registry.async_get(site.solar[0])
        if entry is not None and entry.device_id:
            matched = match_meter_entities(self._device_rows(registry, entry.device_id))
            found = {k: v for k, v in matched.items() if k.startswith("power_")}
        if len([p for p in PHASES if found.get(f"power_{p}")]) >= 2:
            return found
        total = (site.solar_power[0] if site.solar_power else None) or found.get("power_a")
        return {f"power_{p}": total for p in PHASES} if total else {}

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
        return [s for s in sorted(self.detector.signatures, key=lambda x: -x.count)
                if s.count >= MIN_COUNT_TO_NAME
                and most_specific(s.locations, s.count, parents) == "main"]

    async def async_rename(self, signature_id: int, name: Optional[str]) -> bool:
        """Name a signature (or clear it) and persist at once - the caller
        bumps the entry so the entities follow."""
        if not self.detector.rename(signature_id, name):
            return False
        await self._store.async_save(self._snapshot())
        for cb in self._listeners:
            cb()
        return True

    def _snapshot(self) -> dict:
        return {"fleet": self.fleet.to_dict(),
                "last_processed": self.last_processed.isoformat() if self.last_processed else None,
                "generation": DETECTOR_GENERATION}

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
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(minutes=DETECTION_INTERVAL_MINUTES))
        self.hass.async_create_task(self._run())

    async def async_reset(self) -> None:
        """Forget everything learned and start the backfill again."""
        self.fleet = Fleet()
        self.fleet.main.tz_offset_s = dt_util.now().utcoffset().total_seconds()
        self.last_processed = None
        self.caught_up = False
        await self._store.async_save(self._snapshot())
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
            samples, q = await self._read(start, end, self.config)
            self.solar = await self._resolve_solar()
            pv_rows, _ = await self._read(start, end, self.solar) if self.solar else ({}, {})
            pv = {p: _align(pv_rows[p], samples[p]) for p in samples if pv_rows.get(p)}
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
            self.last_processed = end
            self.caught_up = end >= now - timedelta(minutes=1)
            self.last_run = now
            await self._store.async_save(self._snapshot())
            for cb in self._listeners:
                cb()
            if not self.caught_up:
                # keep slicing without waiting for the next tick
                async_call_later(self.hass, 2, self._tick)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Load detection run failed")
        finally:
            self._running = False

    async def _read(self, start: datetime, end: datetime, cfg: dict):
        """(watts per phase, reactive VAr per phase) over the window.

        Voltage and current were configured but unread until now: with them
        the REACTIVE power follows, and a load's own power factor is the
        ratio of how far each moved when it switched - which is what tells a
        heater from a motor. A power factor entity does instead."""
        entities = {}
        for p in PHASES:
            for kind in ("power", "pf", "current", "voltage"):
                eid = cfg.get(f"{kind}_{p}")      # "device" is not read here
                if eid:
                    entities[(kind, p)] = eid
        if not any(kind == "power" for kind, _ in entities):
            return {}, {}
        states = await get_instance(self.hass).async_add_executor_job(
            _fetch, self.hass, start, end, list(dict.fromkeys(entities.values()))
        )
        series: Dict[tuple, list] = {}
        for key, eid in entities.items():
            rows = []
            for st in states.get(eid, []):
                try:
                    v = float(st.state)
                except (TypeError, ValueError):
                    continue
                rows.append((st.last_updated.timestamp(), v))
            rows.sort()
            series[key] = rows
        samples = {p: series[("power", p)] for p in PHASES if series.get(("power", p))}
        q: Dict[str, Dict[float, float]] = {}
        for p, rows in samples.items():
            var = _reactive(rows, series.get(("voltage", p)), series.get(("current", p)), series.get(("pf", p)))
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
    out = {}
    for eid in entity_ids:
        res = history.state_changes_during_period(hass, start, end, eid, no_attributes=True, include_start_time_state=True)
        out.update(res)
    return out
