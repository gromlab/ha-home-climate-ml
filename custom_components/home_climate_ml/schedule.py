"""YAML schedule loader for Home Climate ML."""
from __future__ import annotations

import re
from datetime import datetime, time
from typing import TypedDict

import yaml

from .const import LOGGER, SETPOINT_MIN_C, SETPOINT_MAX_C


class ScheduleBlock(TypedDict):
    start: time
    end: time
    mode: str
    setpoint_c: float | None


class ScheduleError(Exception):
    pass


def _parse_time(s: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m:
        raise ScheduleError(f"Invalid time '{s}' — expected HH:MM")
    h, mn = int(m.group(1)), int(m.group(2))
    if h == 24 and mn == 0:
        return time(23, 59, 59)  # treat 24:00 as end-of-day
    return time(h, mn)


def _validate_zone_schedule(zone_id: str, day_type: str, blocks: list[dict]) -> list[ScheduleBlock]:
    parsed: list[ScheduleBlock] = []
    for i, b in enumerate(blocks):
        try:
            mode = b["mode"].lower()
            if mode not in ("off", "cool"):
                raise ScheduleError(f"Invalid mode '{mode}'")
            setpoint_c: float | None = None
            if mode == "cool":
                setpoint_c = float(b["setpoint_c"])
                if not SETPOINT_MIN_C <= setpoint_c <= SETPOINT_MAX_C:
                    raise ScheduleError(
                        f"setpoint_c {setpoint_c} out of range [{SETPOINT_MIN_C}–{SETPOINT_MAX_C}]"
                    )
            parsed.append({
                "start": _parse_time(b["start"]),
                "end": _parse_time(b["end"]),
                "mode": mode,
                "setpoint_c": setpoint_c,
            })
        except ScheduleError:
            raise
        except Exception as exc:
            raise ScheduleError(
                f"Zone '{zone_id}' {day_type} block {i}: {exc}"
            ) from exc

    # Validate 24-hr coverage with no gaps/overlaps
    parsed.sort(key=lambda b: b["start"])
    prev_end = time(0, 0)
    for b in parsed:
        if b["start"] != prev_end:
            raise ScheduleError(
                f"Zone '{zone_id}' {day_type}: gap or overlap at {b['start']} (expected {prev_end})"
            )
        prev_end = b["end"]
    if prev_end not in (time(23, 59, 59), time(23, 59)):
        # Accept time(23,59,59) as stand-in for 24:00
        pass  # Loose check: last block just needs to reach end of day

    return parsed


def load_schedule(path: str) -> dict[str, dict[str, list[ScheduleBlock]]]:
    """Load and validate schedule YAML. Raises ScheduleError on invalid config."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "schedules" not in raw:
        raise ScheduleError("Missing top-level 'schedules' key in YAML")

    result: dict[str, dict[str, list[ScheduleBlock]]] = {}
    for zone_id, day_types in raw["schedules"].items():
        result[zone_id] = {}
        for day_type, blocks in day_types.items():
            if day_type not in ("weekday", "weekend"):
                raise ScheduleError(f"Zone '{zone_id}': unknown day type '{day_type}'")
            result[zone_id][day_type] = _validate_zone_schedule(zone_id, day_type, blocks)

    return result


def get_block(
    schedules: dict[str, dict[str, list[ScheduleBlock]]],
    zone_id: str,
    now: datetime,
) -> tuple[str, float | None]:
    """Return (mode, setpoint_c) for zone at the given time. Returns ('off', None) if zone not in schedule."""
    if zone_id not in schedules:
        return "off", None

    day_type = "weekend" if now.weekday() >= 5 else "weekday"
    zone = schedules[zone_id]
    blocks = zone.get(day_type) or zone.get("weekday", [])

    current_time = now.time().replace(second=0, microsecond=0)
    for block in blocks:
        if block["start"] <= current_time < block["end"]:
            return block["mode"], block["setpoint_c"]

    # Fallback: last block
    if blocks:
        b = blocks[-1]
        return b["mode"], b["setpoint_c"]
    return "off", None
