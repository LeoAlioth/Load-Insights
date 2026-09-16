"""Fetches the site's hourly statistics on the quarter hours and forecasts."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as rec_stats
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, HISTORY_WEEKS, HORIZON_HOURS, REFRESH_MINUTES, REFRESH_SECOND
from .insights.model import SiteModel
from .insights.profile import Forecast, forecast
from .insights.series import combine, coverage, subtract_all

_LOGGER = logging.getLogger(__name__)


@dataclass
class InsightsData:
    site: SiteModel
    consumption: Forecast
    remainder: Optional[Forecast]
    computed_at: datetime
    remainder_complete_since: Optional[datetime] = None
    devices_without_statistics: tuple = ()
    # One forecast per individually metered device, keyed by its statistic id.
    # A device with no statistics yet has no entry, and its sensor stays
    # unavailable rather than showing a forecast of nothing.
    devices: Dict[str, Forecast] = None  # type: ignore[assignment]


class InsightsCoordinator(DataUpdateCoordinator):
    """One refresh per quarter hour, aligned to the clock rather than to the
    previous refresh, so every run sees the statistics for a just-closed
    period and the forecast lands on the same grid as the tariff blocks."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self._unsub = async_track_time_change(
            hass, self._on_quarter, minute=list(REFRESH_MINUTES), second=REFRESH_SECOND
        )

    @callback
    def _on_quarter(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    async def async_shutdown(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await super().async_shutdown()

    async def _async_update_data(self) -> InsightsData:
        manager = await async_get_manager(self.hass)
        site = SiteModel.from_prefs(manager.data)
        if not site.has_sources:
            raise UpdateFailed("the Energy dashboard has no grid source configured")

        now = dt_util.now()
        start = now - timedelta(weeks=HISTORY_WEEKS)
        ids = site.all_statistic_ids()
        # Hourly "change" of each energy statistic, normalised to kWh whatever
        # unit the sensor reports in. Recorder work runs on its own executor.
        rows = await get_instance(self.hass).async_add_executor_job(
            rec_stats.statistics_during_period,
            self.hass, start, None, ids, "hour", {"energy": "kWh"}, {"change"},
        )
        series = {sid: _rows_to_samples(rows.get(sid, []), now.tzinfo) for sid in ids}

        consumption = combine(series, site.consumption_terms())
        if not consumption:
            raise UpdateFailed("no hourly consumption statistics yet")
        remainder_ids = [d.energy for d in site.remainder_devices()]
        remainder = subtract_all(consumption, series, remainder_ids) if remainder_ids else []
        complete_since, missing = coverage(series, remainder_ids) if remainder_ids else (None, [])
        labels = {d.energy: d.label for d in site.devices}

        # The fit is pure Python over a few thousand rows - still, never on
        # the event loop.
        cons_fc = await self.hass.async_add_executor_job(forecast, consumption, now, HORIZON_HOURS)
        rem_fc = (
            await self.hass.async_add_executor_job(forecast, remainder, now, HORIZON_HOURS)
            if remainder else None
        )
        # Every listed device, nested ones included - the series are already
        # in hand for the remainder, so this is only the fits.
        device_fc: Dict[str, Forecast] = {}
        for d in site.devices:
            rows = series.get(d.energy) or []
            if rows:
                device_fc[d.energy] = await self.hass.async_add_executor_job(forecast, rows, now, HORIZON_HOURS)
        return InsightsData(
            site=site, consumption=cons_fc, remainder=rem_fc, computed_at=now,
            remainder_complete_since=complete_since,
            devices_without_statistics=tuple(labels.get(m, m) for m in missing),
            devices=device_fc,
        )


def _rows_to_samples(rows: List[dict], tz) -> List[tuple]:
    """Recorder rows -> (local period start, kWh). ``start`` is an epoch float
    in current cores; a datetime is accepted too."""
    out = []
    for r in rows:
        v = r.get("change")
        if v is None:
            continue
        s = r.get("start")
        if isinstance(s, (int, float)):
            t = datetime.fromtimestamp(s, tz)
        elif isinstance(s, datetime):
            t = s.astimezone(tz)
        else:
            continue
        out.append((t, float(v)))
    return out
