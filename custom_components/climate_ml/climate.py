"""Climate platform for ClimateML — virtual thermostat per zone."""
from __future__ import annotations

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_SETPOINT_C, DOMAIN, LOGGER, ZONES
from .coordinator import HomeClimateMlCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomeClimateMlCoordinator = entry.runtime_data
    async_add_entities(
        HomeClimateMlZone(coordinator, zone_id, zone_cfg)
        for zone_id, zone_cfg in ZONES.items()
    )


class HomeClimateMlZone(CoordinatorEntity[HomeClimateMlCoordinator], ClimateEntity):
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_should_poll = False
    _attr_min_temp = 16.0
    _attr_max_temp = 28.0
    _attr_target_temperature_step = 0.5

    def __init__(
        self,
        coordinator: HomeClimateMlCoordinator,
        zone_id: str,
        zone_cfg: dict,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_cfg = zone_cfg
        display_name = zone_cfg["name"]
        self._attr_name = f"ML — {display_name}"
        self._attr_unique_id = f"{DOMAIN}_{zone_id}"

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
    def target_temperature(self) -> float | None:
        if d := self.zone_data:
            return d.get("target_setpoint_c", DEFAULT_SETPOINT_C)
        return DEFAULT_SETPOINT_C

    @property
    def hvac_mode(self) -> HVACMode:
        if d := self.zone_data:
            mode = d.get("target_mode", "off")
            return HVACMode.COOL if mode == "cool" else HVACMode.OFF
        return HVACMode.OFF

    @property
    def extra_state_attributes(self) -> dict:
        if not (d := self.zone_data):
            return {}
        return {
            "enabled": d.get("enabled", True),
            "offset_c": d.get("offset_c"),
            "corrected_setpoint_c": d.get("corrected_setpoint_c"),
            "head_temp_c": d.get("head_temp_c"),
            "override_active": d.get("override_active", False),
            "schedule_source": d.get("schedule_source"),
            "error": d.get("error"),
        }

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        mode = kwargs.get("hvac_mode")
        current_mode = self.hvac_mode.value
        self.coordinator.set_override(
            self._zone_id,
            float(temp),
            mode.value if mode else current_mode,
        )
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        current_setpoint = self.target_temperature or DEFAULT_SETPOINT_C
        self.coordinator.set_override(self._zone_id, current_setpoint, hvac_mode.value)
        self.async_write_ha_state()
