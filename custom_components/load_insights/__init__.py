"""Load Insights - consumption forecasts from what the Energy dashboard knows."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_NAME,
    DEFAULT_NAME,
    DOMAIN,
    SERVICE_REFRESH,
    SERVICE_RESET_DETECTION,
)
from .coordinator import InsightsCoordinator
from .detection import DetectionRunner

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def _async_for_each_entry(hass: HomeAssistant, fn) -> None:
    """Run ``fn`` for every Load Insights entry - these services act on the
    integration rather than on one entity, and a site has exactly one."""
    for entry_id, obj in list(hass.data.get(DOMAIN, {}).items()):
        if isinstance(obj, InsightsCoordinator):
            await fn(entry_id, obj)


async def async_setup(hass: HomeAssistant, config) -> bool:
    """Register the services once, whatever entries come and go."""

    async def _refresh(call) -> None:
        async def go(entry_id, coordinator):
            await coordinator.async_request_refresh()
        await _async_for_each_entry(hass, go)

    async def _reset_detection(call) -> None:
        async def go(entry_id, _coordinator):
            runner: DetectionRunner | None = hass.data[DOMAIN].get(f"{entry_id}_detection")
            if runner is not None:
                await runner.async_reset()
        await _async_for_each_entry(hass, go)

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _refresh)
    hass.services.async_register(DOMAIN, SERVICE_RESET_DETECTION, _reset_detection)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = InsightsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    detection = DetectionRunner(hass, entry)
    await detection.async_start()
    # The site device exists before any entity, so the per-device and
    # per-load devices can point at it with via_device_id - the registry's own
    # id. The old tuple form (`via_device`) is deprecated and breaks in
    # 2027.8, and this project is new enough not to inherit that.
    site_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_NAME, DEFAULT_NAME),
        manufacturer="Load Insights",
        model="Site",
        # wherever the grid meter lives, rather than floating unassigned
        suggested_area=_area_of_the_meter(hass, coordinator),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    hass.data[DOMAIN][f"{entry.entry_id}_detection"] = detection
    hass.data[DOMAIN][f"{entry.entry_id}_site_device"] = site_device.id
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_changed))
    _prune_empty_devices(hass, entry)
    return True


@callback
def _area_of_the_meter(hass: HomeAssistant, coordinator: InsightsCoordinator) -> str | None:
    """The area the grid import meter sits in, if it has one - the closest
    thing this integration has to a physical location."""
    from homeassistant.helpers import area_registry as ar

    data = coordinator.data
    if data is None or not data.site.grid_import:
        return None
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    for stat_id in data.site.grid_import:
        reg = entities.async_get(stat_id)
        if reg is None:
            continue
        area_id = reg.area_id
        if not area_id and reg.device_id:
            device = devices.async_get(reg.device_id)
            area_id = device.area_id if device else None
        if area_id:
            area = areas.async_get_area(area_id)
            if area:
                return area.name
    return None


@callback
def _prune_empty_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop our devices that no longer hold an entity.

    A device a metered thing used to have - one removed from the Energy
    dashboard, or one left behind when the layout changed - lingers in the
    registry with nothing in it, and Home Assistant does not clear it. This
    runs after the platforms, so every entity that should exist does.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if er.async_entries_for_device(entities, device.id, include_disabled_entities=True):
            continue
        # only drops the device when this entry was the last one on it
        devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device) -> bool:
    """Let a device be deleted from the UI - the answer is always yes.

    Its entities come back on the next reload if the thing still exists, so
    deleting one is a way to tidy, never a way to lose anything.
    """
    return True


async def _async_options_changed(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A changed input is a different model: reload rather than patch."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: InsightsCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        hass.data[DOMAIN].pop(f"{entry.entry_id}_site_device", None)
        detection: DetectionRunner = hass.data[DOMAIN].pop(f"{entry.entry_id}_detection", None)
        if detection:
            await detection.async_stop()
    return ok
