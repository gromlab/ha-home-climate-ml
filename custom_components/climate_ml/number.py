"""Number platform for ClimateML — controller tuning parameters."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import HomeClimateMlCoordinator


@dataclass
class NumberDef:
    key: str
    name: str
    unique_suffix: str
    native_min: float
    native_max: float
    native_step: float
    native_unit: str | None
    default: float


_NUMBERS = [
    NumberDef(
        key="override_duration_minutes",
        name="ML — Override Duration",
        unique_suffix="override_duration",
        native_min=15,
        native_max=480,
        native_step=15,
        native_unit=UnitOfTime.MINUTES,
        default=120,
    ),
    NumberDef(
        key="setpoint_tolerance_c",
        name="ML — Setpoint Tolerance",
        unique_suffix="setpoint_tolerance",
        native_min=0.1,
        native_max=2.0,
        native_step=0.1,
        native_unit=UnitOfTemperature.CELSIUS,
        default=0.5,
    ),
    NumberDef(
        key="default_setpoint_c",
        name="ML — Default Setpoint",
        unique_suffix="default_setpoint",
        native_min=16.0,
        native_max=28.0,
        native_step=0.5,
        native_unit=UnitOfTemperature.CELSIUS,
        default=21.0,
    ),
    NumberDef(
        key="sensor_guard_threshold_c",
        name="ML — Sensor Guard Threshold",
        unique_suffix="sensor_guard_threshold",
        native_min=1.0,
        native_max=10.0,
        native_step=0.5,
        native_unit=UnitOfTemperature.CELSIUS,
        default=3.0,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomeClimateMlCoordinator = entry.runtime_data
    async_add_entities([
        ClimateMLControllerNumber(coordinator, defn)
        for defn in _NUMBERS
    ])


class ClimateMLControllerNumber(NumberEntity, RestoreEntity):
    """Tuning parameter stored in coordinator.params — updated in-place, never triggers reload."""

    _attr_should_poll = False
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: HomeClimateMlCoordinator,
        defn: NumberDef,
    ) -> None:
        self._coordinator = coordinator
        self._defn = defn
        self._attr_name = defn.name
        self._attr_unique_id = f"{DOMAIN}_{defn.unique_suffix}"
        self._attr_native_min_value = defn.native_min
        self._attr_native_max_value = defn.native_max
        self._attr_native_step = defn.native_step
        self._attr_native_unit_of_measurement = defn.native_unit
        self._attr_native_value = coordinator.params.get(defn.key, defn.default)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "controller")},
            name="ClimateML Controller",
            manufacturer="ClimateML",
            model="System Controller",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is not None:
            try:
                restored = float(state.state)
                if self._attr_native_min_value <= restored <= self._attr_native_max_value:
                    self._attr_native_value = restored
                    self._coordinator.params[self._defn.key] = restored
            except (ValueError, TypeError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        # Update coordinator params in-place — does NOT trigger entry reload
        self._coordinator.params[self._defn.key] = value
        self.async_write_ha_state()
