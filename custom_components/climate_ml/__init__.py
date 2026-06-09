"""ClimateML — per-zone heat pump offset correction and scheduling."""
from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr
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

    # Zones come from subentries; subentry unique_id is the stable zone slug.
    # Build a parallel zone_id → subentry_id map for device registry linkage.
    zone_subentry_map: dict[str, str] = {}
    zones = []
    for s in entry.subentries.values():
        if s.subentry_type != "zone":
            continue
        zone_id = s.unique_id or s.subentry_id
        zones.append({
            "id": zone_id,
            "name": s.title,
            "head_entity": s.data.get("head_entity", ""),
            "sensor_entity": s.data.get("sensor_entity", ""),
            "schedule": s.data.get("schedule", {"weekday": [], "weekend": []}),
        })
        zone_subentry_map[zone_id] = s.subentry_id

    # Sync device registry: link each zone device to its subentry and remove
    # devices whose subentry was deleted.
    dreg = dr.async_get(hass)
    current_zone_ids = {z["id"] for z in zones}

    for device in dr.async_entries_for_config_entry(dreg, entry.entry_id):
        for ident_domain, zone_id in device.identifiers:
            if ident_domain == DOMAIN and zone_id not in current_zone_ids:
                dreg.async_remove_device(device.id)
                break

    for zone in zones:
        dreg.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=zone_subentry_map.get(zone["id"]),
            identifiers={(DOMAIN, zone["id"])},
            name=f"ClimateML — {zone['name']}",
            manufacturer="ClimateML",
            model="Zone Controller",
        )

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

    # Entity platforms add a bare (no-subentry) association to zone devices.
    # Remove that so each device appears only under its own subentry, not also
    # under "Devices that don't belong to a sub-entry".
    for zone in zones:
        device = dreg.async_get_device(identifiers={(DOMAIN, zone["id"])})
        if device:
            dreg.async_update_device(device.id, remove_config_subentry_id=None)

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
    """Migrate entry versions."""
    from types import MappingProxyType
    from homeassistant.config_entries import ConfigSubentry

    LOGGER.info("Migrating ClimateML entry from version %s", entry.version)

    if entry.version == 1:
        # v1 (YAML schedule) → v3 (subentry zones): carry over global options, drop path.
        new_options = copy.deepcopy(DEFAULT_OPTIONS)
        old = dict(entry.options)
        old.pop("schedule_yaml_path", None)
        new_options.update({k: v for k, v in old.items() if k in new_options})
        hass.config_entries.async_update_entry(entry, options=new_options, version=3)
        LOGGER.info("Migration to v3 complete (from v1)")

    elif entry.version == 2:
        # v2 (options-based zones) → v3 (subentry zones): lift zones out of options.
        old_zones = entry.options.get("zones", [])
        for zone in old_zones:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({
                        "head_entity": zone.get("head_entity", ""),
                        "sensor_entity": zone.get("sensor_entity", ""),
                        "schedule": zone.get("schedule", {"weekday": [], "weekend": []}),
                    }),
                    subentry_type="zone",
                    title=zone["name"],
                    unique_id=zone["id"],
                ),
            )
        new_options = {k: v for k, v in entry.options.items() if k != "zones"}
        hass.config_entries.async_update_entry(entry, options=new_options, version=3)
        LOGGER.info("Migration to v3 complete (from v2, %d zones migrated)", len(old_zones))

    return True
