"""Select platform for ClimateML — per-zone default comfort level."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    for zone in coordinator.zones:
        async_add_entities(
            [ClimateMLZoneComfortLevel(coordinator, zone)],
            config_subentry_id=coordinator.zone_subentry_map.get(zone["id"]),
        )


def _level_label(level: int, comfort_levels: dict[int, dict]) -> str:
    name = comfort_levels.get(level, {}).get("name", "")
    return f"{level} — {name}" if name else str(level)


class ClimateMLZoneComfortLevel(
    CoordinatorEntity[HomeClimateMlCoordinator], RestoreEntity, SelectEntity
):
    """SELECT entity for the zone's default comfort level (1–5)."""

    _attr_should_poll = False
    _attr_icon = "mdi:thermostat"

    def __init__(self, coordinator: HomeClimateMlCoordinator, zone: dict) -> None:
        super().__init__(coordinator)
        self._zone_id = zone["id"]
        self._attr_name = f"ML — {zone['name']} Default Comfort"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_default_comfort_level"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id)},
            name=f"ClimateML — {zone['name']}",
            manufacturer="ClimateML",
            model="Zone Controller",
        )
        self._update_options()

    def _update_options(self) -> None:
        levels = self.coordinator.comfort_levels
        self._attr_options = [_level_label(i, levels) for i in range(1, 6)]

    @property
    def current_option(self) -> str | None:
        level = self.coordinator._zone_default_level.get(self._zone_id, 3)
        return _level_label(level, self.coordinator.comfort_levels)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state not in (
            "unavailable", "unknown", None
        ):
            try:
                level = int(last.state.split(" ")[0])
                if level in range(1, 6):
                    self.coordinator.set_zone_default_level(self._zone_id, level)
            except (ValueError, IndexError):
                pass
        # Refresh so first tick uses the restored level
        await self.coordinator.async_request_refresh()

    async def async_select_option(self, option: str) -> None:
        try:
            level = int(option.split(" ")[0])
        except (ValueError, IndexError):
            return
        if level not in range(1, 6):
            return
        self.coordinator.set_zone_default_level(self._zone_id, level)
        self.async_write_ha_state()
