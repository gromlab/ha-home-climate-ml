"""Schedule parsing for ClimateML."""
from __future__ import annotations

import re
from datetime import datetime, time
from typing import TypedDict

from .const import LOGGER


class ScheduleBlock(TypedDict):
    start: time
    end: time
    mode: str
    setpoint_c: float | None


class ScheduleError(Exception):
    pass


def _parse_time(s: str) -> time:
    """Accept HH:MM or HH:MM:SS (TimeSelector returns seconds)."""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", s)
    if not m:
        raise ScheduleError(f"Invalid time '{s}' — expected HH:MM")
    h, mn = int(m.group(1)), int(m.group(2))
    if h == 24 and mn == 0:
        return time(23, 59, 59)
    return time(h, mn)


def _normalise_time_str(s: str) -> str:
    """Strip seconds from HH:MM:SS so stored strings are always HH:MM."""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2}:\d{2})(?::\d{2})?", s)
    return m.group(1) if m else s


def _validate_zone_schedule(
    zone_id: str,
    day_type: str,
    blocks: list[dict],
    setpoint_min: float,
    setpoint_max: float,
) -> list[ScheduleBlock]:
    parsed: list[ScheduleBlock] = []
    for i, b in enumerate(blocks):
        try:
            raw_mode = b["mode"]
            if raw_mode is False:
                mode = "off"
            else:
                mode = str(raw_mode).lower()
            if mode not in ("off", "cool"):
                raise ScheduleError(f"Invalid mode '{mode}'")
            setpoint_c: float | None = None
            if mode == "cool":
                setpoint_c = float(b["setpoint_c"])
                if not setpoint_min <= setpoint_c <= setpoint_max:
                    raise ScheduleError(
                        f"setpoint_c {setpoint_c} out of range [{setpoint_min}–{setpoint_max}]"
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

    parsed.sort(key=lambda b: b["start"])
    prev_end = time(0, 0)
    for b in parsed:
        if b["start"] != prev_end:
            raise ScheduleError(
                f"Zone '{zone_id}' {day_type}: gap or overlap at {b['start']} (expected {prev_end})"
            )
        prev_end = b["end"]

    return parsed


def parse_schedule_from_options(
    zone_dict: dict,
    setpoint_min: float,
    setpoint_max: float,
) -> dict[str, list[ScheduleBlock]]:
    """Parse a zone's schedule from entry.options. Empty lists are allowed."""
    raw = zone_dict.get("schedule", {})
    result: dict[str, list[ScheduleBlock]] = {}
    for day_type in ("weekday", "weekend"):
        blocks = raw.get(day_type, [])
        if not blocks:
            result[day_type] = []
            continue
        try:
            result[day_type] = _validate_zone_schedule(
                zone_dict.get("id", "unknown"), day_type, blocks, setpoint_min, setpoint_max
            )
        except ScheduleError as exc:
            LOGGER.warning(
                "Schedule error for zone %s %s: %s — treating as empty",
                zone_dict.get("id"), day_type, exc,
            )
            result[day_type] = []
    return result


def validate_block(
    block: dict,
    setpoint_min: float,
    setpoint_max: float,
) -> str | None:
    """Validate a single schedule block dict. Returns error string or None if valid."""
    try:
        raw_mode = block.get("mode", "")
        mode = str(raw_mode).lower()
        if mode not in ("off", "cool"):
            return f"Invalid mode '{mode}' — must be 'off' or 'cool'"
        start_str = block.get("start", "")
        end_str = block.get("end", "")
        if not start_str or not end_str:
            return "Start and end times are required"
        start = _parse_time(start_str)
        end = _parse_time(end_str)
        if end <= start:
            return "End time must be after start time"
        if mode == "cool":
            sp = block.get("setpoint_c")
            if sp is None:
                return "Setpoint is required for 'cool' mode"
            sp = float(sp)
            if not setpoint_min <= sp <= setpoint_max:
                return f"Setpoint {sp}°C out of range [{setpoint_min}–{setpoint_max}]"
    except ScheduleError as exc:
        return str(exc)
    except (ValueError, TypeError) as exc:
        return str(exc)
    return None


def get_block(
    schedules: dict[str, dict[str, list[ScheduleBlock]]],
    zone_id: str,
    now: datetime,
) -> tuple[str, float | None]:
    """Return (mode, setpoint_c) for zone at given time. ('off', None) if not scheduled."""
    if zone_id not in schedules:
        return "off", None

    day_type = "weekend" if now.weekday() >= 5 else "weekday"
    zone = schedules[zone_id]
    blocks = zone.get(day_type) or zone.get("weekday", [])

    if not blocks:
        return "off", None

    current_time = now.time().replace(second=0, microsecond=0)
    for block in blocks:
        if block["start"] <= current_time < block["end"]:
            return block["mode"], block["setpoint_c"]

    b = blocks[-1]
    return b["mode"], b["setpoint_c"]
