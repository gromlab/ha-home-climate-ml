"""Switch platform for ClimateML — master enable and vacation mode."""
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
    async_add_entities(
        [
            ClimateMLMasterSwitch(coordinator),
            ClimateMLVacationSwitch(coordinator),
        ],
        config_subentry_id=coordinator.controller_subentry_id,
    )


class ClimateMLMasterSwitch(
    CoordinatorEntity[HomeClimateMlCoordinator], RestoreEntity, SwitchEntity
):
    """Master enable switch. Persists across restarts via RestoreEntity.
    Survives options reload via hass.data. Acts as AND gate with zone climate entities."""

    _attr_should_poll = False
    _attr_name = "All Zones Enabled"

    def __init__(self, coordinator: HomeClimateMlCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_master_enabled"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "controller")},
            name="ClimateML Controller",
            manufacturer="ClimateML",
            model="System Controller",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            enabled = last_state.state == "on"
            self.coordinator.set_master_enabled(enabled)
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self.coordinator._master_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_master_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_master_enabled(False)
        self.async_write_ha_state()


class ClimateMLVacationSwitch(
    CoordinatorEntity[HomeClimateMlCoordinator], RestoreEntity, SwitchEntity
):
    """Vacation mode switch. Overrides all zones to the vacation comfort level."""

    _attr_should_poll = False
    _attr_name = "Vacation Mode"

    def __init__(self, coordinator: HomeClimateMlCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_vacation_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "controller")},
            name="ClimateML Controller",
            manufacturer="ClimateML",
            model="System Controller",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            active = last_state.state == "on"
            self.coordinator.set_vacation_mode(active)
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self.coordinator.vacation_mode

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_vacation_mode(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_vacation_mode(False)
        self.async_write_ha_state()
