"""Config flow for ClimateML."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import CONF_SCHEDULE_PATH, DEFAULT_SCHEDULE_PATH, DOMAIN, LOGGER
from .schedule import ScheduleError, load_schedule


class HomeClimateMlFlowHandler(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="ClimateML", data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> HomeClimateMlOptionsFlowHandler:
        return HomeClimateMlOptionsFlowHandler()


class HomeClimateMlOptionsFlowHandler(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        current_path = self.config_entry.options.get(
            CONF_SCHEDULE_PATH,
            self.hass.config.path("climate_schedules.yaml"),
        )

        if user_input is not None:
            path = user_input[CONF_SCHEDULE_PATH]
            try:
                await self.hass.async_add_executor_job(load_schedule, path)
            except FileNotFoundError:
                errors[CONF_SCHEDULE_PATH] = "schedule_not_found"
            except ScheduleError as exc:
                LOGGER.error("Schedule validation error: %s", exc)
                errors[CONF_SCHEDULE_PATH] = "schedule_invalid"
            except Exception:  # noqa: BLE001
                errors[CONF_SCHEDULE_PATH] = "unknown"

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SCHEDULE_PATH, default=current_path): str,
            }),
            errors=errors,
        )
