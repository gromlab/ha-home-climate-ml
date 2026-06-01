"""Config flow for Home Climate ML."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import DOMAIN, LOGGER


class HomeClimateMlFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Home Climate ML."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial user step.

        This integration reads from the HA state machine directly — no external
        credentials or host configuration needed at setup time. Zone configuration
        is done via the Options flow after the entry is created.
        """
        if user_input is not None:
            # Prevent duplicate installs — only one instance supported
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title="Home Climate ML",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            description_placeholders={},
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> HomeClimateMlOptionsFlowHandler:
        """Return the options flow handler."""
        return HomeClimateMlOptionsFlowHandler(config_entry)


class HomeClimateMlOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow for Home Climate ML.

    Phase 1 will add per-zone configuration here:
    - Head sensor entity selection
    - External/reference sensor entity selection
    - Offset bounds (min/max °C)
    - Schedule entity linkage
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
        )
