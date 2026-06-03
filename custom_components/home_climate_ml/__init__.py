"""Home Climate ML — per-zone heat pump offset correction and scheduling."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers.event import async_track_time_change

from .const import CONF_SCHEDULE_PATH, DEFAULT_SCHEDULE_PATH, DOMAIN, LOGGER, UPDATE_INTERVAL_MINUTES
from .coordinator import HomeClimateMlCoordinator
from .schedule import ScheduleError, load_schedule
from .store import ClimateDataStore

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

PLATFORMS: list[Platform] = [Platform.CLIMATE]

type HomeClimateMlConfigEntry = ConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeClimateMlConfigEntry,
) -> bool:
    schedule_path = entry.options.get(
        CONF_SCHEDULE_PATH,
        hass.config.path("climate_schedules.yaml"),
    )
    db_path = hass.config.path("home_climate_ml", "decisions.db")

    # Load schedule (blocking I/O in executor)
    try:
        schedules = await hass.async_add_executor_job(load_schedule, schedule_path)
        LOGGER.info("Loaded schedule from %s", schedule_path)
    except (ScheduleError, FileNotFoundError) as exc:
        LOGGER.warning("Schedule not loaded (%s) — integration will run without scheduling", exc)
        schedules = {}

    store = await hass.async_add_executor_job(ClimateDataStore, db_path)

    coordinator = HomeClimateMlCoordinator(
        hass=hass,
        store=store,
        schedules=schedules,
        update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
    )

    entry.runtime_data = coordinator

    await coordinator.async_config_entry_first_refresh()
    await coordinator.restore_overrides()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Daily retention pruning at 03:00
    async def _prune(_now):
        await hass.async_add_executor_job(store.prune)

    entry.async_on_unload(
        async_track_time_change(hass, _prune, hour=3, minute=0, second=0)
    )

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: HomeClimateMlConfigEntry,
) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: HomeClimateMlConfigEntry,
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
