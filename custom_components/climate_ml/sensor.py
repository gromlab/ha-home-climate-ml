"""Sensor platform for ClimateML — per-zone diagnostic sensors."""
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
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomeClimateMlCoordinator

_SENSORS = [
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
        "key": "corrected_setpoint_c",
        "name_suffix": "Corrected Setpoint",
        "unique_suffix": "corrected_setpoint",
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
    entities = []
    for zone in coordinator.zones:
        for sensor_def in _SENSORS:
            entities.append(ClimateMLZoneSensor(coordinator, zone, sensor_def))
        entities.append(ClimateMLZoneDecisionLog(coordinator, zone))
        entities.append(ClimateMLZoneCurrentBlock(coordinator, zone))
        entities.append(ClimateMLZoneNextTransition(coordinator, zone))
    async_add_entities(entities)


def _device_info(zone: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, zone["id"])},
        name=f"ClimateML — {zone['name']}",
        manufacturer="ClimateML",
        model="Zone Controller",
    )


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
        self._attr_device_info = _device_info(zone)

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


class ClimateMLZoneDecisionLog(CoordinatorEntity[HomeClimateMlCoordinator], SensorEntity):
    """Rolling log of the last 30 decisions for a zone, newest first."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} Decision Log"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_decision_log"
        self._attr_device_info = _device_info(zone)

    @property
    def native_value(self) -> str | None:
        log = self.coordinator.get_decision_log(self._zone_id)
        if not log:
            return None
        last = log[0]
        mode = last.get("mode", "off")
        source = last.get("source", "")
        commanded = last.get("commanded", False)

        if mode == "off":
            return f"off [{source}]"

        scheduled_c = last.get("scheduled_c")
        corrected_c = last.get("corrected_c")
        offset_c = last.get("offset_c")

        suffix = " ✓" if commanded else " held"

        if offset_c is not None and abs(offset_c) >= 0.05 and corrected_c is not None and scheduled_c is not None:
            sign = "+" if offset_c >= 0 else ""
            return (
                f"{mode}: {scheduled_c:.1f}°C → {corrected_c:.1f}°C "
                f"({sign}{offset_c:.1f}°C offset) [{source}]{suffix}"
            )

        sp = corrected_c if corrected_c is not None else scheduled_c
        sp_str = f"{sp:.1f}°C" if sp is not None else "?"
        return f"{mode} @ {sp_str} [{source}]{suffix}"

    @property
    def extra_state_attributes(self) -> dict:
        return {"decisions": self.coordinator.get_decision_log(self._zone_id)}


class ClimateMLZoneCurrentBlock(CoordinatorEntity[HomeClimateMlCoordinator], SensorEntity):
    """Active schedule block for the zone — mode and setpoint."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} Current Schedule Block"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_current_block"
        self._attr_device_info = _device_info(zone)

    @property
    def native_value(self) -> str | None:
        block = self.coordinator.get_current_block(self._zone_id)
        if block is None:
            return "off (unscheduled)"
        mode = block["mode"]
        sp = block.get("setpoint_c")
        start = block["start"].strftime("%H:%M")
        end = block["end"].strftime("%H:%M")
        if mode == "off":
            return f"off ({start}–{end})"
        sp_str = f"{sp:.1f}°C" if sp is not None else "?"
        return f"{mode} @ {sp_str} ({start}–{end})"

    @property
    def extra_state_attributes(self) -> dict:
        block = self.coordinator.get_current_block(self._zone_id)
        if block is None:
            return {}
        return {
            "mode": block["mode"],
            "setpoint_c": block.get("setpoint_c"),
            "start": block["start"].strftime("%H:%M"),
            "end": block["end"].strftime("%H:%M"),
        }


class ClimateMLZoneNextTransition(CoordinatorEntity[HomeClimateMlCoordinator], SensorEntity):
    """Timestamp of the next schedule block boundary for the zone."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} Next Schedule Transition"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_next_transition"
        self._attr_device_info = _device_info(zone)

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.get_next_transition(self._zone_id)
