"""Schedule parsing for ClimateML."""
from __future__ import annotations

import re
from datetime import datetime, time
from typing import TypedDict

from .const import LOGGER


class ScheduleBlock(TypedDict):
    start: time
    end: time
    comfort_level: int


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
) -> list[ScheduleBlock]:
    parsed: list[ScheduleBlock] = []
    for i, b in enumerate(blocks):
        try:
            comfort_level = int(b["comfort_level"])
            if comfort_level not in range(1, 6):
                raise ScheduleError(f"Invalid comfort_level {comfort_level} — must be 1–5")
            parsed.append({
                "start": _parse_time(b["start"]),
                "end": _parse_time(b["end"]),
                "comfort_level": comfort_level,
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


def parse_schedule_from_options(zone_dict: dict) -> dict[str, list[ScheduleBlock]]:
    """Parse a zone's schedule from subentry data. Empty lists are allowed."""
    raw = zone_dict.get("schedule", {})
    result: dict[str, list[ScheduleBlock]] = {}
    for day_type in ("weekday", "weekend"):
        blocks = raw.get(day_type, [])
        if not blocks:
            result[day_type] = []
            continue
        try:
            result[day_type] = _validate_zone_schedule(
                zone_dict.get("id", "unknown"), day_type, blocks
            )
        except ScheduleError as exc:
            LOGGER.warning(
                "Schedule error for zone %s %s: %s — treating as empty",
                zone_dict.get("id"), day_type, exc,
            )
            result[day_type] = []
    return result


def validate_block(block: dict) -> str | None:
    """Validate a single schedule block dict. Returns error string or None if valid."""
    try:
        comfort_level = block.get("comfort_level")
        if comfort_level is None:
            return "Comfort level is required"
        if int(comfort_level) not in range(1, 6):
            return f"Comfort level must be 1–5, got {comfort_level}"
        start_str = block.get("start", "")
        end_str = block.get("end", "")
        if not start_str or not end_str:
            return "Start and end times are required"
        start = _parse_time(start_str)
        end = _parse_time(end_str)
        if end <= start:
            return "End time must be after start time"
    except ScheduleError as exc:
        return str(exc)
    except (ValueError, TypeError) as exc:
        return str(exc)
    return None


def get_block(
    schedules: dict[str, dict[str, list[ScheduleBlock]]],
    zone_id: str,
    now: datetime,
) -> int | None:
    """Return active comfort_level for zone at given time, or None if unscheduled."""
    if zone_id not in schedules:
        return None

    day_type = "weekend" if now.weekday() >= 5 else "weekday"
    zone = schedules[zone_id]
    blocks = zone.get(day_type) or zone.get("weekday", [])

    if not blocks:
        return None

    current_time = now.time().replace(second=0, microsecond=0)
    for block in blocks:
        if block["start"] <= current_time < block["end"]:
            return block["comfort_level"]

    return None


def get_current_block_detail(
    schedules: dict[str, dict[str, list[ScheduleBlock]]],
    zone_id: str,
    now: datetime,
) -> ScheduleBlock | None:
    """Return the active ScheduleBlock for zone at now, or None if unscheduled."""
    if zone_id not in schedules:
        return None

    day_type = "weekend" if now.weekday() >= 5 else "weekday"
    zone = schedules[zone_id]
    blocks = zone.get(day_type) or zone.get("weekday", [])

    current_time = now.time().replace(second=0, microsecond=0)
    for block in blocks:
        if block["start"] <= current_time < block["end"]:
            return block
    return None


def get_next_transition(
    schedules: dict[str, dict[str, list[ScheduleBlock]]],
    zone_id: str,
    now: datetime,
) -> datetime | None:
    """Return the next schedule transition time after now, or None if no schedule."""
    from datetime import timedelta
    from homeassistant.util import dt as dt_util

    if zone_id not in schedules:
        return None

    zone = schedules[zone_id]

    def _transitions_for_day(day: datetime) -> list[datetime]:
        day_type = "weekend" if day.weekday() >= 5 else "weekday"
        blocks = zone.get(day_type) or zone.get("weekday", [])
        result = []
        for b in blocks:
            for t in (b["start"], b["end"]):
                result.append(day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0))
        return result

    for delta_days in range(2):
        if delta_days == 0:
            check_day = now
        else:
            tomorrow = now + timedelta(days=1)
            check_day = dt_util.start_of_local_day(tomorrow)

        for candidate in sorted(_transitions_for_day(check_day)):
            if candidate > now:
                return candidate

    return None
