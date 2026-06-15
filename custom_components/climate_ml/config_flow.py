"""Config flow for ClimateML."""
from __future__ import annotations

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
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)

from .const import (
    CONTROLLER_DEFAULTS,
    DEFAULT_SETPOINT_C,
    DOMAIN,
    ECO_TOLERANCE_DEFAULT,
    IDLE_HEAD_THRESHOLD_DEFAULT,
    VACATION_SETPOINT_DEFAULT,
)


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
        result.append({
            "start": _normalise_time(b.get("start", "00:00")),
            "end": _normalise_time(b.get("end", "00:00")),
            "day_type": str(b.get("day_type", "weekday")),
            "setpoint_c": float(b.get("setpoint_c", DEFAULT_SETPOINT_C)),
            "mode": str(b.get("mode", "eco")),
        })
    return result


def _blocks_for_form(blocks: list) -> list:
    """Convert stored blocks to ObjectSelector-compatible format (numeric → str for selects)."""
    return [
        {
            "start": b.get("start", "00:00"),
            "end": b.get("end", "00:00"),
            "day_type": b.get("day_type", "weekday"),
            "setpoint_c": float(b.get("setpoint_c", DEFAULT_SETPOINT_C)),
            "mode": b.get("mode", "eco"),
        }
        for b in blocks
        if isinstance(b, dict)
    ]


def _block_object_selector() -> ObjectSelector:
    return ObjectSelector({
        "multiple": True,
        "label_field": "start",
        "fields": {
            "day_type": {
                "selector": {"select": {"options": ["weekday", "weekend", "both"]}},
                "required": True,
                "label": "Day type",
            },
            "start": {"selector": {"time": {}}, "required": True, "label": "Start"},
            "end": {"selector": {"time": {}}, "required": True, "label": "End"},
            "setpoint_c": {
                "selector": {"number": {"min": 10.0, "max": 32.0, "step": 0.5, "mode": "box", "unit_of_measurement": "°C"}},
                "required": True,
                "label": "Setpoint (°C)",
            },
            "mode": {
                "selector": {"select": {"options": ["eco", "comfort", "off"]}},
                "required": True,
                "label": "Mode",
            },
        },
    })


def _zone_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required("name", default=d.get("name", "")): TextSelector(),
        vol.Required("head_entity", default=d.get("head_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="climate")),
        vol.Required("sensor_entity", default=d.get("sensor_entity", "")):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("default_setpoint_c", default=float(d.get("default_setpoint_c", DEFAULT_SETPOINT_C))):
            NumberSelector(NumberSelectorConfig(min=10.0, max=32.0, step=0.5, mode=NumberSelectorMode.BOX)),
        vol.Optional("default_mode", default=d.get("default_mode", "eco")):
            SelectSelector(SelectSelectorConfig(options=["eco", "comfort"])),
        vol.Optional("is_starvation_priority", default=d.get("is_starvation_priority", False)):
            BooleanSelector(),
        vol.Optional("eva_in_entity", default=d.get("eva_in_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("eva_out_entity", default=d.get("eva_out_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("demand_delta_entity", default=d.get("demand_delta_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("head_thermal_delta_entity", default=d.get("head_thermal_delta_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("occupancy_entity", default=d.get("occupancy_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        vol.Optional("humidity_entity", default=d.get("humidity_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional("door_entity", default=d.get("door_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        vol.Optional("window_entity", default=d.get("window_entity") or vol.UNDEFINED):
            EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        vol.Optional("blocks", default=d.get("blocks", [])): _block_object_selector(),
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
        vol.Optional("vacation_setpoint_c",
                     default=float(d.get("vacation_setpoint_c", VACATION_SETPOINT_DEFAULT))):
            NumberSelector(NumberSelectorConfig(min=18.0, max=28.0, step=0.5, mode=NumberSelectorMode.BOX)),
        vol.Optional("idle_head_threshold_minutes",
                     default=int(d.get("idle_head_threshold_minutes", IDLE_HEAD_THRESHOLD_DEFAULT))):
            NumberSelector(NumberSelectorConfig(min=15, max=180, step=5, mode=NumberSelectorMode.BOX)),
        vol.Optional("eco_tolerance_c",
                     default=float(d.get("eco_tolerance_c", ECO_TOLERANCE_DEFAULT))):
            NumberSelector(NumberSelectorConfig(min=0.0, max=2.0, step=0.1, mode=NumberSelectorMode.BOX)),
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
    VERSION = 7

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
                    data=_zone_data_from_input(user_input),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_zone_schema(),
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
                    data=_zone_data_from_input(user_input),
                )

        defaults = {
            "name": subentry.title,
            "head_entity": subentry.data.get("head_entity", ""),
            "sensor_entity": subentry.data.get("sensor_entity", ""),
            "default_setpoint_c": float(subentry.data.get("default_setpoint_c", DEFAULT_SETPOINT_C)),
            "default_mode": subentry.data.get("default_mode", "eco"),
            "is_starvation_priority": bool(subentry.data.get("is_starvation_priority", False)),
            "eva_in_entity": subentry.data.get("eva_in_entity", ""),
            "eva_out_entity": subentry.data.get("eva_out_entity", ""),
            "demand_delta_entity": subentry.data.get("demand_delta_entity", ""),
            "head_thermal_delta_entity": subentry.data.get("head_thermal_delta_entity", ""),
            "occupancy_entity": subentry.data.get("occupancy_entity", ""),
            "humidity_entity": subentry.data.get("humidity_entity", ""),
            "door_entity": subentry.data.get("door_entity", ""),
            "window_entity": subentry.data.get("window_entity", ""),
            "blocks": _blocks_for_form(subentry.data.get("schedule", {}).get("blocks", [])),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_zone_schema(defaults),
            errors=errors,
        )


def _zone_data_from_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Build zone subentry data dict from validated form input."""
    return {
        "head_entity": user_input.get("head_entity", ""),
        "sensor_entity": user_input.get("sensor_entity", ""),
        "default_setpoint_c": float(user_input.get("default_setpoint_c", DEFAULT_SETPOINT_C)),
        "default_mode": str(user_input.get("default_mode", "eco")),
        "is_starvation_priority": bool(user_input.get("is_starvation_priority", False)),
        "eva_in_entity": user_input.get("eva_in_entity", ""),
        "eva_out_entity": user_input.get("eva_out_entity", ""),
        "demand_delta_entity": user_input.get("demand_delta_entity", ""),
        "head_thermal_delta_entity": user_input.get("head_thermal_delta_entity", ""),
        "occupancy_entity": user_input.get("occupancy_entity", ""),
        "humidity_entity": user_input.get("humidity_entity", ""),
        "door_entity": user_input.get("door_entity", ""),
        "window_entity": user_input.get("window_entity", ""),
        "schedule": {
            "blocks": _normalise_blocks(user_input.get("blocks") or []),
        },
    }


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
            "vacation_setpoint_c": float(user_input.get("vacation_setpoint_c", VACATION_SETPOINT_DEFAULT)),
            "idle_head_threshold_minutes": int(user_input.get("idle_head_threshold_minutes", IDLE_HEAD_THRESHOLD_DEFAULT)),
            "eco_tolerance_c": float(user_input.get("eco_tolerance_c", ECO_TOLERANCE_DEFAULT)),
            "odu_mode_entity": user_input.get("odu_mode_entity", ""),
            "outdoor_temp_entity": user_input.get("outdoor_temp_entity", ""),
            "extra_temp_entities": user_input.get("extra_temp_entities") or [],
            "power_entity": user_input.get("power_entity", ""),
            "energy_entity": user_input.get("energy_entity", ""),
            "weather_entity": user_input.get("weather_entity", ""),
        }
