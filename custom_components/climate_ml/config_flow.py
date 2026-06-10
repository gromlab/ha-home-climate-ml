"""Config flow for ClimateML."""
from __future__ import annotations

import copy
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    TextSelector,
)

from .const import CONTROLLER_DEFAULTS, DEFAULT_OPTIONS, DOMAIN


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "zone"


def _unique_slug(slug: str, existing_ids: list[str]) -> str:
    if slug not in existing_ids:
        return slug
    i = 2
    while f"{slug}_{i}" in existing_ids:
        i += 1
    return f"{slug}_{i}"


def _normalise_time(s: str) -> str:
    s = str(s).strip()
    if re.match(r"^\d{1,2}:\d{2}:\d{2}$", s):
        return s[:5]
    return s


def _normalise_blocks(blocks: list) -> list:
    result = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        block = {
            "start": _normalise_time(b.get("start", "00:00")),
            "end": _normalise_time(b.get("end", "00:00")),
            "mode": str(b.get("mode", "off")),
        }
        if b.get("setpoint_c") is not None:
            block["setpoint_c"] = float(b["setpoint_c"])
        result.append(block)
    return result


def _block_object_selector(sp_min: float = 16.0, sp_max: float = 28.0) -> ObjectSelector:
    return ObjectSelector({
        "multiple": True,
        "label_field": "start",
        "fields": {
            "start": {"selector": {"time": {}}, "required": True, "label": "Start"},
            "end": {"selector": {"time": {}}, "required": True, "label": "End"},
            "mode": {
                "selector": {"select": {"options": ["off", "cool"]}},
                "required": True,
                "label": "Mode",
            },
            "setpoint_c": {
                "selector": {
                    "number": {
                        "min": sp_min,
                        "max": sp_max,
                        "step": 0.5,
                        "mode": "box",
                        "unit_of_measurement": "°C",
                    }
                },
                "required": False,
                "label": "Setpoint (°C)",
            },
        },
    })


def _zone_schema(
    defaults: dict | None = None,
    sp_min: float = 16.0,
    sp_max: float = 28.0,
) -> vol.Schema:
    d = defaults or {}
    block_sel = _block_object_selector(sp_min, sp_max)
    return vol.Schema({
        vol.Required("name", default=d.get("name", "")): TextSelector(),
        vol.Required("head_entity", default=d.get("head_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="climate")),
        vol.Required("sensor_entity", default=d.get("sensor_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("eva_in_entity", default=d.get("eva_in_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("eva_out_entity", default=d.get("eva_out_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("occupancy_entity", default=d.get("occupancy_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        vol.Optional("weekday_blocks", default=d.get("weekday_blocks", [])): block_sel,
        vol.Optional("weekend_blocks", default=d.get("weekend_blocks", [])): block_sel,
    })


def _controller_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    sp_min = d.get("setpoint_min_c", CONTROLLER_DEFAULTS["setpoint_min_c"])
    sp_max = d.get("setpoint_max_c", CONTROLLER_DEFAULTS["setpoint_max_c"])
    return vol.Schema({
        vol.Optional("update_interval_minutes",
                     default=d.get("update_interval_minutes", CONTROLLER_DEFAULTS["update_interval_minutes"])):
            NumberSelector(NumberSelectorConfig(min=1, max=60, step=1, mode=NumberSelectorMode.BOX)),
        vol.Optional("setpoint_min_c", default=sp_min):
            NumberSelector(NumberSelectorConfig(min=10.0, max=20.0, step=0.5, mode=NumberSelectorMode.BOX)),
        vol.Optional("setpoint_max_c", default=sp_max):
            NumberSelector(NumberSelectorConfig(min=22.0, max=32.0, step=0.5, mode=NumberSelectorMode.BOX)),
        vol.Optional("odu_mode_entity", default=d.get("odu_mode_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("outdoor_temp_entity", default=d.get("outdoor_temp_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("extra_temp_entities", default=d.get("extra_temp_entities") or []):
            EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
        vol.Optional("power_entity", default=d.get("power_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("energy_entity", default=d.get("energy_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("weather_entity", default=d.get("weather_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="weather")),
    })


# ---------------------------------------------------------------------------
# Config flow — creates the integration entry
# ---------------------------------------------------------------------------

class ClimateMLConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 5

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {
            "zone": ZoneSubentryFlowHandler,
            "controller": ControllerSubentryFlowHandler,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title="ClimateML",
                data={},
                options={},
            )
        return self.async_show_form(step_id="user")


# ---------------------------------------------------------------------------
# Zone subentry flow
# ---------------------------------------------------------------------------

class ZoneSubentryFlowHandler(ConfigSubentryFlow):

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input.get("name", "").strip()
            if not name:
                errors["name"] = "name_required"
            else:
                entry = self._get_entry()
                existing_ids = [
                    s.unique_id for s in entry.subentries.values()
                    if s.subentry_type == "zone" and s.unique_id
                ]
                zone_id = _unique_slug(_slugify(name), existing_ids)
                return self.async_create_entry(
                    title=name,
                    unique_id=zone_id,
                    data={
                        "head_entity": user_input.get("head_entity", ""),
                        "sensor_entity": user_input.get("sensor_entity", ""),
                        "eva_in_entity": user_input.get("eva_in_entity", ""),
                        "eva_out_entity": user_input.get("eva_out_entity", ""),
                        "occupancy_entity": user_input.get("occupancy_entity", ""),
                        "schedule": {
                            "weekday": _normalise_blocks(user_input.get("weekday_blocks") or []),
                            "weekend": _normalise_blocks(user_input.get("weekend_blocks") or []),
                        },
                    },
                )

        entry = self._get_entry()
        controller = next(
            (s for s in entry.subentries.values() if s.subentry_type == "controller"), None
        )
        sp_min = (controller.data.get("setpoint_min_c") if controller else None) or CONTROLLER_DEFAULTS["setpoint_min_c"]
        sp_max = (controller.data.get("setpoint_max_c") if controller else None) or CONTROLLER_DEFAULTS["setpoint_max_c"]
        return self.async_show_form(
            step_id="user",
            data_schema=_zone_schema(sp_min=sp_min, sp_max=sp_max),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input.get("name", "").strip()
            if not name:
                errors["name"] = "name_required"
            else:
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=name,
                    data={
                        "head_entity": user_input.get("head_entity", ""),
                        "sensor_entity": user_input.get("sensor_entity", ""),
                        "eva_in_entity": user_input.get("eva_in_entity", ""),
                        "eva_out_entity": user_input.get("eva_out_entity", ""),
                        "occupancy_entity": user_input.get("occupancy_entity", ""),
                        "schedule": {
                            "weekday": _normalise_blocks(user_input.get("weekday_blocks") or []),
                            "weekend": _normalise_blocks(user_input.get("weekend_blocks") or []),
                        },
                    },
                )

        entry = self._get_entry()
        controller = next(
            (s for s in entry.subentries.values() if s.subentry_type == "controller"), None
        )
        sp_min = (controller.data.get("setpoint_min_c") if controller else None) or CONTROLLER_DEFAULTS["setpoint_min_c"]
        sp_max = (controller.data.get("setpoint_max_c") if controller else None) or CONTROLLER_DEFAULTS["setpoint_max_c"]
        defaults = {
            "name": subentry.title,
            "head_entity": subentry.data.get("head_entity", ""),
            "sensor_entity": subentry.data.get("sensor_entity", ""),
            "eva_in_entity": subentry.data.get("eva_in_entity", ""),
            "eva_out_entity": subentry.data.get("eva_out_entity", ""),
            "occupancy_entity": subentry.data.get("occupancy_entity", ""),
            "weekday_blocks": list(subentry.data.get("schedule", {}).get("weekday", [])),
            "weekend_blocks": list(subentry.data.get("schedule", {}).get("weekend", [])),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_zone_schema(defaults, sp_min, sp_max),
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Controller subentry flow
# ---------------------------------------------------------------------------

class ControllerSubentryFlowHandler(ConfigSubentryFlow):

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        if any(s.subentry_type == "controller" for s in entry.subentries.values()):
            return self.async_abort(reason="controller_already_configured")
        if user_input is not None:
            return self.async_create_entry(
                title="ClimateML Controller",
                unique_id="controller",
                data=self._clean_data(user_input),
            )
        return self.async_show_form(step_id="user", data_schema=_controller_schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title="ClimateML Controller",
                data=self._clean_data(user_input),
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_controller_schema(dict(subentry.data)),
        )

    @staticmethod
    def _clean_data(user_input: dict[str, Any]) -> dict[str, Any]:
        return {
            "update_interval_minutes": int(user_input.get("update_interval_minutes", CONTROLLER_DEFAULTS["update_interval_minutes"])),
            "setpoint_min_c": float(user_input.get("setpoint_min_c", CONTROLLER_DEFAULTS["setpoint_min_c"])),
            "setpoint_max_c": float(user_input.get("setpoint_max_c", CONTROLLER_DEFAULTS["setpoint_max_c"])),
            "odu_mode_entity": user_input.get("odu_mode_entity", ""),
            "outdoor_temp_entity": user_input.get("outdoor_temp_entity", ""),
            "extra_temp_entities": user_input.get("extra_temp_entities") or [],
            "power_entity": user_input.get("power_entity", ""),
            "energy_entity": user_input.get("energy_entity", ""),
            "weather_entity": user_input.get("weather_entity", ""),
        }
