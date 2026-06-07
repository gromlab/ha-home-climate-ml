"""Config flow for ClimateML."""
from __future__ import annotations

import copy
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
    TimeSelectorConfig,
)

from .const import DEFAULT_OPTIONS, DOMAIN
from .schedule import validate_block


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
    if s and re.match(r"^\d{1,2}:\d{2}:\d{2}$", s.strip()):
        return s.strip()[:5]
    return s


class ClimateMLConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input: dict | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="ClimateML", data={}, options=copy.deepcopy(DEFAULT_OPTIONS))
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ClimateMLOptionsFlow:
        return ClimateMLOptionsFlow()


class ClimateMLOptionsFlow(OptionsFlow):

    def __init__(self) -> None:
        self._selected_zone_id: str | None = None
        self._selected_day_type: str | None = None
        self._selected_block_idx: int | None = None
        self._pending_block_action: str | None = None  # "edit" | "delete"

    def _options(self) -> dict:
        return dict(self.config_entry.options)

    def _zones(self) -> list[dict]:
        return list(self._options().get("zones", []))

    def _zone(self) -> dict | None:
        for z in self._zones():
            if z["id"] == self._selected_zone_id:
                return z
        return None

    def _blocks(self) -> list[dict]:
        z = self._zone()
        if not z:
            return []
        return z.get("schedule", {}).get(self._selected_day_type or "weekday", [])

    # ------------------------------------------------------------------ top menu

    async def async_step_init(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["global_settings", "add_zone", "manage_zones"],
        )

    # ------------------------------------------------------------------ global settings

    async def async_step_global_settings(self, user_input: dict | None = None):
        opts = self._options()
        errors: dict[str, str] = {}

        if user_input is not None:
            new_opts = {**opts, **user_input}
            return self.async_create_entry(title="", data=new_opts)

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

        return self.async_show_form(
            step_id="global_settings", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------ add zone

    async def async_step_add_zone(self, user_input: dict | None = None):
        opts = self._options()
        zones = self._zones()
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input["name"].strip()
            if not name:
                errors["name"] = "name_required"
            else:
                existing_ids = [z["id"] for z in zones]
                zone_id = _unique_slug(_slugify(name), existing_ids)
                new_zone = {
                    "id": zone_id,
                    "name": name,
                    "head_entity": user_input["head_entity"],
                    "sensor_entity": user_input["sensor_entity"],
                    "schedule": {"weekday": [], "weekend": []},
                }
                new_opts = {**opts, "zones": zones + [new_zone]}
                return self.async_create_entry(title="", data=new_opts)

        schema = vol.Schema({
            vol.Required("name"): TextSelector(),
            vol.Required("head_entity"): EntitySelector(EntitySelectorConfig(domain="climate")),
            vol.Required("sensor_entity"): EntitySelector(EntitySelectorConfig(domain="sensor")),
        })
        return self.async_show_form(step_id="add_zone", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------ manage zones

    async def async_step_manage_zones(self, user_input: dict | None = None):
        zones = self._zones()
        if not zones:
            return self.async_abort(reason="no_zones")

        if user_input is not None:
            self._selected_zone_id = user_input["zone_id"]
            return await self.async_step_zone_menu()

        zone_options = {z["id"]: z["name"] for z in zones}
        schema = vol.Schema({
            vol.Required("zone_id"): SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": k, "label": v} for k, v in zone_options.items()],
                    mode=SelectSelectorMode.LIST,
                )
            )
        })
        return self.async_show_form(step_id="manage_zones", data_schema=schema)

    # ------------------------------------------------------------------ zone menu

    async def async_step_zone_menu(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="zone_menu",
            menu_options=["edit_zone", "manage_schedule", "delete_zone"],
        )

    # ------------------------------------------------------------------ edit zone

    async def async_step_edit_zone(self, user_input: dict | None = None):
        opts = self._options()
        zones = self._zones()
        zone = self._zone()
        errors: dict[str, str] = {}

        if zone is None:
            return self.async_abort(reason="zone_not_found")

        if user_input is not None:
            name = user_input["name"].strip()
            if not name:
                errors["name"] = "name_required"
            else:
                updated = [
                    {**z, "name": name, "head_entity": user_input["head_entity"],
                     "sensor_entity": user_input["sensor_entity"]}
                    if z["id"] == self._selected_zone_id else z
                    for z in zones
                ]
                return self.async_create_entry(title="", data={**opts, "zones": updated})

        schema = vol.Schema({
            vol.Required("name", default=zone["name"]): TextSelector(),
            vol.Required("head_entity", default=zone["head_entity"]):
                EntitySelector(EntitySelectorConfig(domain="climate")),
            vol.Required("sensor_entity", default=zone["sensor_entity"]):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
        })
        return self.async_show_form(step_id="edit_zone", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------ delete zone

    async def async_step_delete_zone(self, user_input: dict | None = None):
        opts = self._options()
        zones = self._zones()
        zone = self._zone()

        if zone is None:
            return self.async_abort(reason="zone_not_found")

        if user_input is not None:
            if user_input.get("confirm"):
                updated = [z for z in zones if z["id"] != self._selected_zone_id]
                return self.async_create_entry(title="", data={**opts, "zones": updated})
            return await self.async_step_zone_menu()

        schema = vol.Schema({
            vol.Required("confirm", default=False): selector.BooleanSelector(),
        })
        return self.async_show_form(
            step_id="delete_zone",
            data_schema=schema,
            description_placeholders={"zone_name": zone["name"]},
        )

    # ------------------------------------------------------------------ manage schedule

    async def async_step_manage_schedule(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="manage_schedule",
            menu_options=["schedule_weekday", "schedule_weekend"],
        )

    async def async_step_schedule_weekday(self, user_input: dict | None = None):
        self._selected_day_type = "weekday"
        return await self.async_step_schedule_blocks()

    async def async_step_schedule_weekend(self, user_input: dict | None = None):
        self._selected_day_type = "weekend"
        return await self.async_step_schedule_blocks()

    # ------------------------------------------------------------------ schedule blocks menu

    async def async_step_schedule_blocks(self, user_input: dict | None = None):
        blocks = self._blocks()
        sp_min = self._options().get("setpoint_min_c", 16.0)
        sp_max = self._options().get("setpoint_max_c", 28.0)

        menu_options: dict[str, str] = {"add_block": "➕ Add block"}
        for i, b in enumerate(blocks):
            label = f"{b['start']}–{b['end']} ({b['mode']}" + (f" {b['setpoint_c']}°C" if b.get("setpoint_c") else "") + ")"
            menu_options[f"edit_block_{i}"] = f"✏️ Edit: {label}"
            menu_options[f"delete_block_{i}"] = f"🗑️ Delete: {label}"

        if user_input is not None:
            choice = user_input.get("choice")
            if choice == "add_block":
                return await self.async_step_add_block()
            if choice and choice.startswith("edit_block_"):
                self._selected_block_idx = int(choice.split("_")[-1])
                self._pending_block_action = "edit"
                return await self.async_step_edit_block()
            if choice and choice.startswith("delete_block_"):
                idx = int(choice.split("_")[-1])
                return await self._delete_block(idx)

        schema = vol.Schema({
            vol.Required("choice"): SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": k, "label": v} for k, v in menu_options.items()],
                    mode=SelectSelectorMode.LIST,
                )
            )
        })
        return self.async_show_form(step_id="schedule_blocks", data_schema=schema)

    async def _delete_block(self, idx: int):
        opts = self._options()
        zones = self._zones()
        zone = self._zone()
        if zone is None:
            return self.async_abort(reason="zone_not_found")
        blocks = list(zone.get("schedule", {}).get(self._selected_day_type, []))
        if 0 <= idx < len(blocks):
            blocks.pop(idx)
        updated_zones = [
            {**z, "schedule": {**z.get("schedule", {}), self._selected_day_type: blocks}}
            if z["id"] == self._selected_zone_id else z
            for z in zones
        ]
        return self.async_create_entry(title="", data={**opts, "zones": updated_zones})

    # ------------------------------------------------------------------ add block

    async def async_step_add_block(self, user_input: dict | None = None):
        opts = self._options()
        sp_min = opts.get("setpoint_min_c", 16.0)
        sp_max = opts.get("setpoint_max_c", 28.0)
        errors: dict[str, str] = {}

        if user_input is not None:
            block = {
                "start": _normalise_time(user_input["start"]),
                "end": _normalise_time(user_input["end"]),
                "mode": user_input["mode"],
            }
            if user_input.get("setpoint_c") is not None:
                block["setpoint_c"] = float(user_input["setpoint_c"])
            err = validate_block(block, sp_min, sp_max)
            if err:
                errors["base"] = "invalid_block"
            else:
                zones = self._zones()
                zone = self._zone()
                if zone is None:
                    return self.async_abort(reason="zone_not_found")
                blocks = list(zone.get("schedule", {}).get(self._selected_day_type, []))
                blocks.append(block)
                blocks.sort(key=lambda b: b["start"])
                updated_zones = [
                    {**z, "schedule": {**z.get("schedule", {}), self._selected_day_type: blocks}}
                    if z["id"] == self._selected_zone_id else z
                    for z in zones
                ]
                return self.async_create_entry(title="", data={**opts, "zones": updated_zones})

        schema = self._block_schema(sp_min, sp_max)
        return self.async_show_form(step_id="add_block", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------ edit block

    async def async_step_edit_block(self, user_input: dict | None = None):
        opts = self._options()
        sp_min = opts.get("setpoint_min_c", 16.0)
        sp_max = opts.get("setpoint_max_c", 28.0)
        errors: dict[str, str] = {}
        idx = self._selected_block_idx or 0
        blocks = self._blocks()

        if idx >= len(blocks):
            return self.async_abort(reason="block_not_found")

        existing = blocks[idx]

        if user_input is not None:
            block = {
                "start": _normalise_time(user_input["start"]),
                "end": _normalise_time(user_input["end"]),
                "mode": user_input["mode"],
            }
            if user_input.get("setpoint_c") is not None:
                block["setpoint_c"] = float(user_input["setpoint_c"])
            err = validate_block(block, sp_min, sp_max)
            if err:
                errors["base"] = "invalid_block"
            else:
                zones = self._zones()
                zone = self._zone()
                if zone is None:
                    return self.async_abort(reason="zone_not_found")
                all_blocks = list(zone.get("schedule", {}).get(self._selected_day_type, []))
                all_blocks[idx] = block
                all_blocks.sort(key=lambda b: b["start"])
                updated_zones = [
                    {**z, "schedule": {**z.get("schedule", {}), self._selected_day_type: all_blocks}}
                    if z["id"] == self._selected_zone_id else z
                    for z in zones
                ]
                return self.async_create_entry(title="", data={**opts, "zones": updated_zones})

        schema = self._block_schema(sp_min, sp_max, existing)
        return self.async_show_form(step_id="edit_block", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------ helpers

    def _block_schema(self, sp_min: float, sp_max: float, existing: dict | None = None) -> vol.Schema:
        defaults = existing or {}
        return vol.Schema({
            vol.Required("start", default=defaults.get("start", "00:00")):
                TimeSelector(TimeSelectorConfig()),
            vol.Required("end", default=defaults.get("end", "08:00")):
                TimeSelector(TimeSelectorConfig()),
            vol.Required("mode", default=defaults.get("mode", "cool")):
                SelectSelector(SelectSelectorConfig(
                    options=["off", "cool"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="schedule_mode",
                )),
            vol.Optional("setpoint_c", default=defaults.get("setpoint_c")):
                NumberSelector(NumberSelectorConfig(
                    min=sp_min, max=sp_max, step=0.5, mode=NumberSelectorMode.BOX,
                    unit_of_measurement="°C",
                )),
        })
