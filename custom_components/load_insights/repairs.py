"""Things that are silently half-done, said out loud.

Every one of these leaves the integration working and quietly worse: a
forecast that cannot respond to the weather, a grid forecast shaped like
consumption because no PV is predicted, a device on the dashboard that
detection can never see. None is an error, so nothing would ever surface
them - which is exactly why they are worth a repair issue rather than a log
line nobody reads.

Each is raised only while it is true and withdrawn the moment it is fixed.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

HALF_TEMPERATURE = "temperature_pair_incomplete"
NO_PV_FORECAST = "no_pv_forecast"
DEVICES_WITHOUT_POWER = "devices_without_power"


@callback
def async_check(hass: HomeAssistant, entry: ConfigEntry, data) -> None:
    """Raise or withdraw every issue, from the refresh that just finished."""
    site = data.site

    # The temperature response needs BOTH: the sensor supplies the past, the
    # weather entity the future. One alone can never fit anything.
    half = bool(data.weather_entity) != bool(data.temperature_entity)
    _set(hass, entry, HALF_TEMPERATURE, half, {
        "have": "weather entity" if data.weather_entity else "temperature sensor",
        "missing": "outdoor temperature sensor" if data.weather_entity else "weather entity",
    })

    # A PV site with no forecast linked: the grid forecast then predicts a
    # week of sunless days, and its import figures are badly pessimistic.
    no_pv = bool(site.solar) and not site.solar_forecast_entries
    _set(hass, entry, NO_PV_FORECAST, no_pv, {})

    # A dashboard device whose hardware publishes no power at all cannot be
    # located by detection - hourly energy is far too coarse for a session.
    missing = list(data.devices_without_statistics or ())
    _set(hass, entry, DEVICES_WITHOUT_POWER, bool(missing),
         {"devices": ", ".join(missing[:6]) + (" and others" if len(missing) > 6 else "")})


@callback
def _set(hass: HomeAssistant, entry: ConfigEntry, key: str, active: bool, placeholders: dict) -> None:
    issue_id = f"{entry.entry_id}_{key}"
    if not active:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass, DOMAIN, issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )
