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

# v0.6.0 direct setpoint defaults
DEFAULT_SETPOINT_C: float = 22.0
VACATION_SETPOINT_DEFAULT: float = 26.0
IDLE_HEAD_THRESHOLD_DEFAULT: int = 60
ECO_TOLERANCE_DEFAULT: float = 1.0

# Starvation logic constants — see coordinator.py Starvation Logic section
# REVIEW: single-priority-zone — these are tuned empirically; future ML will replace them
# Sign convention: demand_delta and thermal_delta are negative when cooling is active
STARVATION_DEMAND_THRESHOLD: float = -1.0   # °C — demand_delta must be below this to watch
STARVATION_THERMAL_TARGET: float = -10.0    # °C — thermal_delta must reach this to exit suppression
STARVATION_WATCH_SECONDS: int = 1800        # 30 min observation before triggering suppression
STARVATION_SUPPRESS_SECONDS: int = 900      # 15 min max suppression per cycle
STARVATION_COOLDOWN_SECONDS: int = 1800     # 30 min cooldown after suppression before re-watching
STARVATION_MIN_SAMPLES: int = 4             # minimum readings in window before triggering

ML_MODEL_FILENAME: str = "model.pkl"
