"""ClimateML — per-zone climate offset correction and scheduling."""
from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_change

from .const import (
    CONTROLLER_DEFAULTS,
    DEFAULT_OPTIONS,
    DEFAULT_SETPOINT_C,
    DOMAIN,
    IDLE_HEAD_THRESHOLD_DEFAULT,
    LOGGER,
    ML_MODEL_FILENAME,
    VACATION_SETPOINT_DEFAULT,
    _REMOVED_OPTIONS,
    _V5_REMOVED_OPTIONS,
)
from .coordinator import HomeClimateMlCoordinator
from .store import ClimateDataStore

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

type ClimateMLConfigEntry = ConfigEntry

_CONTROLLER_IDENTIFIER = "controller"

# Comfort level → setpoint mapping used in v6→v7 migration
_LEVEL_TO_SETPOINT: dict[int, float] = {1: 20.5, 2: 21.0, 3: 22.0, 4: 23.0, 5: 26.0}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateMLConfigEntry,
) -> bool:
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
                "default_setpoint_c": float(s.data.get("default_setpoint_c", DEFAULT_SETPOINT_C)),
                "default_mode": str(s.data.get("default_mode", "eco")),
                "is_starvation_priority": bool(s.data.get("is_starvation_priority", False)),
                "eva_in_entity": s.data.get("eva_in_entity", ""),
                "eva_out_entity": s.data.get("eva_out_entity", ""),
                "demand_delta_entity": s.data.get("demand_delta_entity", ""),
                "head_thermal_delta_entity": s.data.get("head_thermal_delta_entity", ""),
                "occupancy_entity": s.data.get("occupancy_entity", ""),
                "humidity_entity": s.data.get("humidity_entity", ""),
                "door_entity": s.data.get("door_entity", ""),
                "window_entity": s.data.get("window_entity", ""),
                "schedule": s.data.get("schedule", {"blocks": []}),
            })
            zone_subentry_map[zone_id] = s.subentry_id
        elif s.subentry_type == "controller":
            controller_subentry_id = s.subentry_id
            controller_config = dict(s.data)

    # Sync device registry — remove stale zone devices
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

    # Controller device created before entity setup so NUMBER/SWITCH/SENSOR entities can reference it
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

    # params: tuning values from NUMBER RestoreEntities (overwritten in-place after setup)
    params = {
        **{k: controller_config.get(k, v) for k, v in CONTROLLER_DEFAULTS.items()},
        **DEFAULT_OPTIONS,
        "vacation_setpoint_c": float(controller_config.get("vacation_setpoint_c", VACATION_SETPOINT_DEFAULT)),
        "idle_head_threshold_minutes": int(controller_config.get("idle_head_threshold_minutes", IDLE_HEAD_THRESHOLD_DEFAULT)),
        "eco_tolerance_c": float(controller_config.get("eco_tolerance_c", 1.0)),
    }

    db_path = hass.config.path("climate_ml", "decisions.db")
    store = await hass.async_add_executor_job(ClimateDataStore, db_path)

    coordinator = HomeClimateMlCoordinator(
        hass=hass,
        entry_id=entry.entry_id,
        store=store,
        zones=zones,
        update_interval=timedelta(minutes=params["update_interval_minutes"]),
        params=params,
        controller_config=controller_config,
        controller_subentry_id=controller_subentry_id,
        zone_subentry_map=zone_subentry_map,
    )

    entry.runtime_data = coordinator

    await coordinator.async_config_entry_first_refresh()
    await coordinator.restore_overrides()

    # Attempt to load ML model — logs warning on failure, never blocks setup
    await _async_load_ml_model(hass, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Remove stale bare (no-subentry) device associations created by older entity registration
    dreg_post = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dreg_post, entry.entry_id):
        subentries = device.config_entries_subentries.get(entry.entry_id, set())
        if None in subentries and any(s is not None for s in subentries):
            dreg_post.async_update_device(
                device.id,
                remove_config_entry_id=entry.entry_id,
                remove_config_subentry_id=None,
            )

    async def _prune(_now):
        await hass.async_add_executor_job(store.prune)

    entry.async_on_unload(
        async_track_time_change(hass, _prune, hour=3, minute=0, second=0)
    )

    return True


async def _async_load_ml_model(
    hass: HomeAssistant, coordinator: HomeClimateMlCoordinator
) -> None:
    """Load ML model from /config/climate_ml/model.pkl — logs warning on failure, never raises."""
    import os
    model_path = hass.config.path("climate_ml", ML_MODEL_FILENAME)

    def _load():
        if not os.path.exists(model_path):
            return None
        import joblib  # noqa: PLC0415
        return joblib.load(model_path)

    try:
        model = await hass.async_add_executor_job(_load)
        if model is None:
            return
        if not callable(getattr(model, "predict", None)):
            LOGGER.warning(
                "ML model at %s has no .predict() method — shadow mode disabled", model_path
            )
            return
        coordinator.set_ml_model(model)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("ML model load failed (%s) — shadow mode disabled", exc)


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
        new_options = copy.deepcopy({**CONTROLLER_DEFAULTS, **DEFAULT_OPTIONS})
        old = dict(entry.options)
        old.pop("schedule_yaml_path", None)
        new_options.update({k: v for k, v in old.items() if k in new_options})
        hass.config_entries.async_update_entry(entry, options=new_options, version=3)
        LOGGER.info("Migration to v3 complete (from v1)")

    elif entry.version == 2:
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

    if entry.version == 4:
        new_options = {k: v for k, v in entry.options.items() if k not in _V5_REMOVED_OPTIONS}
        hass.config_entries.async_update_entry(entry, options=new_options, version=5)
        LOGGER.info("Migration to v5 complete (options consolidated to controller subentry)")

    if entry.version == 5:
        from homeassistant.helpers import entity_registry as er

        # Update zone subentries: strip mode/setpoint_c from blocks, add comfort_level
        for subentry in list(entry.subentries.values()):
            if subentry.subentry_type != "zone":
                continue
            new_schedule: dict = {"weekday": [], "weekend": []}
            for day in ("weekday", "weekend"):
                for block in subentry.data.get("schedule", {}).get(day, []):
                    new_schedule[day].append({
                        "start": block.get("start", "00:00"),
                        "end": block.get("end", "00:00"),
                        "comfort_level": 3,  # default Relaxed
                    })
            new_data = {
                **dict(subentry.data),
                "schedule": new_schedule,
                "default_comfort_level": 3,
                "humidity_entity": subentry.data.get("humidity_entity", ""),
                "door_entity": subentry.data.get("door_entity", ""),
                "window_entity": subentry.data.get("window_entity", ""),
            }
            hass.config_entries.async_update_subentry(
                entry, subentry, data=MappingProxyType(new_data)
            )

        # Update controller subentry: add comfort levels and new settings
        for subentry in list(entry.subentries.values()):
            if subentry.subentry_type != "controller":
                continue
            new_data = {
                **dict(subentry.data),
                "comfort_levels": _COMFORT_LEVEL_DEFAULTS_V5,
                "vacation_comfort_level": 5,
                "band_hysteresis_c": 0.5,
            }
            hass.config_entries.async_update_subentry(
                entry, subentry, data=MappingProxyType(new_data)
            )

        # Remove zone enabled switch entities (replaced by climate entity AUTO/OFF)
        ereg = er.async_get(hass)
        for entity_entry in er.async_entries_for_config_entry(ereg, entry.entry_id):
            if (
                entity_entry.domain == "switch"
                and entity_entry.unique_id
                and entity_entry.unique_id.endswith("_enabled")
                and entity_entry.unique_id != f"{DOMAIN}_master_enabled"
            ):
                ereg.async_remove(entity_entry.entity_id)

        hass.config_entries.async_update_entry(entry, version=6)
        LOGGER.info("Migration to v6 complete")

    if entry.version == 6:
        from homeassistant.helpers import entity_registry as er

        # Zone subentries: convert weekday/weekend blocks to flat blocks list with setpoint_c + mode
        for subentry in list(entry.subentries.values()):
            if subentry.subentry_type == "zone":
                old_schedule = subentry.data.get("schedule", {})
                new_blocks = []
                for day_type in ("weekday", "weekend"):
                    for b in old_schedule.get(day_type, []):
                        level = int(b.get("comfort_level", 3))
                        new_blocks.append({
                            "start": b.get("start", "00:00"),
                            "end": b.get("end", "00:00"),
                            "day_type": day_type,
                            "setpoint_c": _LEVEL_TO_SETPOINT.get(level, 22.0),
                            "mode": "eco",  # migrate all blocks to eco by default
                        })
                old_level = int(subentry.data.get("default_comfort_level", 3))
                new_data = {
                    **dict(subentry.data),
                    "default_setpoint_c": _LEVEL_TO_SETPOINT.get(old_level, DEFAULT_SETPOINT_C),
                    "default_mode": "eco",
                    "is_starvation_priority": False,
                    "demand_delta_entity": "",
                    "head_thermal_delta_entity": "",
                    "schedule": {"blocks": new_blocks},
                }
                new_data.pop("default_comfort_level", None)
                hass.config_entries.async_update_subentry(
                    entry, subentry, data=MappingProxyType(new_data)
                )

            elif subentry.subentry_type == "controller":
                vac_level = int(subentry.data.get("vacation_comfort_level", 5))
                new_data = {
                    **dict(subentry.data),
                    "vacation_setpoint_c": _LEVEL_TO_SETPOINT.get(vac_level, VACATION_SETPOINT_DEFAULT),
                    "idle_head_threshold_minutes": IDLE_HEAD_THRESHOLD_DEFAULT,
                    "eco_tolerance_c": 1.0,
                }
                for key in ("comfort_levels", "vacation_comfort_level", "band_hysteresis_c"):
                    new_data.pop(key, None)
                hass.config_entries.async_update_subentry(
                    entry, subentry, data=MappingProxyType(new_data)
                )

        # Remove stale select entities (comfort level selectors)
        ereg = er.async_get(hass)
        for entity_entry in er.async_entries_for_config_entry(ereg, entry.entry_id):
            if entity_entry.domain == "select" and "comfort_level" in (entity_entry.unique_id or ""):
                ereg.async_remove(entity_entry.entity_id)

        hass.config_entries.async_update_entry(entry, version=7)
        LOGGER.info("Migration to v7 complete (comfort bands → direct setpoints)")

    return True


# Kept for v5→v6 migration path only — not used in v0.6.0 runtime
_COMFORT_LEVEL_DEFAULTS_V5 = [
    {"level": 1, "name": "Sleep", "min_c": 19.0, "max_c": 21.0},
    {"level": 2, "name": "Comfort", "min_c": 20.0, "max_c": 22.0},
    {"level": 3, "name": "Relaxed", "min_c": 21.0, "max_c": 23.0},
    {"level": 4, "name": "Eco", "min_c": 22.0, "max_c": 24.0},
    {"level": 5, "name": "Vacation", "min_c": 24.0, "max_c": 26.0},
]
