"""Constants for home_climate_ml."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "home_climate_ml"

# Update interval — 5-minute decision cycle reading HA state directly
UPDATE_INTERVAL_MINUTES = 5

# Config entry data keys
CONF_ZONES = "zones"

# Default offset bounds (°C) — safety rails for heat pump correction
OFFSET_MIN_C = -5.0
OFFSET_MAX_C = 5.0

# Schedule loader defaults
DEFAULT_SCHEDULE_ENTITY = ""
