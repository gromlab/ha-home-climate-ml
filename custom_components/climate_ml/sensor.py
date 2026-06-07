"""Sensor platform for ClimateML — per-zone diagnostic sensors."""
from __future__ import annotations

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
    async_add_entities(entities)


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id)},
            name=f"ClimateML — {zone['name']}",
            manufacturer="ClimateML",
            model="Zone Controller",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            zone_data = self.coordinator.data.get(self._zone_id)
            if zone_data:
                return zone_data.get(self._key)
        return None

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success
