"""Runs the detector on the recorder's raw states, incrementally."""
from __future__ import annotations

import logging
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
                rows = []
                for e in er.async_entries_for_device(registry, entry.device_id, include_disabled_entities=False):
                    if e.domain != "sensor":
                        continue
                    st = self.hass.states.get(e.entity_id)
                    rows.append({
                        "entity_id": e.entity_id,
                        "device_class": e.device_class or e.original_device_class or (st.attributes.get("device_class") if st else None),
                        "name": e.name or e.original_name or "",
                    })
                fields = match_meter_entities(rows)
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

    @property
    def detector(self) -> Detector:
        return self.fleet.main

    @property
    def parents(self) -> Dict[str, Optional[str]]:
        """Meter -> the meter it sits inside, from included_in_stat."""
        return {name: m["parent"] for name, m in self.submeters.items()}

    def unlocated(self) -> list:
        """Signatures no device meter accounts for - the ones worth naming."""
        parents = self.parents
        return [s for s in sorted(self.detector.signatures, key=lambda x: -x.count)
                if most_specific(s.locations, s.count, parents) == "main"]

    async def async_rename(self, signature_id: int, name: Optional[str]) -> bool:
        """Name a signature (or clear it) and persist at once - the caller
        bumps the entry so the entities follow."""
        if not self.detector.rename(signature_id, name):
            return False
        await self._store.async_save(
            {"fleet": self.fleet.to_dict(),
             "last_processed": self.last_processed.isoformat() if self.last_processed else None}
        )
        for cb in self._listeners:
            cb()
        return True

    @property
    def enabled(self) -> bool:
        return any(self.config.get(f"power_{p}") for p in PHASES)

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    async def async_start(self) -> None:
        raw = await self._store.async_load() or {}
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
            samples, pf = await self._read(start, end, self.config)
            self.submeters = await self._resolve_submeters()
            sub_samples, sub_pf, agnostic = {}, {}, {}
            for name, meter in self.submeters.items():
                ss, sp = await self._read(start, end, meter["fields"])
                if ss:
                    sub_samples[name], sub_pf[name] = ss, sp
                    agnostic[name] = meter["agnostic"]
            await self.hass.async_add_executor_job(
                self.fleet.process, samples, sub_samples, pf, sub_pf, end.timestamp(), agnostic
            )
            self.last_processed = end
            self.caught_up = end >= now - timedelta(minutes=1)
            self.last_run = now
            await self._store.async_save({"fleet": self.fleet.to_dict(), "last_processed": end.isoformat()})
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
        entities = {}
        for p in PHASES:
            for kind in ("power", "pf"):
                eid = cfg.get(f"{kind}_{p}")      # "device" and the other kinds are not read here
                if eid:
                    entities[(kind, p)] = eid
        if not entities:
            return {}, {}
        states = await get_instance(self.hass).async_add_executor_job(
            _fetch, self.hass, start, end, list(entities.values())
        )
        samples: Dict[str, list] = {}
        pf: Dict[str, Dict[float, float]] = {}
        for (kind, p), eid in entities.items():
            rows = []
            for st in states.get(eid, []):
                try:
                    v = float(st.state)
                except (TypeError, ValueError):
                    continue
                rows.append((st.last_updated.timestamp(), v))
            if kind == "power":
                samples[p] = rows
            else:
                pf[p] = {ts: v for ts, v in rows}
        return samples, pf


def _fetch(hass, start, end, entity_ids):
    out = {}
    for eid in entity_ids:
        res = history.state_changes_during_period(hass, start, end, eid, no_attributes=True, include_start_time_state=True)
        out.update(res)
    return out
