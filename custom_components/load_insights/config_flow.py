"""One step: confirm what the Energy dashboard already holds, name the site."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.energy.data import async_get_manager

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN
from .insights.model import SiteModel


class LoadInsightsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        manager = await async_get_manager(self.hass)
        site = SiteModel.from_prefs(manager.data)
        if not site.has_sources:
            return self.async_abort(reason="no_energy_dashboard")

        if user_input is not None:
            return self.async_create_entry(title=user_input[CONF_NAME], data={CONF_NAME: user_input[CONF_NAME]})

        s = site.summary()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default=DEFAULT_NAME): str}),
            description_placeholders={
                "grid": str(s["grid"]), "solar": str(s["solar"]), "battery": str(s["battery"]),
                "devices": str(s["devices"]), "forecasts": str(s["forecasts"]),
            },
        )
