"""
Home Climate ML — per-zone heat pump offset correction and scheduling.

Native HA custom integration. Reads zone temperature sensors every 5 minutes,
computes correction offsets (head sensor minus external sensor), and writes
adjusted setpoints back to climate entities.

For more details see https://github.com/gromlab/ha-home-climate-ml
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.loader import async_get_loaded_integration

from .const import DOMAIN, LOGGER, UPDATE_INTERVAL_MINUTES
from .coordinator import HomeClimateMlCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

# No platform files yet — populated in Phase 1 when climate/sensor modules are added
PLATFORMS: list[Platform] = []

type HomeClimateMlConfigEntry = ConfigEntry  # runtime_data: HomeClimateMlCoordinator


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeClimateMlConfigEntry,
) -> bool:
    """Set up Home Climate ML from a config entry."""
    coordinator = HomeClimateMlCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
        update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
    )

    entry.runtime_data = coordinator

    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: HomeClimateMlConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: HomeClimateMlConfigEntry,
) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
