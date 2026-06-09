"""ClimateML — per-zone heat pump offset correction and scheduling."""
from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_change

from .const import DEFAULT_OPTIONS, DOMAIN, LOGGER, _REMOVED_OPTIONS
from .coordinator import HomeClimateMlCoordinator
from .store import ClimateDataStore

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

type ClimateMLConfigEntry = ConfigEntry

_CONTROLLER_IDENTIFIER = "controller"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateMLConfigEntry,
) -> bool:
    options = entry.options

    # Zones come from zone subentries; build zone list and zone_id → subentry_id map.
    zone_subentry_map: dict[str, str] = {}
    zones = []
    controller_subentry_id: str | None = None
    controller_config: dict = {}

    for s in entry.subentries.values():
        if s.subentry_type == "zone":
            zone_id = s.unique_id or s.subentry_id
            zones.append({
                "id": zone_id,
                "name": s.title,
                "head_entity": s.data.get("head_entity", ""),
                "sensor_entity": s.data.get("sensor_entity", ""),
                "eva_in_entity": s.data.get("eva_in_entity", ""),
                "eva_out_entity": s.data.get("eva_out_entity", ""),
                "schedule": s.data.get("schedule", {"weekday": [], "weekend": []}),
            })
            zone_subentry_map[zone_id] = s.subentry_id
        elif s.subentry_type == "controller":
            controller_subentry_id = s.subentry_id
            controller_config = dict(s.data)

    # Sync device registry: remove orphaned zone devices, create/update zone + controller devices.
    dreg = dr.async_get(hass)
    current_zone_ids = {z["id"] for z in zones}
    protected_identifiers = {_CONTROLLER_IDENTIFIER}

    for device in dr.async_entries_for_config_entry(dreg, entry.entry_id):
        for ident_domain, ident_value in device.identifiers:
            if (
                ident_domain == DOMAIN
                and ident_value not in current_zone_ids
                and ident_value not in protected_identifiers
            ):
                dreg.async_remove_device(device.id)
                break

    # Controller device — create before async_forward_entry_setups so NUMBER entities can reference it.
    if controller_subentry_id is not None:
        dreg.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=controller_subentry_id,
            identifiers={(DOMAIN, _CONTROLLER_IDENTIFIER)},
            name="ClimateML Controller",
            manufacturer="ClimateML",
            model="System Controller",
        )

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
        for k in DEFAULT_OPTIONS
        if k not in ("hallway_sensor", "outdoor_sensor")
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
        controller_config=controller_config,
        controller_subentry_id=controller_subentry_id,
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

    if entry.version == 3:
        # v3 → v4: create controller subentry, strip removed option keys, add new defaults.
        has_controller = any(
            s.subentry_type == "controller" for s in entry.subentries.values()
        )
        if not has_controller:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({}),
                    subentry_type="controller",
                    title="ClimateML Controller",
                    unique_id=_CONTROLLER_IDENTIFIER,
                ),
            )
            LOGGER.info("Migration to v4: controller subentry created")

        new_options = {k: v for k, v in entry.options.items() if k not in _REMOVED_OPTIONS}
        if "sensor_guard_threshold_c" not in new_options:
            new_options["sensor_guard_threshold_c"] = DEFAULT_OPTIONS["sensor_guard_threshold_c"]
        hass.config_entries.async_update_entry(entry, options=new_options, version=4)
        LOGGER.info("Migration to v4 complete")

    return True
