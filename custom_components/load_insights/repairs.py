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

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

from .const import DOMAIN
from .detection import device_uid, runner_of
from .insights.detect import implausible_baseline
from .insights.model import dashboard_renames

HALF_TEMPERATURE = "temperature_pair_incomplete"
NO_PV_FORECAST = "no_pv_forecast"
DEVICES_WITHOUT_POWER = "devices_without_power"
BATTERY_NOT_MODELLED = "battery_not_modelled"
NO_POWER_FACTOR = "no_power_factor"
FOUND_NOTHING = "detection_found_nothing"
NEGATIVE_HOUSE = "house_reading_negative"
DASHBOARD_RENAMED = "energy_dashboard_renamed"


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

    # A battery on the dashboard whose state of charge and usable capacity
    # were never filled in - both are optional boxes there, so a battery site
    # can sit for months with its SOC forecast missing and the grid forecast
    # quietly reported before the battery.
    grid = getattr(data, "grid", None)
    lacks = []
    if site.battery_in or site.battery_out or site.battery_soc:
        if not site.battery_soc:
            lacks.append("a state of charge sensor")
        if not site.battery_capacity_kwh:
            lacks.append("a usable capacity in kWh")
        if not lacks and grid is not None and not grid.battery_modelled:
            # both are configured, so the SOC it names has no live state -
            # an external statistic rather than a sensor entity
            lacks.append("a readable state of charge (the statistic it names is not a live sensor)")
    _set(hass, entry, BATTERY_NOT_MODELLED, bool(lacks), {"missing": " and ".join(lacks)})

    # A phase with a power reading but no current AND voltage (and no power
    # factor sensor) can never tell a heater from a motor: the watts are the
    # same and only the reactive part separates them. Kozolec has this on two
    # of its three phases (Anze, 2026-09-17), and it is invisible until you
    # notice that every guess there is missing.
    runner = runner_of(entry)
    cfg = (getattr(runner, "config", None) or {}) if getattr(runner, "enabled", False) else {}
    blind = [p.upper() for p in ("a", "b", "c")
             if cfg.get(f"power_{p}") and not cfg.get(f"pf_{p}")
             and not (cfg.get(f"current_{p}") and cfg.get(f"voltage_{p}"))]
    _set(hass, entry, NO_POWER_FACTOR, bool(blind), {"phases": ", ".join(blind)})

    # A meter that never moves. Kozolec is off grid, so the MultiPlus AC
    # INPUT it was pointed at is zero by definition, and ten days of backfill
    # over a flat line taught it nothing - which looked like a broken
    # detector rather than the wrong sensor (Anze, 2026-09-17).
    nothing = bool(cfg) and getattr(runner, "caught_up", False) and not runner.detector.signatures
    _set(hass, entry, FOUND_NOTHING, nothing,
         {"samples": str(getattr(runner, "samples_read", 0))})

    # A house that idles deeply negative is not a house. Nothing breaks -
    # sessions open and close and signatures form - and every one of them is
    # nonsense, which is exactly why it needs saying (Anze's Home ran for days
    # at -6318 W on phase A, its grid meter read as the house with its sign
    # inverted and its solar never added back, 2026-09-22).
    floors = {p: st.baseline for p, st in runner.detector.phases.items()} if cfg else {}
    upside_down = implausible_baseline(floors)
    _set(hass, entry, NEGATIVE_HOUSE, bool(upside_down), {
        "phases": ", ".join(upside_down),
        "watts": ", ".join(f"{floors[p.lower()]:.0f} W" for p in upside_down),
    })

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


async def async_dashboard_renamed(hass: HomeAssistant, renames: dict) -> None:
    """Raise, or bring up to date, the one issue for renamed statistics the
    Energy dashboard still names. A waiting issue's renames are carried
    through the new batch, so a to b and then b to c asks for a to c; one
    the user has since fixed by hand is withdrawn."""
    issue = ir.async_get(hass).async_get_issue(DOMAIN, DASHBOARD_RENAMED)
    was = dict(issue.data or {}) if issue is not None and issue.active else {}
    merged = {**{o: renames.get(n, n) for o, n in was.items()}, **renames}
    named, _ = dashboard_renames((await async_get_manager(hass)).data, merged)
    if not named:
        ir.async_delete_issue(hass, DOMAIN, DASHBOARD_RENAMED)
        return
    ir.async_create_issue(
        hass, DOMAIN, DASHBOARD_RENAMED,
        is_fixable=True,
        # the renames are known only from the event, so they must survive a restart
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=DASHBOARD_RENAMED,
        translation_placeholders={"renames": "\n".join(f"- {o} → {n}" for o, n in named.items())},
        data=named,
    )


class _FollowOnTheDashboard(ConfirmRepairFlow):
    """Nothing on the dashboard changes until the user confirms."""

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None:
            await _async_follow_on_the_dashboard(self.hass, dict(self.data or {}))
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data) -> ConfirmRepairFlow:
    return _FollowOnTheDashboard()


async def _async_follow_on_the_dashboard(hass: HomeAssistant, renames: dict) -> None:
    """The dashboard's lists rewritten through the renames; each renamed
    device's forecast sensor moved onto its new statistic id - the entity and
    the device it hangs off, so its history and whatever the user set on
    either carry on - and every entry reloaded to read the new dashboard."""
    manager = await async_get_manager(hass)
    _, update = dashboard_renames(manager.data, renames)
    if update:
        await manager.async_update(update)
    entities, devices = er.async_get(hass), dr.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        for old, new in renames.items():
            eid = entities.async_get_entity_id("sensor", DOMAIN, device_uid(entry.entry_id, old))
            if eid and not entities.async_get_entity_id("sensor", DOMAIN, device_uid(entry.entry_id, new)):
                entities.async_update_entity(eid, new_unique_id=device_uid(entry.entry_id, new))
            # sensor._child_device's identifier for the device's forecast
            old_id, new_id = (DOMAIN, f"{entry.entry_id}_device_{old}"), (DOMAIN, f"{entry.entry_id}_device_{new}")
            device = devices.async_get_device(identifiers={old_id})
            if device is not None and devices.async_get_device(identifiers={new_id}) is None:
                devices.async_update_device(device.id, new_identifiers={new_id})
        hass.config_entries.async_schedule_reload(entry.entry_id)
