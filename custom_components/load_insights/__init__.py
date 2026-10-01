"""Load Insights - consumption forecasts from what the Energy dashboard knows."""
from __future__ import annotations

import logging

from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.debounce import Debouncer

from .const import (
    CONF_NAME,
    CONF_SIGNATURE_REVISION,
    DEFAULT_NAME,
    DOMAIN,
    SERVICE_BACKFILL_STATISTICS,
    SERVICE_NAME_LOAD,
    SERVICE_RESET_DETECTION,
)
from .config_flow import _area_id
from .coordinator import InsightsCoordinator
from .detection import DetectionRunner, RuntimeData
from .insights.model import SiteModel, follow_renames, migrate_inputs, relink
from .repairs import async_dashboard_renamed

_LOGGER = logging.getLogger(__name__)

# how long renamed entities are gathered before they are followed
RENAME_SETTLE_S = 5.0

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


def _loaded(hass: HomeAssistant) -> ConfigEntry | None:
    """The loaded entry these services act on - the integration rather than
    one entity, and a site has exactly one (single_config_entry)."""
    return next(iter(hass.config_entries.async_loaded_entries(DOMAIN)), None)


async def async_setup(hass: HomeAssistant, config) -> bool:
    """Register the services once, whatever entries come and go."""

    async def _reset_detection(call) -> None:
        if (entry := _loaded(hass)) is not None:
            await entry.runtime_data.runner.async_reset(forget_names=bool(call.data.get("forget_names", False)))

    async def _name_load(call) -> None:
        """Name a load by its id, for one the naming page does not offer yet -
        Kozolec's fridge, whose runs the detector measures too loosely to clear
        the page's bar (Anze, 2026-09-28). An empty name clears it."""
        load_id = int(call.data["load_id"])
        name = (call.data.get("name") or "").strip() or None
        entry = _loaded(hass)
        runner = entry.runtime_data.runner if entry is not None else None
        was = next((s.name for s in runner.detector.signatures if s.id == load_id), None) if runner else None
        if runner is None or not await runner.async_rename(load_id, name):
            raise ServiceValidationError(f"No detected load has id {load_id}")
        # what the naming page's Done does: the entities follow the names,
        # and so do the inputs linked to the load
        rev = int(entry.options.get(CONF_SIGNATURE_REVISION, 0)) + 1
        hass.config_entries.async_update_entry(
            entry, options=relink({**entry.options, CONF_SIGNATURE_REVISION: rev}, was, name))

    async def _backfill_statistics(call) -> None:
        """Write the hours detection saw of a named load over its energy
        meter's statistics - for loads named before naming did it by itself,
        and after a reset (2026-09-29). Every named load when no name is
        given; running it again writes the same."""
        wanted = (call.data.get("name") or "").strip().casefold()
        entry = _loaded(hass)
        runner = entry.runtime_data.runner if entry is not None else None
        names = [n for n in sorted(runner.detector.names()) if not wanted or n.casefold() == wanted] if runner else []
        if wanted and not names:
            raise ServiceValidationError(f"No load is named {call.data.get('name')}")
        for name in names:
            if await runner.async_backfill_statistics(name) is None:
                _LOGGER.info("Not backfilling %s yet: its energy meter has no hour of its own; "
                             "it is filled once it has", name)

    # Renamed entities: gathered for a few seconds - a rename tool changes
    # dozens at once - then followed in one options change per entry, which
    # reloads it once. Registered here rather than per entry so a rename that
    # lands while an entry reloads is not missed.
    pending: dict = {}

    async def _follow() -> None:
        renames = dict(pending)
        pending.clear()
        for entry in hass.config_entries.async_entries(DOMAIN):
            loaded: RuntimeData | None = getattr(entry, "runtime_data", None)
            if loaded is not None:
                await loaded.runner.async_follow_renames(renames)
                await loaded.coordinator.async_follow_renames(renames)
            data, options = follow_renames(dict(entry.data), renames), follow_renames(dict(entry.options), renames)
            if data != dict(entry.data) or options != dict(entry.options):
                _LOGGER.info("Following renamed entities in %s: %s", entry.title, renames)
                hass.config_entries.async_update_entry(entry, data=data, options=options)
        # The Energy dashboard names them too, and Home Assistant leaves it
        # on the old ids. It is the user's dashboard, so it is asked in
        # Repairs rather than changed here (2026-09-29).
        await async_dashboard_renamed(hass, renames)

    flush = Debouncer(hass, _LOGGER, cooldown=RENAME_SETTLE_S, immediate=False, function=_follow)

    @callback
    def _renamed(event) -> None:
        old, new = event.data.get("old_entity_id"), event.data.get("entity_id")
        if event.data.get("action") != "update" or not old or not new or old == new:
            return
        for k, v in list(pending.items()):   # renamed twice before the flush: a to b to c
            if v == old:
                pending[k] = new
        pending[old] = new
        hass.async_create_task(flush.async_call())

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _renamed)
    hass.services.async_register(DOMAIN, SERVICE_RESET_DETECTION, _reset_detection)
    hass.services.async_register(DOMAIN, SERVICE_NAME_LOAD, _name_load)
    hass.services.async_register(DOMAIN, SERVICE_BACKFILL_STATISTICS, _backfill_statistics)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    moved = migrate_inputs(dict(entry.options))
    if moved != dict(entry.options):
        # before the update listener exists, so this does not reload
        hass.config_entries.async_update_entry(entry, options=moved)
    coordinator = InsightsCoordinator(hass, entry)
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
        suggested_area=_area_of_the_meter(hass, SiteModel.from_prefs((await async_get_manager(hass)).data)),
    )
    entry.runtime_data = RuntimeData(coordinator, detection, site_device.id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_changed))
    _prune_empty_devices(hass, entry)
    # The first forecast reads weeks of statistics and fits every device, and
    # Home Assistant's start waited on it every time ("Waiting for
    # integrations to complete setup: load_insights"). The entities exist
    # already and show unavailable until it lands; a background task, so the
    # start does not wait on it either. A failure is logged and the next
    # quarter hour tries again, where setup used to be retried (2026-09-29).
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN} first forecast")
    return True


@callback
def _area_of_the_meter(hass: HomeAssistant, site: SiteModel) -> str | None:
    """The area the grid import meter sits in, if it has one - the closest
    thing this integration has to a physical location. From the dashboard
    rather than the first refresh, which setup no longer waits for."""
    from homeassistant.helpers import area_registry as ar

    areas = ar.async_get(hass)
    for stat_id in site.grid_import:
        area_id = _area_id(hass, stat_id)
        area = areas.async_get_area(area_id) if area_id else None
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
        await entry.runtime_data.coordinator.async_shutdown()
        await entry.runtime_data.runner.async_stop()
    return ok
