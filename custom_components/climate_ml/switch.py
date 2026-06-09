"""Switch platform for ClimateML — per-zone and master enable/disable."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomeClimateMlCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomeClimateMlCoordinator = entry.runtime_data
    entities: list = [ClimateMLMasterSwitch(coordinator)]
    entities += [
        ClimateMLZoneSwitch(coordinator, zone)
        for zone in coordinator.zones
    ]
    async_add_entities(entities)


class ClimateMLZoneSwitch(
    CoordinatorEntity[HomeClimateMlCoordinator], SwitchEntity, RestoreEntity
):
    _attr_should_poll = False

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} Enabled"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_enabled"
        self._attr_is_on = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id)},
            name=f"ClimateML — {zone['name']}",
            manufacturer="ClimateML",
            model="Zone Controller",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == "on"
            self.coordinator.set_zone_enabled(self._zone_id, self._attr_is_on)

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.coordinator.set_zone_enabled(self._zone_id, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.coordinator.set_zone_enabled(self._zone_id, False)
        self.async_write_ha_state()


class ClimateMLMasterSwitch(
    CoordinatorEntity[HomeClimateMlCoordinator], SwitchEntity
):
    """Master enable switch. In-memory only — resets to OFF on HA restart.
    Survives options reload via hass.data. Acts as AND gate with zone switches."""

    _attr_should_poll = False
    _attr_name = "ML — All Zones Enabled"

    def __init__(self, coordinator: HomeClimateMlCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_master_enabled"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "controller")},
            name="ClimateML Controller",
            manufacturer="ClimateML",
            model="System Controller",
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator._master_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_master_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_master_enabled(False)
        self.async_write_ha_state()
