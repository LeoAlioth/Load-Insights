"""Runs the detector on the recorder's raw states, incrementally."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from homeassistant.components.recorder import get_instance, history
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
from .insights.detect import PHASES, Detector

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
        self.detector: Detector = Detector()
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

    @property
    def enabled(self) -> bool:
        return any(self.config.get(f"power_{p}") for p in PHASES)

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    async def async_start(self) -> None:
        raw = await self._store.async_load() or {}
        self.detector = Detector.from_dict(raw.get("detector"))
        self.detector.tz_offset_s = dt_util.now().utcoffset().total_seconds()
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
            samples, pf = await self._read(start, end)
            await self.hass.async_add_executor_job(self.detector.process, samples, pf, end.timestamp())
            self.last_processed = end
            self.caught_up = end >= now - timedelta(minutes=1)
            self.last_run = now
            await self._store.async_save({"detector": self.detector.to_dict(), "last_processed": end.isoformat()})
            for cb in self._listeners:
                cb()
            if not self.caught_up:
                # keep slicing without waiting for the next tick
                async_call_later(self.hass, 2, self._tick)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Load detection run failed")
        finally:
            self._running = False

    async def _read(self, start: datetime, end: datetime):
        cfg = self.config
        entities = {}
        for p in PHASES:
            for kind in ("power", "pf"):
                eid = cfg.get(f"{kind}_{p}")
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
