"""Climate platform for ClimateML — virtual thermostat per zone."""
from __future__ import annotations

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
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
    for zone in coordinator.zones:
        async_add_entities(
            [ClimateMLZone(coordinator, zone)],
            config_subentry_id=coordinator.zone_subentry_map.get(zone["id"]),
        )


class ClimateMLZone(CoordinatorEntity[HomeClimateMlCoordinator], RestoreEntity, ClimateEntity):
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.OFF]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature(0)
    _attr_should_poll = False

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']}"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id)},
            name=f"ClimateML — {zone['name']}",
            manufacturer="ClimateML",
            model="Zone Controller",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            enabled = last.state != HVACMode.OFF
            self.coordinator.set_zone_enabled(self._zone_id, enabled)

    @property
    def _enabled(self) -> bool:
        return self.coordinator.is_zone_enabled(self._zone_id)

    @property
    def zone_data(self):
        if self.coordinator.data:
            return self.coordinator.data.get(self._zone_id)
        return None

    @property
    def current_temperature(self) -> float | None:
        if d := self.zone_data:
            return d.get("ext_temp_c")
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.AUTO if self._enabled else HVACMode.OFF

    @property
    def extra_state_attributes(self) -> dict:
        if not (d := self.zone_data):
            return {"enabled": self._enabled}
        return {
            "enabled": d.get("enabled", True),
            "offset_c": d.get("offset_c"),
            "commanded_setpoint": d.get("commanded_setpoint"),
            "head_temp_c": d.get("head_temp_c"),
            "override_active": d.get("override_active", False),
            "comfort_source": d.get("comfort_source"),
            "active_comfort_level": d.get("active_comfort_level"),
            "band_min": d.get("band_min"),
            "band_max": d.get("band_max"),
            "error": d.get("error"),
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self.coordinator.set_zone_enabled(self._zone_id, hvac_mode == HVACMode.AUTO)
        self.async_write_ha_state()
