"""Setup: confirm what the Energy dashboard holds, name the site, link the
explanatory inputs. Options: change the inputs later."""
from __future__ import annotations

from datetime import datetime

import logging

from typing import Any, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, selector

from .const import (
    CONF_GRID_DEVICE,
    CONF_LAYOUT,
    ROLE_PREFIX,
    CONF_INVERTERS,
    CONF_INV_DEVICE,
    CONF_INV_INPUT_PREFIX,
    CONF_INV_TOPOLOGY,
    SOURCE_KINDS,
    CONF_SOURCE_KIND,
    DEFAULT_SOURCE_KIND,
    LAYOUT_PARALLEL,
    LAYOUT_SERIES,
    CONF_CALENDAR_ENTITIES,
    CONF_DETECTION,
    CONF_SINGLE_DEVICE,
    CONF_DETECTION_INTERVAL,
    CONF_METER_WAIT,
    DETECTION_BACKFILL_DAYS,
    NAMING_MAX_STALE_S,
    DETECTION_INTERVAL_CHOICES,
    DETECTION_INTERVAL_MINUTES,
    CONF_INPUT_ENTITIES,
    CONF_INPUT_LINKS,
    CONF_SIGNATURE_REVISION,
    CONF_NAME,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULT_NAME,
    DOMAIN,
)
from homeassistant.util import dt as dt_util

from .insights.detect import (EDGE_HELPED_SHARE, METER_WAIT_CAP_S, SWITCH_PREFIX, edge_story, most_specific, same_device_phrase,
                              input_groups, suggest_levels)
from .overview import overview_text

_LOGGER = logging.getLogger(__name__)
NAMING_MAX_ROWS = 24           # the menu's length; the rest wait for the next visit
NAMING_MAX_GROUPS = 12         # meters on the first page; the translations carry this many rows
NAMED = "\x00named"            # the named loads' page, which is not a meter's
from .insights.classify import _fmt_s, _fmt_w
from .insights.phases import KIND_BY_DEVICE_CLASS, describe_match, match_meter_entities
from .detection import named_load_energy
from .insights.model import LOAD_PREFIX, SiteModel, add_inputs, relink, suggest_inputs
from .insights.named import chosen_name

# What a room says about the devices in it: whether someone is there, and its
# air. Offered on the suggested-inputs page, by device class.
SUGGESTED_CLASSES = {"binary_sensor": ("occupancy", "presence", "motion"), "sensor": ("temperature", "humidity")}


def _grid_fields(defaults: dict) -> dict:
    """The grid connection: its whole electrical set, and how it is wired.

    Voltage and current live here and not only on the load page because they
    belong to the METER that publishes them. A power factor is only a power
    factor when the watts and the amps are the same circuit, and the load
    page is often pointed at a template with no volts or amps of its own -
    which is home, where the grid meter is the one carrying every household
    watt and so the one whose reactive power steps when a load switches."""
    out = {vol.Optional(CONF_GRID_DEVICE, description={"suggested_value": defaults.get(CONF_GRID_DEVICE)}):
           selector.DeviceSelector(selector.DeviceSelectorConfig(
               entity=[selector.EntityFilterSelectorConfig(domain="sensor", device_class="power")]))}
    for kind, device_class in (("power", "power"), ("voltage", "voltage"),
                               ("current", "current"), ("pf", "power_factor")):
        for p in ("a", "b", "c"):
            key = f"{ROLE_PREFIX['grid']}{kind}_{p}"
            out[vol.Optional(key, description={"suggested_value": defaults.get(key)})] = \
                selector.EntitySelector(selector.EntitySelectorConfig(
                    domain="sensor", device_class=device_class))
    out[vol.Optional(CONF_SOURCE_KIND,
                     default=defaults.get(CONF_SOURCE_KIND, DEFAULT_SOURCE_KIND))] = selector.SelectSelector(
        selector.SelectSelectorConfig(options=list(SOURCE_KINDS), translation_key=CONF_SOURCE_KIND,
                                      mode=selector.SelectSelectorMode.DROPDOWN))
    return out


def _inverter_fields(defaults: dict) -> dict:
    """One inverter: what it puts out, where that output lands, and whether
    its battery sits in front of the conversion or behind it.

    Edited one at a time, stored as a LIST - a site already exists with two,
    a SolarEdge cabled to a Deye hybrid's load port, and where an inverter
    ATTACHES is what tells that apart from the same two boxes side by side.
    """
    out = {vol.Optional(CONF_INV_DEVICE, description={"suggested_value": defaults.get(CONF_INV_DEVICE)}):
           selector.DeviceSelector(selector.DeviceSelectorConfig(
               entity=[selector.EntityFilterSelectorConfig(domain="sensor", device_class="power")]))}
    for prefix in ("", CONF_INV_INPUT_PREFIX):
        out[vol.Optional(f"{prefix}power",
                         description={"suggested_value": defaults.get(f"{prefix}power")})] = \
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power"))
        for p in ("a", "b", "c"):
            key = f"{prefix}power_{p}"
            out[vol.Optional(key, description={"suggested_value": defaults.get(key)})] = \
                selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power"))
    # The OUTPUT side's volts and amps. Where the loads hang off an inverter -
    # Kozolec, where the MultiPlus output IS the house - this is the circuit
    # they are in, so it is the only place a power factor for them can come
    # from. The grid side's own readings live on the Grid connection page.
    for kind, device_class in (("voltage", "voltage"), ("current", "current"), ("pf", "power_factor")):
        for p in ("a", "b", "c"):
            key = f"{kind}_{p}"
            out[vol.Optional(key, description={"suggested_value": defaults.get(key)})] = \
                selector.EntitySelector(selector.EntitySelectorConfig(
                    domain="sensor", device_class=device_class))
    out[vol.Optional(CONF_INV_TOPOLOGY, default=defaults.get(CONF_INV_TOPOLOGY, LAYOUT_PARALLEL))] = \
        selector.SelectSelector(selector.SelectSelectorConfig(
            options=[LAYOUT_PARALLEL, LAYOUT_SERIES], translation_key=CONF_LAYOUT,
            mode=selector.SelectSelectorMode.DROPDOWN))

    return out


def _inverter_line(inverters: list) -> str:
    if not inverters:
        return ("None set up - the Energy dashboard's solar sources are used instead. "
                "Add one here to override that, which is what a site whose arrays are "
                "DC-coupled needs: their power never appears on the AC side at all.")
    bits = []
    for inv in inverters:
        bits.append(f"{inv.get('label') or inv.get(CONF_INV_DEVICE, '?')[:8]} "
                    f"({inv.get(CONF_INV_TOPOLOGY, LAYOUT_PARALLEL)})")
    return f"{len(inverters)} set up: " + "; ".join(bits)


def _load_line(hass, entry) -> str:
    """Where the house figure is coming from, in words."""
    cfg = entry.options.get(CONF_DETECTION) or {}
    own = [p.upper() for p in ("a", "b", "c") if cfg.get(f"power_{p}")]
    if own:
        return (f"Using the reading you set by hand on {'+'.join(own)}. Clear those fields and it "
                "is worked out from the grid connection and the inverters instead.")
    grid = any(cfg.get(f"{ROLE_PREFIX['grid']}power_{p}") for p in ("a", "b", "c"))
    inverters = entry.options.get(CONF_INVERTERS) or []
    if not grid and not inverters:
        return ("Nothing to work with yet - set up the grid connection, the inverters, or both. "
                "The house is their sum: the meter, plus what each inverter puts out less what "
                "it takes in.")
    parts = []
    if grid:
        parts.append("the grid meter")
    if inverters:
        parts.append(f"{len(inverters)} inverter(s)")
    return "House consumption is worked out from " + " and ".join(parts) + "."


def _group_title(where: str, hass=None) -> str:
    """A naming group's row: the meter's name, or where there is none - and
    for a switch, what switches it: the one device it turns on, unmetered."""
    if where == "main":
        return "Under no meter"
    if where.startswith(SWITCH_PREFIX):
        entity_id = where[len(SWITCH_PREFIX):]
        st = hass.states.get(entity_id) if hass is not None else None
        return f"Switched by {st.attributes.get('friendly_name') or entity_id if st else entity_id}"
    return where


def _helpers(hass, sig, edges=()) -> list:
    """(input, what it says about this load) for each input that helped find
    it - so an input added for the whole site can be linked to the load it
    turned out to explain once it is named (Anze, 2026-09-28)."""
    def label(eid):
        st = hass.states.get(eid)
        return (st.attributes.get("friendly_name") if st is not None else None) or eid, st

    out = []
    story = edge_story(edges, sig)
    said = {}
    for role, verb in (("start", "Starts"), ("stop", "Stops")):
        for eid, seen in ((story.get(role) or {}).get("signals") or {}).items():
            if seen["share"] < EDGE_HELPED_SHARE:
                continue
            was, _, now = seen["kind"].partition("→")
            what = {"off→on": "turning on", "on→off": "turning off"}.get(seen["kind"], f"going from {was} to {now}")
            when = ("" if seen["lag_s"] is None else
                    f", {abs(seen['lag_s']):.0f} s {'before' if seen['lag_s'] < 0 else 'after'} the meter sees it")
            said.setdefault(eid, []).append(f"{verb} with {label(eid)[0]} {what}{when}: {seen['share']:.0%} of its "
                                            f"{role}s")
    for eid, lines in said.items():
        out.append((eid, "; ".join(lines)))
    tied = sig.strongest_input()
    if tied is not None:
        eid, value, share, lift = tied
        out.append((eid, f"Runs while {label(eid)[0]} is {value}: {share:.0%} of its runs, "
                         f"{lift:.0f} times what chance would give"))
    for key in ("d", "g"):
        found = sig.strongest_driver(key)
        if found is None:
            continue
        eid, per_unit, r2 = found
        name, st = label(eid)
        unit = (st.attributes.get("unit_of_measurement") if st is not None else None) or "unit"
        way = ("longer" if per_unit > 0 else "shorter") if key == "d" else ("longer" if per_unit > 0 else "less")
        what = "Runs" if key == "d" else "Waits"
        tail = "" if key == "d" else " between runs"
        out.append((eid, f"{what} {abs(per_unit) * 100:.0f} % {way}{tail} for each {unit} {name} is higher "
                         f"- that explains {r2:.0%} of how much it varies"))
    return out


def _meter_entity(runner, where: str) -> Optional[str]:
    """An entity behind a meter's readings - for a switch, the switch."""
    if where.startswith(SWITCH_PREFIX):
        return where[len(SWITCH_PREFIX):]
    fields = (runner.submeters.get(where) or {}).get("fields") or {}
    return next(iter(fields.values()), None)


def _area_id(hass, entity_id: Optional[str]) -> Optional[str]:
    """An entity's own area where it has one, else its device's."""
    entity = er.async_get(hass).async_get(entity_id) if entity_id else None
    if entity is None or entity.area_id or not entity.device_id:
        return entity.area_id if entity is not None else None
    device = dr.async_get(hass).async_get(entity.device_id)
    return device.area_id if device else None


def _meter_place(hass, runner, where: str) -> str:
    """The area and floor of the device behind a meter's readings - the
    entity's own area where it has one, else its device's."""
    area_id = _area_id(hass, _meter_entity(runner, where))
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    if area is None:
        return ""
    floor = None
    if getattr(area, "floor_id", None):
        from homeassistant.helpers import floor_registry as fr   # HA 2024.4+
        found = fr.async_get(hass).async_get_floor(area.floor_id)
        floor = found.name if found else None
    return f"{area.name}, {floor}" if floor else area.name


def _group_detail(hass, runner, where: str, shown: int, waiting: int) -> str:
    """Under a group's row: how many loads it offers, how many wait behind
    them, and where the meter is."""
    parts = [f"{shown} load{'s' if shown != 1 else ''} to name"]
    if waiting:
        parts.append(f"{waiting} more waiting")
    place = "no meter saw these" if where == "main" else _meter_place(hass, runner, where)
    if place:
        parts.append(place)
    return " · ".join(parts)


def _behind(seconds: Optional[float]) -> str:
    if seconds is None:
        return "it has not read anything yet"
    if seconds < 3600:
        return f"{seconds / 60:.0f} minutes behind"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} hours behind"
    return f"{seconds / 86400:.1f} days behind"


def _since(when: float) -> str:
    days = (dt_util.utcnow().timestamp() - when) / 86400.0
    if days < 2:
        return f"for {days * 24:.0f} hours"
    return f"for {days:.0f} days" if days < 60 else f"since {datetime.fromtimestamp(when, dt_util.DEFAULT_TIME_ZONE):%-d %b %Y}"


def _interval_field(defaults: dict) -> dict:
    """How often the recorder is re-read. Its cost no longer scales with it -
    one query per pass rather than one per entity, and the state written
    hourly rather than every pass - so the default is a minute and slowing it
    down is for a large site or slow storage, not for a quiet one.

    It is the only thing left here. "How sure before offering a load" and
    "smallest change to notice" were removed (Anze, 2026-09-23): the first
    only filtered the naming page, which already lengthens as loads are named
    and never runs dry, and nobody can say what an evidence of 0.7 should be;
    the second is a floor under each phase's MEASURED noise that 1 to 10 W
    left the scored loads alone at both sites. Both are fixed values now -
    DEFAULT_MIN_EVIDENCE and MIN_NOISE_W - and a stored choice is ignored."""
    out = {}
    out.update({vol.Optional(CONF_DETECTION_INTERVAL,
                         default=defaults.get(CONF_DETECTION_INTERVAL, DETECTION_INTERVAL_MINUTES)):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=[str(n) for n in DETECTION_INTERVAL_CHOICES],
                translation_key=CONF_DETECTION_INTERVAL,
                mode=selector.SelectSelectorMode.DROPDOWN))})
    # how long the grid's steps wait for the meters below it - see METER_WAIT_CAP_S
    out.update({vol.Optional(CONF_METER_WAIT, default=defaults.get(CONF_METER_WAIT, METER_WAIT_CAP_S)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=300, step=5, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX))})
    return out


def _single_device_field(meters: list, declared) -> dict:
    """Which meters hold ONE device. Until the page is saved the box shows
    what the library's shape suggests (the Hidrofor plug: 99 % of its
    sightings are one load); saved, the answer is the user's."""
    default = list(declared) if declared is not None else [stat for stat, _, guess in meters if guess]
    return {vol.Optional(CONF_SINGLE_DEVICE, default=default): selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=stat, label=name) for stat, name, _ in meters],
            multiple=True, mode=selector.SelectSelectorMode.LIST))}


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
        rows.append({"entity_id": e.entity_id, "device_class": device_class, "name": name,
                     "device_id": e.device_id})
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
        self._naming_selected: int | None = None
        self._pending_grid: dict | None = None
        self._naming_rows: list[int] = []      # menu position -> signature id

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["overview", "inputs", "input_links", "suggested_inputs", "detection", "grid",
                          "inverters", "naming"],
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
                # every kind, not just watts: the meter's own volts and amps
                # are what make its reactive power meaningful
                found = {f"{ROLE_PREFIX['grid']}{k}": v
                         for k, v in _discover(self.hass, device, role="grid").items()}
                typed = {k: v for k, v in user_input.items() if v}
                merged = {**found, **typed}
                if device != detection.get(CONF_GRID_DEVICE) or merged != typed:
                    self._pending_grid = merged
                    return self.async_show_form(
                        step_id="grid", data_schema=vol.Schema(_grid_fields(merged)),
                        description_placeholders={"found": _found_line(self.hass, {
                            **{k[len(ROLE_PREFIX["grid"]):]: v for k, v in merged.items()
                               if k.startswith(ROLE_PREFIX["grid"]) and k != CONF_GRID_DEVICE},
                            "device": device})},
                    )
            keep = {k: v for k, v in detection.items()
                    if not k.startswith(ROLE_PREFIX["grid"])
                    and k not in (CONF_LAYOUT, CONF_SOURCE_KIND)}
            cfg = {**keep, **{k: v for k, v in user_input.items() if v}}
            return self.async_create_entry(
                data={**dict(self.config_entry.options), CONF_DETECTION: cfg})
        current = dict(self._pending_grid or detection)
        return self.async_show_form(
            step_id="grid", data_schema=vol.Schema(_grid_fields(current)),
            description_placeholders={"found": _found_line(self.hass, {
                **{k[len(ROLE_PREFIX["grid"]):]: v for k, v in current.items()
                   if k.startswith(ROLE_PREFIX["grid"]) and k != CONF_GRID_DEVICE},
                "device": current.get(CONF_GRID_DEVICE)})},
        )

    async def async_step_inverters(self, user_input: dict[str, Any] | None = None):
        """Add or edit one inverter. Emptying every power reading removes it.

        Solar set up HERE wins over what the Energy dashboard lists, which is
        the override Kozolec needs: its arrays are two MPPTs charging the
        battery on the DC bus, so nothing about them is visible to any AC
        meter and no amount of looking at the AC side will find them."""
        current = list(self.config_entry.options.get(CONF_INVERTERS) or [])
        if user_input is not None:
            device = user_input.get(CONF_INV_DEVICE)
            # every reading, not just the ones whose key starts with "power":
            # that filter silently dropped the grid-side input the form had
            # just collected, and would have dropped the volts and amps too
            readings = {k: v for k, v in user_input.items() if v
                        and k not in (CONF_INV_DEVICE, CONF_INV_TOPOLOGY)}
            rest = [inv for inv in current if inv.get(CONF_INV_DEVICE) != device]
            if device and any(k.endswith("power") or "power_" in k for k in readings):
                rest.append({CONF_INV_DEVICE: device, **readings,
                             CONF_INV_TOPOLOGY: user_input.get(CONF_INV_TOPOLOGY, LAYOUT_PARALLEL)})
            return self.async_create_entry(
                data={**dict(self.config_entry.options), CONF_INVERTERS: rest})
        return self.async_show_form(
            step_id="inverters", data_schema=vol.Schema(_inverter_fields({})),
            description_placeholders={"found": _inverter_line(current)})

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
        """The meters the detected loads were seen on, one row each, and the
        named loads; each opens its own list (async_step_naming_list).

        A MENU rather than a form: its rows are real buttons, so choosing one
        goes straight to it with no submit, which is what a dropdown could
        never do. Their labels come from the translation of
        ``menu_options.<key>``, and the frontend passes this step's
        description_placeholders into that lookup - so a fixed key whose
        template is nothing but a placeholder carries whatever we put there,
        which is how each row shows its own numbers and its own day (Anze,
        2026-09-18)."""
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        if runner is None or not runner.enabled:
            return self.async_abort(reason="no_detection")
        # Nothing is worth naming from a library that is still being built:
        # the backfill walks ten days in six-hour slices, so part way through
        # it holds whatever happened in the first few days and the rows would
        # change under the reader. Offer a refresh instead - but only while
        # it is plausibly still working, since a detector wedged eight days
        # back should still show what it has rather than nothing at all.
        behind = runner.behind_s
        if behind is None or behind > NAMING_MAX_STALE_S:
            done = ""
            if behind is not None:
                total = DETECTION_BACKFILL_DAYS * 86400.0
                done = f"{max(0.0, min(100.0, 100.0 * (1.0 - behind / total))):.0f}"
            return self.async_show_menu(
                step_id="naming_waiting", menu_options=["naming", "init"],
                description_placeholders={
                    "progress": done or "0",
                    "behind": _behind(behind)})
        groups = runner.naming_groups()
        named = runner.named()
        if not groups and not named:
            return self.async_abort(reason="nothing_to_name")
        groups = groups[:NAMING_MAX_GROUPS]
        self._naming_groups = [g[0] for g in groups]
        if len(groups) == 1 and not named:
            # one page of one list is the list
            self._naming_group = groups[0][0]
            return await self.async_step_naming_list()
        placeholders, options = {"named": str(len(named))}, []
        for index, (where, shown, waiting) in enumerate(groups):
            placeholders[f"group_{index}"] = _group_title(where, self.hass)
            placeholders[f"group_{index}_detail"] = _group_detail(
                self.hass, runner, where, len(shown), waiting)
            options.append(f"group_{index}")
        if named:
            options.append("naming_named")
        options.append("naming_done")
        return self.async_show_menu(step_id="naming", menu_options=options,
                                    description_placeholders=placeholders)

    async def async_step_naming_named(self, user_input: dict[str, Any] | None = None):
        self._naming_group = NAMED
        return await self.async_step_naming_list()

    async def async_step_naming_list(self, user_input: dict[str, Any] | None = None):
        """One meter's loads, biggest first, one clickable row each - or,
        for NAMED, every named load wherever it was seen."""
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        if runner is None or not runner.enabled:
            return self.async_abort(reason="no_detection")
        where = self.__dict__.get("_naming_group")
        if where == NAMED:
            candidates, waiting = runner.named(), 0
        else:
            group = next((g for g in runner.naming_groups() if g[0] == where), None)
            if group is None:
                return await self.async_step_naming()
            _, candidates, waiting = group
        if not candidates:
            return await self.async_step_naming()
        shown = candidates[:NAMING_MAX_ROWS]
        self._naming_rows = [s.id for s in shown]

        tz = dt_util.DEFAULT_TIME_ZONE
        now_ts = dt_util.utcnow().timestamp()
        running = runner.detector.running_now(now_ts)
        # the other settings each load may be of the same device as
        partners: dict = {}
        for group in (suggest_levels(runner.detector.signatures, runner.detector.recent)
                      + input_groups(runner.detector.signatures)):
            for i in group:
                partners[i] = list(dict.fromkeys(partners.get(i, []) + [g for g in group if g != i]))
        by_id = {s.id: s for s in runner.detector.signatures}
        # What is WAITING, not what was cut off this menu. The two are not the
        # same: the list arrives already shortened to the length the user has
        # earned, so subtracting the menu from it said "0 more" to everyone -
        # and a page whose whole promise is that it lengthens as you name
        # things then read as "this is all there is" (Anze, 2026-09-22).
        placeholders = {"group": "Named loads" if where == NAMED else _group_title(where, self.hass),
                        "count": str(len(shown)),
                        "hidden": str(waiting + max(0, len(candidates) - len(shown)))}
        options = []
        for index, sig in enumerate(shown):
            # Two lines: a menu row's own label is cut at the dialog's width,
            # so it carries only what tells one load from another, and the rest
            # goes in the row's description, which wraps (Anze, 2026-09-23: the
            # page "does not fit all the text").
            label, rest = sig.menu_row(tz, now_ts, sig.id in running)
            if sig.name:
                label = f"{sig.name} — {label}"
            maybe = same_device_phrase([by_id[i] for i in partners.get(sig.id, []) if i in by_id])
            if maybe:
                rest += f" · {maybe}"
            placeholders[f"load_{index}"] = label
            placeholders[f"load_{index}_detail"] = rest
            options.append(f"load_{index}")
        if len(self.__dict__.get("_naming_groups") or []) > 1 or where == NAMED or runner.named():
            options.append("naming")              # back to the meters
        options.append("naming_done")
        return self.async_show_menu(step_id="naming_list", menu_options=options,
                                    description_placeholders=placeholders)

    def __getattr__(self, name: str):
        """Route the menu's rows, which are steps named after their position."""
        if name.startswith("async_step_load_") and name[16:].isdigit():
            index = int(name[16:])

            async def _chosen(user_input: dict[str, Any] | None = None):
                rows = self.__dict__.get("_naming_rows") or []
                if index >= len(rows):
                    return await self.async_step_naming_list()
                self._naming_selected = rows[index]
                return await self.async_step_naming_detail()

            return _chosen
        if name.startswith("async_step_group_") and name[17:].isdigit():
            index = int(name[17:])

            async def _opened(user_input: dict[str, Any] | None = None):
                groups = self.__dict__.get("_naming_groups") or []
                if index >= len(groups):
                    return await self.async_step_naming()
                self._naming_group = groups[index]
                return await self.async_step_naming_list()

            return _opened
        raise AttributeError(name)

    async def async_step_naming_done(self, user_input: dict[str, Any] | None = None):
        """Apply: the names were written as they were given, and this is what
        rebuilds the entities behind them."""
        rev = int(self.config_entry.options.get(CONF_SIGNATURE_REVISION, 0)) + 1
        options = {**dict(self.config_entry.options), CONF_SIGNATURE_REVISION: rev}
        # the inputs linked to a load follow its name, and the ones ticked
        # when it was named are linked to it now
        pending = self.__dict__.get("_pending_links") or {}
        for was, name in pending.get("relink") or []:
            options = relink(options, was, name)
        if pending.get("link"):
            targets = await self._link_targets()
            links = dict(options.get(CONF_INPUT_LINKS) or {})
            for eid, name in pending["link"]:
                links[eid] = list(dict.fromkeys(list(links.get(eid) or []) + [self._as_target(LOAD_PREFIX + name, targets)]))
            options[CONF_INPUT_LINKS] = links
        return self.async_create_entry(data=options)

    def _picked(self):
        """The runner, and the load chosen from the list - or None for either."""
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        if runner is None:
            return None, None
        sid = self.__dict__.get("_naming_selected")
        return runner, next((s for s in runner.detector.signatures if s.id == sid), None)

    async def async_step_naming_detail(self, user_input: dict[str, Any] | None = None):
        """The load that was picked, and what can be done with it.

        A MENU, not a form: a form's only control is Submit, so going back to
        the list used to mean submitting an empty name box. As a menu, back is
        a row like any other (Anze, 2026-09-23: "when i click on an entry i
        would like a back button"), and naming is one click further in."""
        runner, sig = self._picked()
        if runner is None:
            return self.async_abort(reason="no_detection")
        if sig is None:
            return await self.async_step_naming_list()
        detail = sig.detail(dt_util.DEFAULT_TIME_ZONE, runner.parents)
        helped = _helpers(self.hass, sig, runner.detector.edges)
        if helped:
            detail += "\n\n**Helped by**\n" + "\n".join(f"- {text}" for _, text in helped)
        if sig.name:
            detail = f"Named **{sig.name}**.\n\n{detail}"
        options = ["naming_name"]
        # A named load whose behaviour changed leaves its name on a
        # fingerprint nothing matches, while what replaced it sits here
        # unnamed. Offer the move where the user is already standing.
        was = runner.detector.predecessor_of(sig.id)
        if was is not None and not sig.name:
            options.append("naming_adopt")
            quiet = _since(was.last_seen)
            detail = (f"**{was.name}** has not run {quiet}, and this looks like what it became "
                      f"- it was {_fmt_w(was.watts)} over {_fmt_s(was.duration_s)}, this is "
                      f"{_fmt_w(sig.watts)} over {_fmt_s(sig.duration_s)}, on the same phases.\n\n"
                      f"{detail}")
        if sig.name:
            options.append("naming_forget")
        options.append("naming_list")             # back to the list
        return self.async_show_menu(
            step_id="naming_detail", menu_options=options,
            description_placeholders={"detail": detail, "name": sig.name or "",
                                      "was": was.name if was is not None else ""})

    async def async_step_naming_name(self, user_input: dict[str, Any] | None = None):
        """The name itself. An empty box changes nothing and goes back to the
        load, where Back is. Or the device it is, picked from the meters that
        hold one: the load is named exactly as that meter, which makes it
        that device (see insights.named) without a typo in the way (Anze,
        2026-09-29)."""
        runner, sig = self._picked()
        if runner is None or sig is None:
            return await self.async_step_naming_list()
        inputs = set(self.config_entry.options.get(CONF_INPUT_ENTITIES) or [])
        helped = [(eid, text) for eid, text in _helpers(self.hass, sig, runner.detector.edges) if eid in inputs]
        if user_input is not None:
            name = chosen_name(user_input.get("name"), user_input.get("same_as_meter"))
            if not name:
                return await self.async_step_naming_detail()
            was = sig.name
            await runner.async_rename(sig.id, name)
            # the options are written by Done, which rebuilds the entities too
            pending = self.__dict__.setdefault("_pending_links", {"relink": [], "link": []})
            pending["relink"].append((was, name))
            pending["link"] += [(eid, name) for eid in user_input.get("link_inputs") or []]
            self._naming_selected = None
            return await self.async_step_naming_list()
        head, rest = sig.menu_row(dt_util.DEFAULT_TIME_ZONE, dt_util.utcnow().timestamp())
        fields = {vol.Optional("name", description={"suggested_value": sig.name or ""}): selector.TextSelector()}
        meters = runner.device_meters()
        if meters:
            fields[vol.Optional("same_as_meter")] = selector.SelectSelector(selector.SelectSelectorConfig(
                options=meters, mode=selector.SelectSelectorMode.DROPDOWN))
        row = f"{head}\n\n{rest}"
        if helped:
            # offered, not ticked: linking changes the load's forecast
            fields[vol.Optional("link_inputs", default=[])] = selector.SelectSelector(selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(value=eid, label=self._input_label(eid))
                         for eid in dict.fromkeys(eid for eid, _ in helped)],
                multiple=True, mode=selector.SelectSelectorMode.LIST))
            row += "\n\n**Helped by**\n" + "\n".join(f"- {text}" for _, text in helped)
        return self.async_show_form(
            step_id="naming_name",
            data_schema=vol.Schema(fields),
            description_placeholders={"row": row},
            last_step=False,
        )

    async def async_step_naming_forget(self, user_input: dict[str, Any] | None = None):
        runner, sig = self._picked()
        if runner is not None and sig is not None:
            was = sig.name
            await runner.async_rename(sig.id, None)
            self.__dict__.setdefault("_pending_links", {"relink": [], "link": []})["relink"].append((was, None))
        self._naming_selected = None
        return await self.async_step_naming_list()

    async def async_step_naming_adopt(self, user_input: dict[str, Any] | None = None):
        # the name moves here and leaves the old fingerprint, which keeps its
        # history but stops answering to a name nothing matches any more
        runner, sig = self._picked()
        if runner is not None and sig is not None:
            await runner.async_adopt(sig.id)
        self._naming_selected = None
        return await self.async_step_naming_list()

    async def async_step_detection(self, user_input: dict[str, Any] | None = None):
        """How detection behaves - not what it watches.

        It used to ask for the meter as well, and that was asking for the
        answer: given the grid connection and the inverters, what the house
        draws is determined, not chosen (Anze, 2026-09-18). So the readings
        live on those two pages and this one keeps the two things that are
        genuinely preferences.

        A reading set here BEFORE that change still wins, for anyone who has
        one that already is the house - a dedicated CT, or a template built
        by hand. Nothing removes it; it simply is not offered any more.
        """
        current = dict(self.config_entry.options.get(CONF_DETECTION) or {})
        meters = await self._device_meters()
        if user_input is not None:
            # a wait of 0 is an answer - judge every step at once - not an empty field
            cfg = {**current, **{k: v for k, v in user_input.items()
                                 if (v or (k == CONF_METER_WAIT and v is not None)) and k != CONF_SINGLE_DEVICE}}
            if meters:
                # kept even when empty: none ticked is an answer - every meter holds several
                cfg[CONF_SINGLE_DEVICE] = list(user_input.get(CONF_SINGLE_DEVICE) or [])
            return self.async_create_entry(data={**dict(self.config_entry.options), CONF_DETECTION: cfg})
        fields = _interval_field(current)
        if meters:
            fields.update(_single_device_field(meters, current.get(CONF_SINGLE_DEVICE)))
        return self.async_show_form(
            step_id="detection",
            data_schema=vol.Schema(fields),
            description_placeholders={"found": _load_line(self.hass, self.config_entry)},
        )

    async def _device_meters(self) -> list:
        """(statistic id, name, guessed to hold one device) for every meter
        detection reads besides the house - the devices of the Energy
        dashboard it could resolve a power reading for."""
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        if runner is None or not runner.enabled:
            return []
        return [(m["energy"], name, runner.guess_one_device(name))
                for name, m in runner.submeters.items() if m.get("energy")]

    async def async_step_inputs(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            # An emptied selector clears the input; only set keys are kept.
            # Everything else is another page's and is carried over whole - it
            # kept only the device map and detection, so saving this page
            # erased the inverters and the naming revision (2026-09-28).
            owned = (CONF_WEATHER_ENTITY, CONF_OUTDOOR_TEMPERATURE_ENTITY, CONF_CALENDAR_ENTITIES, CONF_INPUT_ENTITIES)
            keep = {k: v for k, v in self.config_entry.options.items() if k not in owned}
            kept = set(user_input.get(CONF_INPUT_ENTITIES) or [])
            keep[CONF_INPUT_LINKS] = {k: v for k, v in (keep.get(CONF_INPUT_LINKS) or {}).items() if k in kept}
            return self.async_create_entry(data={**keep, **{k: v for k, v in user_input.items() if v and k in owned}})
        current = dict(self.config_entry.options)
        if not current.get(CONF_WEATHER_ENTITY):
            current[CONF_WEATHER_ENTITY] = _single_weather_entity(self.hass)
        return self.async_show_form(step_id="inputs", data_schema=vol.Schema(_inputs_schema(current)))

    async def _link_targets(self) -> dict:
        """What an input can be linked to: every device of the Energy
        dashboard, and every named load not on it (yet) - value -> label."""
        manager = await async_get_manager(self.hass)
        targets = {d.energy: d.label for d in SiteModel.from_prefs(manager.data).devices}
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        for name in sorted(runner.detector.names() if runner is not None else ()):
            if named_load_energy(self.hass, self.config_entry.entry_id, name) not in targets:
                targets[LOAD_PREFIX + name] = f"{name} (detected load)"
        return targets

    def _as_target(self, target: str, targets: dict) -> str:
        """A link to a named load that has since been put on the dashboard
        is a link to that device."""
        if target.startswith(LOAD_PREFIX):
            energy = named_load_energy(self.hass, self.config_entry.entry_id, target[len(LOAD_PREFIX):])
            if energy in targets:
                return energy
        return target

    def _input_label(self, eid: str) -> str:
        st = self.hass.states.get(eid)
        return (st.attributes.get("friendly_name") if st is not None else None) or eid

    async def async_step_input_links(self, user_input: dict[str, Any] | None = None):
        """Pick an input, then what it belongs to. Every input counts for the
        whole site; a link adds a device - and every device it sits inside -
        whose next hours a number then nudges (Anze, 2026-09-28)."""
        inputs = list(self.config_entry.options.get(CONF_INPUT_ENTITIES) or [])
        if not inputs:
            return self.async_abort(reason="no_inputs")
        if user_input is not None:
            self._linking = user_input["input"]
            return await self.async_step_input_links_to()
        targets = await self._link_targets()
        links = self.config_entry.options.get(CONF_INPUT_LINKS) or {}

        def label(eid):
            linked = [targets.get(self._as_target(t, targets)) for t in links.get(eid) or []]
            return f"{self._input_label(eid)}  ({', '.join(x for x in linked if x) or 'the site only'})"
        return self.async_show_form(
            step_id="input_links",
            data_schema=vol.Schema({vol.Required("input"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(value=e, label=label(e)) for e in inputs],
                mode=selector.SelectSelectorMode.DROPDOWN))}),
        )

    async def async_step_input_links_to(self, user_input: dict[str, Any] | None = None):
        eid = self.__dict__.get("_linking")
        targets = await self._link_targets()
        links = dict(self.config_entry.options.get(CONF_INPUT_LINKS) or {})
        if user_input is not None:
            chosen = list(user_input.get("linked_to") or [])
            if chosen:
                links[eid] = chosen
            else:
                links.pop(eid, None)
            return self.async_create_entry(data={**dict(self.config_entry.options), CONF_INPUT_LINKS: links})
        current = [t for t in (self._as_target(t, targets) for t in links.get(eid) or []) if t in targets]
        return self.async_show_form(
            step_id="input_links_to",
            data_schema=vol.Schema({vol.Optional("linked_to", default=current): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=k, label=v) for k, v in targets.items()],
                    multiple=True, mode=selector.SelectSelectorMode.LIST))}),
            description_placeholders={"input": self._input_label(eid)},
        )

    async def async_step_suggested_inputs(self, user_input: dict[str, Any] | None = None):
        """Sensors in the same area as a dashboard device or a named load that
        are not inputs yet, each offered linked to it - unticked, since a
        room's temperature need not say anything about its fridge
        (2026-09-29). A named load's area is its meter's; under none, it has
        none."""
        options = dict(self.config_entry.options)
        if user_input is not None:
            picks = [v.split(" ", 1) for v in user_input.get("suggested") or []]
            return self.async_create_entry(data=add_inputs(options, picks))
        targets = await self._link_targets()
        runner = self.hass.data.get(DOMAIN, {}).get(f"{self.config_entry.entry_id}_detection")
        meter = {}
        for sig in runner.named() if runner is not None else ():      # biggest first
            meter.setdefault(sig.name, most_specific(sig.locations, sig.count, runner.parents))
        places = {}
        for t in targets:
            if not t.startswith(LOAD_PREFIX):
                places[t] = _area_id(self.hass, t)
            elif meter.get(t[len(LOAD_PREFIX):], "main") != "main":
                places[t] = _area_id(self.hass, _meter_entity(runner, meter[t[len(LOAD_PREFIX):]]))
        # not a diagnostic one: a Shelly's own temperature is its relay's, not
        # the room's. And one from a device that meters power is that device's
        # own - a battery's cells, an inverter's heat sink, a car's cabin, a
        # boiler's tank: offered to that device only. A battery's cells were
        # offered as Kozolec's PV room temperature (2026-09-29).
        registry = er.async_get(self.hass)
        metering = {e.device_id for e in registry.entities.values() if e.device_id
                    and (e.device_class or e.original_device_class) in ("power", "energy")}
        chosen = [e for e in registry.entities.values()
                  if not e.disabled_by and not e.entity_category
                  and (e.device_class or e.original_device_class) in SUGGESTED_CLASSES.get(e.domain, ())]
        candidates = {e.entity_id: _area_id(self.hass, e.entity_id) for e in chosen}
        # a device by its name, so the two halves of one appliance meet: Home's
        # boiler is a plug metering it and a controller reading its tank, both
        # called Workshop Boiler (2026-09-29)
        devices = dr.async_get(self.hass)

        def appliance(device_id):
            device = devices.async_get(device_id) if device_id else None
            return ((device.name_by_user or device.name or device_id).strip().lower()) if device else None
        own = {e.entity_id: appliance(e.device_id) for e in chosen if e.device_id in metering}
        target_devices = {t: appliance(registry.async_get(t).device_id if registry.async_get(t) else None)
                          for t in places if not t.startswith(LOAD_PREFIX)}
        found = suggest_inputs(places, candidates, options.get(CONF_INPUT_ENTITIES), own, target_devices)
        if not found:
            return self.async_abort(reason="no_suggestions")
        areas = ar.async_get(self.hass)

        def row(eid, t):
            what = t[len(LOAD_PREFIX):] if t.startswith(LOAD_PREFIX) else targets[t]
            area = areas.async_get_area(places[t])
            return f"{self._input_label(eid)} → {what} ({area.name if area else places[t]})"
        rows = sorted((row(e, t), f"{e} {t}") for e, t in found)
        return self.async_show_form(
            step_id="suggested_inputs",
            data_schema=vol.Schema({vol.Optional("suggested", default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=v, label=label) for label, v in rows],
                    multiple=True, mode=selector.SelectSelectorMode.LIST))}),
        )
