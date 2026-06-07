"""Constants for ClimateML."""
from __future__ import annotations

import logging

DOMAIN = "climate_ml"
LOGGER = logging.getLogger(__package__)

EXTERNAL_SENSOR_MIN_C = 0.0
EXTERNAL_SENSOR_MAX_C = 50.0

DEFAULT_OPTIONS: dict = {
    "update_interval_minutes": 5,
    "override_duration_minutes": 120,
    "setpoint_tolerance_c": 0.5,
    "offset_clamp_c": 5.0,
    "setpoint_min_c": 16.0,
    "setpoint_max_c": 28.0,
    "default_setpoint_c": 21.0,
    "force_cool_threshold_c": 30.0,
    "force_cool_clear_c": 28.0,
    "hallway_sensor": "",
    "outdoor_sensor": "",
    "zones": [
        {
            "id": "main_floor",
            "name": "Main Floor",
            "head_entity": "climate.ac_living_room",
            "sensor_entity": "sensor.living_room_atc_temperature",
            "schedule": {"weekday": [], "weekend": []},
        },
        {
            "id": "front_bedroom",
            "name": "Front Bedroom",
            "head_entity": "climate.ac_front_bedroom",
            "sensor_entity": "sensor.atc_d6c6_temperature",
            "schedule": {"weekday": [], "weekend": []},
        },
        {
            "id": "middle_bedroom",
            "name": "Middle Bedroom",
            "head_entity": "climate.ac_middle_bedroom",
            "sensor_entity": "sensor.middle_bedroom_temperature",
            "schedule": {"weekday": [], "weekend": []},
        },
        {
            "id": "rear_bedroom",
            "name": "Rear Bedroom",
            "head_entity": "climate.ac_rear_bedroom",
            "sensor_entity": "sensor.rear_bedroom_atc_temperature",
            "schedule": {"weekday": [], "weekend": []},
        },
    ],
}
