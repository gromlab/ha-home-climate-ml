"""ClimateML — per-zone heat pump offset correction and scheduling."""
from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers.event import async_track_time_change

from .const import DEFAULT_OPTIONS, DOMAIN, LOGGER
from .coordinator import HomeClimateMlCoordinator
from .store import ClimateDataStore

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH]

type ClimateMLConfigEntry = ConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateMLConfigEntry,
) -> bool:
    options = entry.options
    zones = options.get("zones", [])
    sense_only = {
        k: v for k, v in {
            "hallway": options.get("hallway_sensor", ""),
            "outdoor": options.get("outdoor_sensor", ""),
        }.items() if v
    }
    params = {
        k: options.get(k, DEFAULT_OPTIONS[k])
        for k in (
            "update_interval_minutes", "override_duration_minutes", "setpoint_tolerance_c",
            "offset_clamp_c", "setpoint_min_c", "setpoint_max_c", "default_setpoint_c",
            "force_cool_threshold_c", "force_cool_clear_c",
        )
    }

    db_path = hass.config.path("climate_ml", "decisions.db")
    store = await hass.async_add_executor_job(ClimateDataStore, db_path)

    coordinator = HomeClimateMlCoordinator(
        hass=hass,
        entry_id=entry.entry_id,
        store=store,
        zones=zones,
        sense_only=sense_only,
        update_interval=timedelta(minutes=params["update_interval_minutes"]),
        params=params,
    )

    entry.runtime_data = coordinator

    await coordinator.async_config_entry_first_refresh()
    await coordinator.restore_overrides()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    async def _prune(_now):
        await hass.async_add_executor_job(store.prune)

    entry.async_on_unload(
        async_track_time_change(hass, _prune, hour=3, minute=0, second=0)
    )

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ClimateMLConfigEntry,
) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: ClimateMLConfigEntry,
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ClimateMLConfigEntry,
) -> bool:
    """Migrate entry from v1 (YAML schedule) to v2 (options-driven)."""
    LOGGER.info("Migrating ClimateML entry from version %s", entry.version)

    if entry.version == 1:
        new_options = copy.deepcopy(DEFAULT_OPTIONS)
        old = dict(entry.options)
        old.pop("schedule_yaml_path", None)
        new_options.update({k: v for k, v in old.items() if k in new_options})
        hass.config_entries.async_update_entry(entry, options=new_options, version=2)
        LOGGER.info("Migration to v2 complete")

    return True
