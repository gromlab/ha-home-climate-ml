"""DataUpdateCoordinator for home_climate_ml."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class HomeClimateMlCoordinator(DataUpdateCoordinator):
    """Coordinator for the Home Climate ML 5-minute decision cycle.

    Reads zone sensor states from HA directly (local_polling) and computes
    heat pump offset corrections. No external API calls — all data lives
    in the HA state machine.
    """

    async def _async_update_data(self) -> Any:
        """Fetch current zone data from HA state machine.

        Phase 1 stub — returns empty dict. Real logic (per-zone offset
        computation, schedule evaluation, setpoint writes) ships in Phase 1.
        """
        try:
            # Phase 1 will populate this with per-zone offset data:
            # {zone_id: {"offset": float, "head_temp": float, "ext_temp": float}}
            return {}
        except Exception as exception:
            raise UpdateFailed(
                f"Error reading zone data from HA state machine: {exception}"
            ) from exception
