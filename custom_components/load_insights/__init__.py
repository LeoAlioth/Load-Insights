"""Load Insights - consumption forecasts from what the Energy dashboard knows."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN
from .coordinator import InsightsCoordinator
from .detection import DetectionRunner

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


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
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    hass.data[DOMAIN][f"{entry.entry_id}_detection"] = detection
    hass.data[DOMAIN][f"{entry.entry_id}_site_device"] = site_device.id
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_changed))
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
