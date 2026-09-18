"""The two maintenance actions, as buttons on the site device.

Both already exist as services, which is the right shape for an automation
but the wrong shape for a person: resetting load detection meant opening
Developer Tools and finding an action by name. These sit on the site's own
device page, beside the diagnostics download (Anze, 2026-09-17).

Home Assistant gives an integration no way to add a link beside "Download
diagnostics" on the device info card, which is where Anze looked for this
twice. A button entity on the same page is as close as it allows, so both
sit in CONTROLS - the card directly beside device info - rather than in the
configuration card further down.

Reset is destructive: the signature library and the names given to it go,
and a button fires on one click with no confirmation. The same action sits
behind a confirmation on the overview page for anyone who wants that.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN
from .coordinator import InsightsCoordinator
from .detection import DetectionRunner


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: InsightsCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [RefreshButton(hass, entry, coordinator)]
    runner: DetectionRunner | None = hass.data[DOMAIN].get(f"{entry.entry_id}_detection")
    if runner is not None and runner.enabled:
        entities.append(ResetDetectionButton(hass, entry, runner))
    add(entities)


class _Base(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}_button"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_NAME, DEFAULT_NAME),
        )


class RefreshButton(_Base):
    """Recompute every forecast now instead of on the hour - for checking
    whether a change to an input took effect."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: InsightsCoordinator) -> None:
        super().__init__(entry, "refresh")
        self._coordinator = coordinator

    async def async_press(self) -> None:
        await self._coordinator.async_request_refresh()


class ResetDetectionButton(_Base):
    """Forget the signature library and start the meter's backfill again."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, runner: DetectionRunner) -> None:
        super().__init__(entry, "reset_detection")
        self._runner = runner

    async def async_press(self) -> None:
        await self._runner.async_reset()
