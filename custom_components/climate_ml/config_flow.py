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
    OptionsFlow,
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

from .const import DEFAULT_OPTIONS, DOMAIN


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
    """Strip seconds from HH:MM:SS → HH:MM."""
    s = str(s).strip()
    if re.match(r"^\d{1,2}:\d{2}:\d{2}$", s):
        return s[:5]
    return s


def _normalise_blocks(blocks: list) -> list:
    """Coerce types and normalise time strings in schedule blocks from ObjectSelector."""
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
        vol.Optional("weekday_blocks", default=d.get("weekday_blocks", [])): block_sel,
        vol.Optional("weekend_blocks", default=d.get("weekend_blocks", [])): block_sel,
    })


# ---------------------------------------------------------------------------
# Config flow — creates the integration entry
# ---------------------------------------------------------------------------

class ClimateMLConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 3

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"zone": ZoneSubentryFlowHandler}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title="ClimateML",
                data={},
                options=copy.deepcopy(DEFAULT_OPTIONS),
            )
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ClimateMLOptionsFlow:
        return ClimateMLOptionsFlow()


# ---------------------------------------------------------------------------
# Zone subentry flow — add and reconfigure zones
# ---------------------------------------------------------------------------

class ZoneSubentryFlowHandler(ConfigSubentryFlow):
    """Each zone is a subentry: name, entities, weekday/weekend schedule blocks."""

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
                        "schedule": {
                            "weekday": _normalise_blocks(user_input.get("weekday_blocks") or []),
                            "weekend": _normalise_blocks(user_input.get("weekend_blocks") or []),
                        },
                    },
                )

        entry = self._get_entry()
        sp_min = entry.options.get("setpoint_min_c", 16.0)
        sp_max = entry.options.get("setpoint_max_c", 28.0)
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
                        "schedule": {
                            "weekday": _normalise_blocks(user_input.get("weekday_blocks") or []),
                            "weekend": _normalise_blocks(user_input.get("weekend_blocks") or []),
                        },
                    },
                )

        entry = self._get_entry()
        sp_min = entry.options.get("setpoint_min_c", 16.0)
        sp_max = entry.options.get("setpoint_max_c", 28.0)
        defaults = {
            "name": subentry.title,
            "head_entity": subentry.data.get("head_entity", ""),
            "sensor_entity": subentry.data.get("sensor_entity", ""),
            "weekday_blocks": list(subentry.data.get("schedule", {}).get("weekday", [])),
            "weekend_blocks": list(subentry.data.get("schedule", {}).get("weekend", [])),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_zone_schema(defaults, sp_min, sp_max),
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Options flow — global settings only (zones managed via subentries)
# ---------------------------------------------------------------------------

class ClimateMLOptionsFlow(OptionsFlow):

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = dict(self.config_entry.options)

        if user_input is not None:
            return self.async_create_entry(title="", data={**opts, **user_input})

        sp_min = opts.get("setpoint_min_c", 16.0)
        sp_max = opts.get("setpoint_max_c", 28.0)

        schema = vol.Schema({
            vol.Optional("update_interval_minutes", default=opts.get("update_interval_minutes", 5)):
                NumberSelector(NumberSelectorConfig(min=1, max=60, step=1, mode=NumberSelectorMode.BOX)),
            vol.Optional("override_duration_minutes", default=opts.get("override_duration_minutes", 120)):
                NumberSelector(NumberSelectorConfig(min=15, max=480, step=15, mode=NumberSelectorMode.BOX)),
            vol.Optional("setpoint_tolerance_c", default=opts.get("setpoint_tolerance_c", 0.5)):
                NumberSelector(NumberSelectorConfig(min=0.1, max=2.0, step=0.1, mode=NumberSelectorMode.BOX)),
            vol.Optional("offset_clamp_c", default=opts.get("offset_clamp_c", 5.0)):
                NumberSelector(NumberSelectorConfig(min=1.0, max=10.0, step=0.5, mode=NumberSelectorMode.BOX)),
            vol.Optional("setpoint_min_c", default=sp_min):
                NumberSelector(NumberSelectorConfig(min=10.0, max=20.0, step=0.5, mode=NumberSelectorMode.BOX)),
            vol.Optional("setpoint_max_c", default=sp_max):
                NumberSelector(NumberSelectorConfig(min=22.0, max=32.0, step=0.5, mode=NumberSelectorMode.BOX)),
            vol.Optional("default_setpoint_c", default=opts.get("default_setpoint_c", 21.0)):
                NumberSelector(NumberSelectorConfig(min=sp_min, max=sp_max, step=0.5, mode=NumberSelectorMode.BOX)),
            vol.Optional("force_cool_threshold_c", default=opts.get("force_cool_threshold_c", 30.0)):
                NumberSelector(NumberSelectorConfig(min=25.0, max=40.0, step=0.5, mode=NumberSelectorMode.BOX)),
            vol.Optional("force_cool_clear_c", default=opts.get("force_cool_clear_c", 28.0)):
                NumberSelector(NumberSelectorConfig(min=20.0, max=38.0, step=0.5, mode=NumberSelectorMode.BOX)),
            vol.Optional("hallway_sensor", default=opts.get("hallway_sensor", "")):
                EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
            vol.Optional("outdoor_sensor", default=opts.get("outdoor_sensor", "")):
                EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
        })

        return self.async_show_form(step_id="init", data_schema=schema)
