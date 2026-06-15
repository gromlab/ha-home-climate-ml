"""Schedule parsing for ClimateML."""
from __future__ import annotations

import re
from datetime import datetime, time
from typing import TypedDict

from .const import LOGGER


class ScheduleBlock(TypedDict):
    start: time
    end: time
    day_type: str   # "weekday" | "weekend"
    setpoint_c: float
    mode: str       # "eco" | "comfort"


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
    blocks: list[dict],
) -> list[ScheduleBlock]:
    parsed: list[ScheduleBlock] = []
    for i, b in enumerate(blocks):
        try:
            setpoint_c = float(b["setpoint_c"])
            if not (10.0 <= setpoint_c <= 32.0):
                raise ScheduleError(f"Invalid setpoint_c {setpoint_c} — must be 10–32°C")
            mode = str(b.get("mode", "eco"))
            if mode not in ("eco", "comfort", "off"):
                raise ScheduleError(f"Invalid mode '{mode}' — must be 'eco', 'comfort', or 'off'")
            day_type = str(b.get("day_type", "weekday"))
            if day_type not in ("weekday", "weekend", "both"):
                raise ScheduleError(f"Invalid day_type '{day_type}' — must be 'weekday', 'weekend', or 'both'")
            parsed.append({
                "start": _parse_time(b["start"]),
                "end": _parse_time(b["end"]),
                "day_type": day_type,
                "setpoint_c": setpoint_c,
                "mode": mode,
            })
        except ScheduleError:
            raise
        except Exception as exc:
            raise ScheduleError(
                f"Zone '{zone_id}' block {i}: {exc}"
            ) from exc

    parsed.sort(key=lambda b: (b["day_type"], b["start"]))

    # Overlap check: expand "both" blocks into weekday+weekend entries for collision detection
    expanded: list[ScheduleBlock] = []
    for b in parsed:
        if b["day_type"] == "both":
            expanded.append({**b, "day_type": "weekday"})
            expanded.append({**b, "day_type": "weekend"})
        else:
            expanded.append(b)
    expanded.sort(key=lambda b: (b["day_type"], b["start"]))

    for day_type in ("weekday", "weekend"):
        day_blocks = [b for b in expanded if b["day_type"] == day_type]
        for i in range(1, len(day_blocks)):
            if day_blocks[i]["start"] < day_blocks[i - 1]["end"]:
                raise ScheduleError(
                    f"Zone '{zone_id}' {day_type}: blocks overlap — "
                    f"{day_blocks[i - 1]['start']}–{day_blocks[i - 1]['end']} and "
                    f"{day_blocks[i]['start']}–{day_blocks[i]['end']}"
                )

    return parsed


def parse_schedule_from_options(zone_dict: dict) -> dict[str, list[ScheduleBlock]]:
    """Parse a zone's schedule from subentry data. Returns {'blocks': [...]}."""
    raw_blocks = zone_dict.get("schedule", {}).get("blocks", [])
    if not raw_blocks:
        return {"blocks": []}
    try:
        blocks = _validate_zone_schedule(zone_dict.get("id", "unknown"), raw_blocks)
        return {"blocks": blocks}
    except ScheduleError as exc:
        LOGGER.warning(
            "Schedule error for zone %s: %s — treating as empty",
            zone_dict.get("id"), exc,
        )
        return {"blocks": []}


def validate_block(block: dict) -> str | None:
    """Validate a single schedule block dict. Returns error string or None if valid."""
    try:
        setpoint_c = block.get("setpoint_c")
        if setpoint_c is None:
            return "Setpoint is required"
        sp = float(setpoint_c)
        if not (10.0 <= sp <= 32.0):
            return f"Setpoint must be 10–32°C, got {sp}"
        mode = block.get("mode", "eco")
        if mode not in ("eco", "comfort", "off"):
            return f"Mode must be 'eco', 'comfort', or 'off', got {mode}"
        day_type = block.get("day_type", "weekday")
        if day_type not in ("weekday", "weekend", "both"):
            return f"Day type must be 'weekday', 'weekend', or 'both', got {day_type}"
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
) -> ScheduleBlock | None:
    """Return active ScheduleBlock for zone at given time, or None if unscheduled."""
    if zone_id not in schedules:
        return None

    day_type = "weekend" if now.weekday() >= 5 else "weekday"
    blocks = schedules[zone_id].get("blocks", [])

    if not blocks:
        return None

    current_time = now.time().replace(second=0, microsecond=0)
    for block in blocks:
        if block["day_type"] in (day_type, "both") and block["start"] <= current_time < block["end"]:
            return block

    return None


def get_current_block_detail(
    schedules: dict[str, dict[str, list[ScheduleBlock]]],
    zone_id: str,
    now: datetime,
) -> ScheduleBlock | None:
    """Alias for get_block — returns the active ScheduleBlock or None."""
    return get_block(schedules, zone_id, now)


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

    blocks = schedules[zone_id].get("blocks", [])
    if not blocks:
        return None

    def _transitions_for_day(day: datetime) -> list[datetime]:
        day_type = "weekend" if day.weekday() >= 5 else "weekday"
        result = []
        for b in blocks:
            if b["day_type"] in (day_type, "both"):
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
