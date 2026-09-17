"""Setup: confirm what the Energy dashboard holds, name the site, link the
explanatory inputs. Options: change the inputs later."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
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
from .insights.discovery import describe_match, match_meter_entities
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


def _device_field(default=None):
    return {
        vol.Optional("device", description={"suggested_value": default}): selector.DeviceSelector(
            selector.DeviceSelectorConfig(
                entity=[selector.EntityFilterSelectorConfig(domain="sensor", device_class="power")]
            )
        )
    }


def _discover(hass, device_id: str) -> dict:
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
    return match_meter_entities(rows)


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

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(step_id="init", menu_options=["inputs", "device_state", "detection", "naming"])

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
            await runner.async_rename(chosen, user_input.get("name"))
            # the entry has to change for the entities to be rebuilt
            rev = int(self.config_entry.options.get(CONF_SIGNATURE_REVISION, 0)) + 1
            return self.async_create_entry(
                data={**dict(self.config_entry.options), CONF_SIGNATURE_REVISION: rev}
            )

        tz = dt_util.DEFAULT_TIME_ZONE
        levels = {i: n for n, group in enumerate(suggest_levels(runner.detector.signatures, runner.detector.recent), 1) for i in group}
        options = []
        parents = runner.parents
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
            options.append(selector.SelectOptionDict(value=str(sig.id), label=label))
        current = candidates[0]
        return self.async_show_form(
            step_id="naming",
            data_schema=vol.Schema({
                vol.Required("signature", default=str(current.id)): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional("name"): selector.TextSelector(),
            }),
            description_placeholders={"count": str(len(candidates))},
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
                        "found": describe_match({k: v for k, v in offer.items() if k != "device"})},
                )
            cfg = {k: v for k, v in user_input.items() if v}
            return self.async_create_entry(data={**dict(self.config_entry.options), CONF_DETECTION: cfg})
        return self.async_show_form(
            step_id="detection",
            data_schema=vol.Schema({**_device_field(current.get("device")), **_meter_fields(current)}),
            description_placeholders={"found": describe_match({k: v for k, v in current.items() if k != "device"})},
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
