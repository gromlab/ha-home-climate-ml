"""Sensor platform for ClimateML — per-zone and controller diagnostic sensors."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomeClimateMlCoordinator

_ZONE_SENSORS = [
    {
        "key": "ext_temp_c",
        "name_suffix": "Room Temperature",
        "unique_suffix": "room_temp",
        "entity_category": None,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    {
        "key": "offset_c",
        "name_suffix": "Offset",
        "unique_suffix": "offset",
        "entity_category": EntityCategory.DIAGNOSTIC,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    {
        "key": "commanded_setpoint",
        "name_suffix": "Commanded Setpoint",
        "unique_suffix": "commanded_setpoint",
        "entity_category": EntityCategory.DIAGNOSTIC,
        "state_class": SensorStateClass.MEASUREMENT,
    },
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomeClimateMlCoordinator = entry.runtime_data

    # Controller entities
    async_add_entities(
        [ClimateMLEnergyTodaySensor(coordinator)],
        config_subentry_id=coordinator.controller_subentry_id,
    )

    # Per-zone entities
    for zone in coordinator.zones:
        entities: list[SensorEntity] = [
            ClimateMLZoneSensor(coordinator, zone, s) for s in _ZONE_SENSORS
        ]
        entities += [
            ClimateMLZoneMLConfidenceSensor(coordinator, zone),
            ClimateMLZoneDecisionLog(coordinator, zone),
            ClimateMLZoneCurrentBlock(coordinator, zone),
            ClimateMLZoneNextTransition(coordinator, zone),
        ]
        async_add_entities(
            entities,
            config_subentry_id=coordinator.zone_subentry_map.get(zone["id"]),
        )


def _zone_device(zone: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, zone["id"])},
        name=f"ClimateML — {zone['name']}",
        manufacturer="ClimateML",
        model="Zone Controller",
    )


_CONTROLLER_DEVICE = DeviceInfo(
    identifiers={(DOMAIN, "controller")},
    name="ClimateML Controller",
    manufacturer="ClimateML",
    model="System Controller",
)


# ---------------------------------------------------------------------------
# Controller sensors
# ---------------------------------------------------------------------------

class ClimateMLEnergyTodaySensor(
    CoordinatorEntity[HomeClimateMlCoordinator], RestoreEntity, SensorEntity
):
    """kWh consumed today — odometer minus midnight snapshot."""

    _attr_should_poll = False
    _attr_name = "Energy Today"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HomeClimateMlCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_controller_energy_today"
        self._attr_device_info = _CONTROLLER_DEVICE
        self._midnight_snapshot: float | None = None
        self._snapshot_date: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            attrs = last.attributes
            self._midnight_snapshot = attrs.get("midnight_snapshot")
            self._snapshot_date = attrs.get("snapshot_date")
        unsub = async_track_time_change(
            self.hass, self._midnight_callback, hour=0, minute=0, second=5
        )
        self.async_on_remove(unsub)

    async def _midnight_callback(self, now: datetime) -> None:
        odometer = self._read_odometer()
        if odometer is not None:
            self._midnight_snapshot = odometer
            self._snapshot_date = now.date().isoformat()
        self.async_write_ha_state()

    def _read_odometer(self) -> float | None:
        entity_id = self.coordinator._controller_config.get("energy_entity", "")
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unavailable", "unknown"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def native_value(self) -> float | None:
        odometer = self._read_odometer()
        if odometer is None:
            return None
        if self._midnight_snapshot is None:
            return None
        return round(max(0.0, odometer - self._midnight_snapshot), 3)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "midnight_snapshot": self._midnight_snapshot,
            "snapshot_date": self._snapshot_date,
        }


# ---------------------------------------------------------------------------
# Per-zone sensors
# ---------------------------------------------------------------------------

class ClimateMLZoneSensor(CoordinatorEntity[HomeClimateMlCoordinator], SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: HomeClimateMlCoordinator,
        zone: dict,
        sensor_def: dict,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._key = sensor_def["key"]
        self._attr_name = f"ML — {zone['name']} {sensor_def['name_suffix']}"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_{sensor_def['unique_suffix']}"
        self._attr_state_class = sensor_def["state_class"]
        if sensor_def["entity_category"]:
            self._attr_entity_category = sensor_def["entity_category"]
        self._attr_device_info = _zone_device(zone)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            zone_data = self.coordinator.data.get(self._zone_id)
            if zone_data:
                return zone_data.get(self._key)
        return None

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        if self.coordinator.data:
            zone_data = self.coordinator.data.get(self._zone_id)
            if zone_data and not zone_data.get("enabled", True):
                return False
        return True


class ClimateMLZoneMLConfidenceSensor(CoordinatorEntity[HomeClimateMlCoordinator], SensorEntity):
    """ML shadow prediction confidence — 0.0 when no model loaded."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False
    _attr_icon = "mdi:robot"

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} ML Confidence"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_ml_confidence"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = _zone_device(zone)

    @property
    def native_value(self) -> float:
        return self.coordinator.get_zone_ml_confidence(self._zone_id)


class ClimateMLZoneDecisionLog(CoordinatorEntity[HomeClimateMlCoordinator], SensorEntity):
    """Rolling log of the last 30 decisions for a zone, newest first."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} Decision Log"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_decision_log"
        self._attr_device_info = _zone_device(zone)

    @property
    def native_value(self) -> str | None:
        log = self.coordinator.get_decision_log(self._zone_id)
        if not log:
            return None
        last = log[0]
        sp = last.get("active_setpoint_c")
        mode = last.get("active_mode", "eco")
        mode_src = last.get("mode_source", "")
        sp_src = last.get("setpoint_source", "")
        commanded_sp = last.get("commanded_setpoint")
        command_issued = last.get("command_issued", False)
        idle = last.get("idle", False)

        source_label = f"{sp_src}/{mode_src}" if mode_src == "occupancy" else sp_src

        if mode == "off":
            return f"off [{source_label}]"
        if idle:
            return f"idle [{source_label}]"

        suffix = " ✓" if command_issued else ""
        if sp is None:
            return f"suppress [{source_label}]"
        if commanded_sp is not None:
            return f"{sp:.1f}°C {mode} → {commanded_sp:.1f}°C [{source_label}]{suffix}"
        return f"{sp:.1f}°C {mode} suppress [{source_label}]"

    @property
    def extra_state_attributes(self) -> dict:
        return {"decisions": self.coordinator.get_decision_log(self._zone_id)}


class ClimateMLZoneCurrentBlock(CoordinatorEntity[HomeClimateMlCoordinator], SensorEntity):
    """Active schedule block for the zone — setpoint + mode and time range."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} Current Schedule Block"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_current_block"
        self._attr_device_info = _zone_device(zone)

    @property
    def native_value(self) -> str | None:
        block = self.coordinator.get_current_block(self._zone_id)
        if block is None:
            sp = self.coordinator.get_zone_default_setpoint(self._zone_id)
            mode = self.coordinator.get_zone_default_mode(self._zone_id)
            return f"default: {sp:.1f}°C {mode}"
        sp = block["setpoint_c"]
        mode = block["mode"]
        start = block["start"].strftime("%H:%M")
        end = block["end"].strftime("%H:%M")
        return f"{sp:.1f}°C {mode} ({start}–{end})"

    @property
    def extra_state_attributes(self) -> dict:
        block = self.coordinator.get_current_block(self._zone_id)
        if block is None:
            return {
                "setpoint_c": self.coordinator.get_zone_default_setpoint(self._zone_id),
                "mode": self.coordinator.get_zone_default_mode(self._zone_id),
                "source": "default",
            }
        return {
            "setpoint_c": block["setpoint_c"],
            "mode": block["mode"],
            "start": block["start"].strftime("%H:%M"),
            "end": block["end"].strftime("%H:%M"),
        }


class ClimateMLZoneNextTransition(CoordinatorEntity[HomeClimateMlCoordinator], SensorEntity):
    """Next schedule block boundary for the zone, or 'No schedule' when unscheduled."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} Next Schedule Transition"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_next_transition"
        self._attr_device_info = _zone_device(zone)

    @property
    def native_value(self) -> str:
        nxt = self.coordinator.get_next_transition(self._zone_id)
        if nxt is None:
            return "No schedule"
        return nxt.strftime("%a %H:%M")
