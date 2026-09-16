"""Setup: confirm what the Energy dashboard holds, name the site, link the
explanatory inputs. Options: change the inputs later."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CALENDAR_ENTITIES,
    CONF_DETECTION,
    CONF_DEVICE_STATE_SENSORS,
    CONF_SUBMETERS,
    DETECTION_KINDS,
    CONF_NAME,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULT_NAME,
    DOMAIN,
)
from .insights.model import SiteModel


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
            options = {k: v for k, v in user_input.items() if k in (CONF_WEATHER_ENTITY, CONF_OUTDOOR_TEMPERATURE_ENTITY, CONF_CALENDAR_ENTITIES) and v}
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
    """Two pages: the site-level inputs, and one device's own state sensor."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(step_id="init", menu_options=["inputs", "device_state", "detection", "submeter"])

    async def async_step_submeter(self, user_input: dict[str, Any] | None = None):
        """A downstream 3-phase meter - a Shelly 3EM on a subpanel. Same
        fields as the main meter plus a name. A load the main meter and this
        meter both see is located here. Edit an existing one by name; a name
        with every field empty removes it."""
        subs = dict(self.config_entry.options.get(CONF_SUBMETERS) or {})
        if user_input is not None:
            name = (user_input.get("name") or "").strip()
            fields = {k: v for k, v in user_input.items() if k != "name" and v}
            if name:
                if fields:
                    subs[name] = fields
                else:
                    subs.pop(name, None)
            return self.async_create_entry(data={**dict(self.config_entry.options), CONF_SUBMETERS: subs})
        schema: dict = {vol.Required("name"): selector.TextSelector()}
        classes = {"power": "power", "pf": "power_factor", "current": "current", "voltage": "voltage"}
        for kind in DETECTION_KINDS:
            for p in ("a", "b", "c"):
                schema[vol.Optional(f"{kind}_{p}")] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class=classes[kind])
                )
        return self.async_show_form(
            step_id="submeter", data_schema=vol.Schema(schema),
            description_placeholders={"existing": ", ".join(sorted(subs)) or "-"},
        )

    async def async_step_detection(self, user_input: dict[str, Any] | None = None):
        """The meter's raw per-phase readings for load detection. Active
        power per phase is what matters; PF, current and voltage refine the
        signatures where the meter has them. All optional."""
        if user_input is not None:
            cfg = {k: v for k, v in user_input.items() if v}
            options = {**dict(self.config_entry.options), CONF_DETECTION: cfg}
            return self.async_create_entry(data=options)
        current = dict(self.config_entry.options.get(CONF_DETECTION) or {})
        schema = {}
        classes = {"power": "power", "pf": "power_factor", "current": "current", "voltage": "voltage"}
        for kind in DETECTION_KINDS:
            for p in ("a", "b", "c"):
                key = f"{kind}_{p}"
                schema[vol.Optional(key, description={"suggested_value": current.get(key)})] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class=classes[kind])
                )
        return self.async_show_form(step_id="detection", data_schema=vol.Schema(schema))

    async def async_step_inputs(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            # An emptied selector clears the input; only set keys are kept.
            # The per-device map lives on another page and is carried over.
            keep = {CONF_DEVICE_STATE_SENSORS: self.config_entry.options.get(CONF_DEVICE_STATE_SENSORS, {}),
                    CONF_DETECTION: self.config_entry.options.get(CONF_DETECTION, {}),
                    CONF_SUBMETERS: self.config_entry.options.get(CONF_SUBMETERS, {})}
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
