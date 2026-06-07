"""Constants for ClimateML."""
from __future__ import annotations
import logging

DOMAIN = "climate_ml"
LOGGER = logging.getLogger(__package__)
UPDATE_INTERVAL_MINUTES = 5

ZONES: dict[str, dict[str, str]] = {
    "main_floor": {
        "head": "climate.ac_living_room",
        "external": "sensor.living_room_atc_temperature",
        "name": "Main Floor",
    },
    "front_bedroom": {
        "head": "climate.ac_front_bedroom",
        "external": "sensor.atc_d6c6_temperature",
        "name": "Front Bedroom",
    },
    "middle_bedroom": {
        "head": "climate.ac_middle_bedroom",
        "external": "sensor.middle_bedroom_temperature",
        "name": "Middle Bedroom",
    },
    "rear_bedroom": {
        "head": "climate.ac_rear_bedroom",
        "external": "sensor.rear_bedroom_atc_temperature",
        "name": "Rear Bedroom",
    },
}

SENSE_ONLY: dict[str, str] = {
    "hallway": "sensor.hallway_atc_temperature",
    "outdoor": "sensor.outdoor_temp_heat_pump",
}

SETPOINT_MIN_C = 16.0
SETPOINT_MAX_C = 28.0
OFFSET_CLAMP_C = 5.0
SETPOINT_TOLERANCE_C = 0.5

FORCE_COOL_THRESHOLD_C = 30.0
FORCE_COOL_CLEAR_C = 28.0
HARD_ALERT_THRESHOLD_C = 35.0
HARD_ALERT_RATE_LIMIT_MIN = 30

EXTERNAL_SENSOR_MIN_C = 0.0
EXTERNAL_SENSOR_MAX_C = 50.0

OVERRIDE_DURATION_MINUTES = 120
DEFAULT_SETPOINT_C = 21.0

CONF_SCHEDULE_PATH = "schedule_yaml_path"
DEFAULT_SCHEDULE_PATH = "/config/climate_schedules.yaml"
