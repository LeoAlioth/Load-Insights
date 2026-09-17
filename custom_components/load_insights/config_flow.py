"""Setup: confirm what the Energy dashboard holds, name the site, link the
explanatory inputs. Options: change the inputs later."""
from __future__ import annotations

import logging

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    CONF_GRID_DEVICE,
    CONF_GRID_PREFIX,
    CONF_LAYOUT,
    LAYOUTS,
    LAYOUT_AUTO,
    CONF_CALENDAR_ENTITIES,
    CONF_DETECTION,
    CONF_DEVICE_STATE_SENSORS,
    CONF_INPUT_ENTITIES,
    CONF_SIGNATURE_REVISION,
    DETECTION_KINDS,
    CONF_NAME,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULT_NAME,
    DOMAIN,
)
from homeassistant.util import dt as dt_util

from .insights.detect import describe_location, suggest_levels
from .overview import overview_text

_LOGGER = logging.getLogger(__name__)
from .insights.discovery import KIND_BY_DEVICE_CLASS, describe_match, match_meter_entities
from .insights.model import SiteModel


def _meter_fields(defaults: dict) -> dict:
    """The twelve per-phase fields, pre-filled."""
    classes = {"power": "power", "pf": "power_factor", "current": "current", "voltage": "voltage"}
    out = {}
    for kind in DETECTION_KINDS:
        for p in ("a", "b", "c"):
            key = f"{kind}_{p}"
            out[vol.Optional(key, description={"suggested_value": defaults.get(key)})] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class=classes[kind])
            )
    return out


def _grid_fields(defaults: dict) -> dict:
    """The grid connection: per-phase power, and how it is wired."""
    out = {vol.Optional(CONF_GRID_DEVICE, description={"suggested_value": defaults.get(CONF_GRID_DEVICE)}):
           selector.DeviceSelector(selector.DeviceSelectorConfig(
               entity=[selector.EntityFilterSelectorConfig(domain="sensor", device_class="power")]))}
    for p in ("a", "b", "c"):
        key = f"{CONF_GRID_PREFIX}{p}"
        out[vol.Optional(key, description={"suggested_value": defaults.get(key)})] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power"))
    out[vol.Optional(CONF_LAYOUT, default=defaults.get(CONF_LAYOUT, LAYOUT_AUTO))] = selector.SelectSelector(
        selector.SelectSelectorConfig(options=list(LAYOUTS), translation_key=CONF_LAYOUT,
                                      mode=selector.SelectSelectorMode.DROPDOWN))
    return out


def _device_field(default=None):
    return {
        vol.Optional("device", description={"suggested_value": default}): selector.DeviceSelector(
            selector.DeviceSelectorConfig(
                entity=[selector.EntityFilterSelectorConfig(domain="sensor", device_class="power")]
            )
        )
    }


def _disabled_readings(hass, device_id: str) -> int:
    """How many of the device's electrical readings are disabled.

    A disabled entity has no state and no history, so discovery cannot use
    it and the device page hides it behind "+N entities not shown". That is
    how a meter can appear to publish nothing useful while the reading you
    want is one click from existing (Anze, 2026-09-17)."""
    registry = er.async_get(hass)
    wanted = set(KIND_BY_DEVICE_CLASS)
    return len([
        e for e in er.async_entries_for_device(registry, device_id, include_disabled_entities=True)
        if e.disabled and e.domain == "sensor"
        and (e.device_class or e.original_device_class) in wanted
    ])


def _found_line(hass, cfg: dict) -> str:
    """What was matched, and what could not be because it is switched off."""
    line = describe_match({k: v for k, v in cfg.items() if k != "device"})
    device = cfg.get("device")
    hidden = _disabled_readings(hass, device) if device else 0
    if hidden:
        line += (f" - {hidden} more electrical readings on this device are DISABLED, "
                 "so nothing can use them; enable them on the device page if the one "
                 "you want is missing")
    return line


def _discover(hass, device_id: str, role: str = "load") -> dict:
    """The device's sensors, matched to per-phase fields."""
    registry = er.async_get(hass)
    rows = []
    for e in er.async_entries_for_device(registry, device_id, include_disabled_entities=False):
        if e.domain != "sensor":
            continue
        state = hass.states.get(e.entity_id)
        device_class = (
            e.device_class
            or e.original_device_class
            or (state.attributes.get("device_class") if state else None)
        )
        name = e.name or e.original_name or (state.attributes.get("friendly_name") if state else "") or ""
        rows.append({"entity_id": e.entity_id, "device_class": device_class, "name": name})
    return match_meter_entities(rows, role)


def _single_weather_entity(hass) -> str | None:
    """The instance's weather entity when there is exactly one - the usual
    case, and the reason the field can be pre-filled 'by default'."""
    ids = hass.states.async_entity_ids("weather")
    return ids[0] if len(ids) == 1 else None


def _inputs_schema(defaults: dict) -> dict:
    return {
        vol.Optional(CONF_WEATHER_ENTITY, description={"suggested_value": defaults.get(CONF_WEATHER_ENTITY)}):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
        vol.Optional(CONF_OUTDOOR_TEMPERATURE_ENTITY, description={"suggested_value": defaults.get(CONF_OUTDOOR_TEMPERATURE_ENTITY)}):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature")),
        vol.Optional(CONF_CALENDAR_ENTITIES, description={"suggested_value": defaults.get(CONF_CALENDAR_ENTITIES) or []}):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="calendar", multiple=True)),
        vol.Optional(CONF_INPUT_ENTITIES, description={"suggested_value": defaults.get(CONF_INPUT_ENTITIES) or []}):
            selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
    }


class LoadInsightsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return LoadInsightsOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        manager = await async_get_manager(self.hass)
        site = SiteModel.from_prefs(manager.data)
        if not site.has_sources:
            return self.async_abort(reason="no_energy_dashboard")

        if user_input is not None:
            data = {CONF_NAME: user_input[CONF_NAME]}
            options = {k: v for k, v in user_input.items() if k in (CONF_WEATHER_ENTITY, CONF_OUTDOOR_TEMPERATURE_ENTITY, CONF_CALENDAR_ENTITIES, CONF_INPUT_ENTITIES) and v}
            return self.async_create_entry(title=data[CONF_NAME], data=data, options=options)

        s = site.summary()
        schema = {vol.Required(CONF_NAME, default=DEFAULT_NAME): str}
        schema.update(_inputs_schema({CONF_WEATHER_ENTITY: _single_weather_entity(self.hass)}))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "grid": str(s["grid"]), "solar": str(s["solar"]), "battery": str(s["battery"]),
                "devices": str(s["devices"]), "forecasts": str(s["forecasts"]),
            },
        )


class LoadInsightsOptionsFlow(config_entries.OptionsFlow):
    """The site-level inputs, a device's own state sensor, and the meters."""

    def __init__(self) -> None:
        self._pending_detection: dict | None = None
        self._naming_selected: int | None = None
        self._pending_grid: dict | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["overview", "inputs", "device_state", "detection", "grid", "naming"],
        )

    async def async_step_overview(self, user_input: dict[str, Any] | None = None):
        """What it sees right now, read-only.

        A MENU rather than a form, the way Load Juggler does it: a form's
        button says Submit, which reads as if something is being saved, while
        menu options are real labelled buttons - Refresh re-enters this step
        and rebuilds the text, Reset asks before forgetting anything."""
        try:
            text = overview_text(self.hass, self.config_entry.entry_id)
        except Exception:  # noqa: BLE001 - a display page must never break
            _LOGGER.exception("Could not build the overview page")
            text = "Could not read the live data - see the Home Assistant log."
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        options = ["overview"]
        if runner is not None and runner.enabled:
            options.append("reset_detection")
        options.append("init")
        return self.async_show_menu(step_id="overview", menu_options=options,
                                    description_placeholders={"overview": text})

    async def async_step_grid(self, user_input: dict[str, Any] | None = None):
        """The grid connection, which completes the load signal.

        Its own page because it is a different ROLE, not a different meter:
        the same inverter usually publishes both sides, and which one is
        wanted depends on the question. Matching here prefers the input,
        grid and mains names that the load page pushes away."""
        detection = dict(self.config_entry.options.get(CONF_DETECTION) or {})
        if user_input is not None:
            device = user_input.get(CONF_GRID_DEVICE)
            pending, self._pending_grid = self._pending_grid, None
            if device and pending is None:
                found = {f"{CONF_GRID_PREFIX}{p}": v for k, v in
                         _discover(self.hass, device, role="grid").items()
                         for p in [k.rsplit("_", 1)[1]] if k.startswith("power_")}
                typed = {k: v for k, v in user_input.items() if v}
                merged = {**found, **typed}
                if device != detection.get(CONF_GRID_DEVICE) or merged != typed:
                    self._pending_grid = merged
                    return self.async_show_form(
                        step_id="grid", data_schema=vol.Schema(_grid_fields(merged)),
                        description_placeholders={"found": _found_line(self.hass, {
                            **{k.replace(CONF_GRID_PREFIX, "power_"): v for k, v in merged.items()
                               if k.startswith(CONF_GRID_PREFIX)},
                            "device": device})},
                    )
            keep = {k: v for k, v in detection.items()
                    if not k.startswith(CONF_GRID_PREFIX) and k not in (CONF_GRID_DEVICE, CONF_LAYOUT)}
            cfg = {**keep, **{k: v for k, v in user_input.items() if v}}
            return self.async_create_entry(
                data={**dict(self.config_entry.options), CONF_DETECTION: cfg})
        current = dict(self._pending_grid or detection)
        return self.async_show_form(
            step_id="grid", data_schema=vol.Schema(_grid_fields(current)),
            description_placeholders={"found": _found_line(self.hass, {
                **{k.replace(CONF_GRID_PREFIX, "power_"): v for k, v in current.items()
                   if k.startswith(CONF_GRID_PREFIX)},
                "device": current.get(CONF_GRID_DEVICE)})},
        )

    async def async_step_reset_detection(self, user_input: dict[str, Any] | None = None):
        """Ask before forgetting: the library and every name in it go."""
        return self.async_show_menu(step_id="reset_detection",
                                    menu_options=["reset_confirmed", "overview"])

    async def async_step_reset_confirmed(self, user_input: dict[str, Any] | None = None):
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        if runner is not None:
            await runner.async_reset()
        return await self.async_step_overview()

    async def async_step_naming(self, user_input: dict[str, Any] | None = None):
        """Name a detected load. One per visit; giving two signatures the same
        name says they are one device (a hob on two settings), and clearing
        the name forgets it again."""
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        if runner is None or not runner.enabled:
            return self.async_abort(reason="no_detection")
        candidates = runner.unlocated()
        if not candidates:
            return self.async_abort(reason="nothing_to_name")

        if user_input is not None:
            chosen = int(user_input["signature"])
            name = (user_input.get("name") or "").strip()
            if not name and self._naming_selected != chosen:
                # picked, but not named yet: show what this one looks like
                # before asking for a name - a config flow cannot draw a
                # chart, so the description carries the day's shape and the
                # three confidences (Anze, 2026-09-17)
                self._naming_selected = chosen
                return self._naming_form(runner, candidates, chosen)
            self._naming_selected = None
            await runner.async_rename(chosen, name or None)
            # the entry has to change for the entities to be rebuilt
            rev = int(self.config_entry.options.get(CONF_SIGNATURE_REVISION, 0)) + 1
            return self.async_create_entry(
                data={**dict(self.config_entry.options), CONF_SIGNATURE_REVISION: rev}
            )

        self._naming_selected = candidates[0].id
        return self._naming_form(runner, candidates, candidates[0].id)

    def _naming_form(self, runner, candidates: list, selected: int):
        """The picker, with the selected load spelled out underneath."""
        tz = dt_util.DEFAULT_TIME_ZONE
        levels = {i: n for n, group in enumerate(suggest_levels(runner.detector.signatures, runner.detector.recent), 1) for i in group}
        parents = runner.parents
        options = []
        for sig in candidates:
            label = sig.describe(tz)
            if sig.locations:
                # nothing owns these - they are all "main" - but a meter that
                # saw it SOMETIMES still narrows where it is
                label += f", {describe_location(sig.locations, sig.count, parents, sig.phases)}"
            if sig.name:
                label = f"{sig.name} - {label}"
            if sig.id in levels:
                label += f"  [looks like set {levels[sig.id]} of one device]"
            # the list is ordered by this, so it has to be visible - otherwise
            # it reads as no order at all (Anze, 2026-09-17)
            label = f"[{sig.evidence:.2f}] {label}"
            options.append(selector.SelectOptionDict(value=str(sig.id), label=label))
        current = next((s for s in candidates if s.id == selected), candidates[0])
        return self.async_show_form(
            step_id="naming",
            data_schema=vol.Schema({
                vol.Required("signature", default=str(current.id)): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional("name", description={"suggested_value": current.name}): selector.TextSelector(),
            }),
            description_placeholders={"count": str(len(candidates)),
                                      "detail": current.detail(tz, parents)},
        )

    async def async_step_detection(self, user_input: dict[str, Any] | None = None):
        """The main meter. Pick the DEVICE and its per-phase readings are
        found for you; the fields below are shown filled in so you can check
        them before saving, and can be set by hand instead.

        Re-submitting the SAME meter offers whatever is still empty, which is
        how a site set up before a naming was recognised picks it up - what is
        already filled in is never touched, by discovery or by a later run."""
        current = dict(self._pending_detection or self.config_entry.options.get(CONF_DETECTION) or {})
        if user_input is not None:
            device = user_input.get("device")
            # consumed here, so the form we may show below is saved on its
            # own submit rather than re-offered forever
            pending, self._pending_detection = self._pending_detection, None
            offer = None
            if device and pending is None:
                found = _discover(self.hass, device)
                if device != current.get("device"):
                    offer = {"device": device, **found}  # another meter, its own readings
                else:
                    typed = {k: v for k, v in user_input.items() if v}
                    merged = {**found, **typed}  # what the user set wins
                    if merged != typed:
                        offer = merged
            if offer is not None:
                self._pending_detection = offer
                return self.async_show_form(
                    step_id="detection",
                    data_schema=vol.Schema({**_device_field(offer.get("device")), **_meter_fields(offer)}),
                    description_placeholders={
                        "found": _found_line(self.hass, offer)},
                )
            cfg = {k: v for k, v in user_input.items() if v}
            return self.async_create_entry(data={**dict(self.config_entry.options), CONF_DETECTION: cfg})
        return self.async_show_form(
            step_id="detection",
            data_schema=vol.Schema({**_device_field(current.get("device")), **_meter_fields(current)}),
            description_placeholders={"found": _found_line(self.hass, current)},
        )

    async def async_step_inputs(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            # An emptied selector clears the input; only set keys are kept.
            # The per-device map lives on another page and is carried over.
            keep = {CONF_DEVICE_STATE_SENSORS: self.config_entry.options.get(CONF_DEVICE_STATE_SENSORS, {}),
                    CONF_DETECTION: self.config_entry.options.get(CONF_DETECTION, {})}
            return self.async_create_entry(data={**keep, **{k: v for k, v in user_input.items() if v}})
        current = dict(self.config_entry.options)
        if not current.get(CONF_WEATHER_ENTITY):
            current[CONF_WEATHER_ENTITY] = _single_weather_entity(self.hass)
        return self.async_show_form(step_id="inputs", data_schema=vol.Schema(_inputs_schema(current)))

    async def async_step_device_state(self, user_input: dict[str, Any] | None = None):
        """Pick a device the Energy dashboard lists, then the sensor that says
        what it will do next. One device per visit; an emptied sensor clears
        that device's mapping."""
        manager = await async_get_manager(self.hass)
        site = SiteModel.from_prefs(manager.data)
        labels = {d.energy: d.label for d in site.devices}
        current = dict(self.config_entry.options.get(CONF_DEVICE_STATE_SENSORS) or {})
        if user_input is not None:
            device = user_input.get("device")
            sensor_id = user_input.get("state_entity")
            if device:
                if sensor_id:
                    current[device] = sensor_id
                else:
                    current.pop(device, None)
            options = {**dict(self.config_entry.options), CONF_DEVICE_STATE_SENSORS: current}
            return self.async_create_entry(data=options)
        if not labels:
            return self.async_abort(reason="no_devices")
        return self.async_show_form(
            step_id="device_state",
            data_schema=vol.Schema({
                vol.Required("device"): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=k, label=f"{v}  ({current.get(k) or '-'})") for k, v in labels.items()],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
                vol.Optional("state_entity"): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "input_number", "number"])),
            }),
        )
