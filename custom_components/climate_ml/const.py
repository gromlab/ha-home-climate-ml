"""Constants for ClimateML."""
from __future__ import annotations

import logging

DOMAIN = "climate_ml"
LOGGER = logging.getLogger(__package__)

EXTERNAL_SENSOR_MIN_C = 0.0
EXTERNAL_SENSOR_MAX_C = 50.0

# Values that live in controller subentry data (read during setup, editable via controller form)
CONTROLLER_DEFAULTS: dict = {
    "update_interval_minutes": 5,
    "setpoint_min_c": 16.0,
    "setpoint_max_c": 28.0,
}

# Values managed by NUMBER entities (restored via RestoreEntity, updated in-place)
DEFAULT_OPTIONS: dict = {
    "override_duration_minutes": 120,
    "setpoint_tolerance_c": 0.5,
    "sensor_guard_threshold_c": 3.0,
}

# Keys removed in v0.4.6 migration (v3→v4)
_REMOVED_OPTIONS = {"force_cool_threshold_c", "force_cool_clear_c", "offset_clamp_c"}

# Keys removed in v0.4.8 migration (v4→v5) — moved to controller subentry or dropped
_V5_REMOVED_OPTIONS = {
    "update_interval_minutes", "setpoint_min_c", "setpoint_max_c",
    "hallway_sensor", "outdoor_sensor",
}

# v0.5 comfort band defaults — editable per install via Controller → Configure
COMFORT_LEVEL_DEFAULTS: list[dict] = [
    {"level": 1, "name": "Sleep",    "min_c": 19.5, "max_c": 20.7},
    {"level": 2, "name": "Comfort",  "min_c": 19.5, "max_c": 21.5},
    {"level": 3, "name": "Relaxed",  "min_c": 19.5, "max_c": 22.0},
    {"level": 4, "name": "Eco",      "min_c": 19.0, "max_c": 23.0},
    {"level": 5, "name": "Vacation", "min_c": 16.0, "max_c": 24.0},
]

BAND_HYSTERESIS_DEFAULT: float = 0.2

ML_MODEL_FILENAME: str = "model.pkl"
