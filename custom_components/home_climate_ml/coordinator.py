"""Coordinator for Home Climate ML."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_SETPOINT_C,
    DOMAIN,
    EXTERNAL_SENSOR_MAX_C,
    EXTERNAL_SENSOR_MIN_C,
    FORCE_COOL_CLEAR_C,
    FORCE_COOL_THRESHOLD_C,
    HARD_ALERT_RATE_LIMIT_MIN,
    HARD_ALERT_THRESHOLD_C,
    LOGGER,
    OFFSET_CLAMP_C,
    OVERRIDE_DURATION_MINUTES,
    SENSE_ONLY,
    SETPOINT_MAX_C,
    SETPOINT_MIN_C,
    SETPOINT_TOLERANCE_C,
    ZONES,
)
from .schedule import ScheduleBlock, get_block
from .store import ClimateDataStore


class ZoneData(TypedDict):
    ext_temp_c: float | None
    head_temp_c: float | None
    offset_c: float | None
    target_setpoint_c: float
    target_mode: str
    corrected_setpoint_c: float | None
    override_active: bool
    schedule_source: str
    last_command_c: float | None
    error: str | None
    enabled: bool


class HomeClimateMlCoordinator(DataUpdateCoordinator[dict[str, ZoneData]]):
    def __init__(
        self,
        hass: HomeAssistant,
        store: ClimateDataStore,
        schedules: dict,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self._store = store
        self._schedules = schedules
        self._overrides: dict[str, dict] = {}  # zone_id → {setpoint_c, mode, expires_at, unsub}
        self._force_cool_active: set[str] = set()
        self._last_hard_alert: dict[str, datetime] = {}
        self._last_sensor_values: dict[str, Any] = {}
        self._zone_enabled: dict[str, bool] = {zone_id: True for zone_id in ZONES}

    # ------------------------------------------------------------------ zone enable

    @property
    def zone_enabled(self) -> dict[str, bool]:
        return self._zone_enabled

    def set_zone_enabled(self, zone_id: str, enabled: bool) -> None:
        self._zone_enabled[zone_id] = enabled
        LOGGER.debug("Zone %s enabled=%s", zone_id, enabled)

    def set_all_zones_enabled(self, enabled: bool) -> None:
        for zone_id in ZONES:
            self._zone_enabled[zone_id] = enabled
        LOGGER.info("All zones enabled=%s", enabled)

    # ------------------------------------------------------------------ overrides

    def set_override(self, zone_id: str, setpoint_c: float | None, mode: str) -> None:
        expires_at = dt_util.utcnow() + timedelta(minutes=OVERRIDE_DURATION_MINUTES)
        self._cancel_override_timer(zone_id)
        self._overrides[zone_id] = {
            "setpoint_c": setpoint_c,
            "mode": mode,
            "expires_at": expires_at,
            "unsub": async_track_point_in_time(
                self.hass, lambda _now, zid=zone_id: self._expire_override(zid), expires_at
            ),
        }
        self.hass.async_add_executor_job(
            self._store.open_override, zone_id, setpoint_c, mode, expires_at
        )

    @callback
    def _expire_override(self, zone_id: str) -> None:
        self._overrides.pop(zone_id, None)
        self.hass.async_add_executor_job(self._store.close_override, zone_id, "timeout")
        LOGGER.debug("Override expired for zone %s", zone_id)
        self.hass.async_create_task(self.async_refresh())

    def _cancel_override_timer(self, zone_id: str) -> None:
        if existing := self._overrides.get(zone_id):
            if unsub := existing.get("unsub"):
                unsub()

    async def restore_overrides(self) -> None:
        rows = await self.hass.async_add_executor_job(self._store.restore_active_overrides)
        now = dt_util.utcnow()
        for row in rows:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                await self.hass.async_add_executor_job(
                    self._store.close_override, row["zone_id"], "timeout"
                )
                continue
            zone_id = row["zone_id"]
            self._overrides[zone_id] = {
                "setpoint_c": row["setpoint_c"],
                "mode": row["mode"],
                "expires_at": expires_at,
                "unsub": async_track_point_in_time(
                    self.hass,
                    lambda _now, zid=zone_id: self._expire_override(zid),
                    expires_at,
                ),
            }
            LOGGER.debug("Restored override for zone %s, expires %s", zone_id, expires_at)

    # ------------------------------------------------------------------ decision loop

    async def _async_update_data(self) -> dict[str, ZoneData]:
        now = dt_util.now()
        results: dict[str, ZoneData] = {}
        all_failed = True

        for zone_id, zone_cfg in ZONES.items():
            try:
                zone_data = await self._process_zone(zone_id, zone_cfg, now)
                results[zone_id] = zone_data
                all_failed = False
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Zone %s decision failed: %s", zone_id, exc)
                results[zone_id] = ZoneData(
                    ext_temp_c=None, head_temp_c=None, offset_c=None,
                    target_setpoint_c=DEFAULT_SETPOINT_C, target_mode="off",
                    corrected_setpoint_c=None, override_active=False,
                    schedule_source="error", last_command_c=None, error=str(exc),
                    enabled=self._zone_enabled.get(zone_id, True),
                )

        # Log sense-only zones (no commands)
        for sense_id, sensor_entity in SENSE_ONLY.items():
            await self._log_sense_zone(sense_id, sensor_entity)

        if all_failed:
            raise UpdateFailed("All zones failed in this decision cycle")

        return results

    async def _process_zone(
        self, zone_id: str, zone_cfg: dict, now: datetime
    ) -> ZoneData:
        if not self._zone_enabled.get(zone_id, True):
            return ZoneData(
                ext_temp_c=None, head_temp_c=None, offset_c=None,
                target_setpoint_c=DEFAULT_SETPOINT_C, target_mode="off",
                corrected_setpoint_c=None, override_active=False,
                schedule_source="disabled", last_command_c=None, error=None,
                enabled=False,
            )

        external_entity = zone_cfg["external"]
        head_entity = zone_cfg["head"]

        # Read temperatures from HA state machine
        ext_state = self.hass.states.get(external_entity)
        head_state = self.hass.states.get(head_entity)

        ext_temp_c: float | None = None
        head_temp_c: float | None = None
        ext_valid = False

        if ext_state and ext_state.state not in ("unavailable", "unknown"):
            try:
                val = float(ext_state.state)
                if EXTERNAL_SENSOR_MIN_C <= val <= EXTERNAL_SENSOR_MAX_C:
                    ext_temp_c = val
                    ext_valid = True
                    await self._maybe_log_sensor(zone_id, "external", "temperature", val)
                else:
                    LOGGER.warning("Zone %s external sensor out of range: %s°C", zone_id, val)
            except (ValueError, TypeError):
                LOGGER.warning("Zone %s external sensor non-numeric: %s", zone_id, ext_state.state)

        if head_state and head_state.state not in ("unavailable", "unknown"):
            try:
                head_temp_c = float(head_state.attributes.get("current_temperature", "nan"))
                await self._maybe_log_sensor(zone_id, "head", "temperature", head_temp_c)
            except (ValueError, TypeError):
                head_temp_c = None

        # Determine target from override or schedule
        override = self._overrides.get(zone_id)
        if override:
            target_mode = override["mode"]
            target_setpoint_c = override["setpoint_c"] or DEFAULT_SETPOINT_C
            schedule_source = "override"
        else:
            sched_mode, sched_setpoint = get_block(self._schedules, zone_id, now)
            target_mode = sched_mode
            target_setpoint_c = sched_setpoint or DEFAULT_SETPOINT_C
            schedule_source = "schedule"

        # Safety check (uses per-zone external sensor)
        if ext_valid and ext_temp_c is not None:
            if ext_temp_c >= HARD_ALERT_THRESHOLD_C:
                await self._handle_hard_alert(zone_id, ext_temp_c)
                target_mode = "cool"
                target_setpoint_c = SETPOINT_MIN_C
                schedule_source = "safety"
            elif ext_temp_c >= FORCE_COOL_THRESHOLD_C:
                if zone_id not in self._force_cool_active:
                    self._force_cool_active.add(zone_id)
                    await self.hass.async_add_executor_job(
                        self._store.log_safety_event,
                        zone_id, "warning",
                        f"Force cool activated: ext_temp={ext_temp_c}°C >= {FORCE_COOL_THRESHOLD_C}°C",
                    )
                target_mode = "cool"
                target_setpoint_c = SETPOINT_MIN_C
                schedule_source = "safety"
            elif ext_temp_c < FORCE_COOL_CLEAR_C and zone_id in self._force_cool_active:
                self._force_cool_active.discard(zone_id)
                LOGGER.info("Zone %s force cool cleared (ext_temp=%.1f°C)", zone_id, ext_temp_c)

        # Execute
        command_issued = False
        corrected_setpoint_c: float | None = None
        offset_c: float | None = None

        if target_mode == "off":
            await self.hass.services.async_call(
                "climate", "turn_off", {"entity_id": head_entity}, blocking=True
            )
            command_issued = True
        else:
            # Compute corrected setpoint
            if ext_valid and ext_temp_c is not None and head_temp_c is not None:
                raw_offset = head_temp_c - ext_temp_c
                offset_c = max(-OFFSET_CLAMP_C, min(OFFSET_CLAMP_C, raw_offset))
            else:
                offset_c = 0.0

            corrected_setpoint_c = max(
                SETPOINT_MIN_C, min(SETPOINT_MAX_C, target_setpoint_c + offset_c)
            )

            # Check against current head setpoint (tolerance gate)
            current_head_setpoint: float | None = None
            if head_state:
                try:
                    current_head_setpoint = float(head_state.attributes.get("temperature", "nan"))
                except (ValueError, TypeError):
                    current_head_setpoint = None

            should_command = (
                current_head_setpoint is None
                or abs(corrected_setpoint_c - current_head_setpoint) >= SETPOINT_TOLERANCE_C
            )

            if should_command:
                # Set hvac_mode to cool first (handles heads that are off)
                if head_state and head_state.state != "cool":
                    await self.hass.services.async_call(
                        "climate", "set_hvac_mode",
                        {"entity_id": head_entity, "hvac_mode": "cool"},
                        blocking=True,
                    )
                await self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": head_entity, "temperature": corrected_setpoint_c},
                    blocking=True,
                )
                command_issued = True

        # Persist decision
        decision_id = await self.hass.async_add_executor_job(
            self._store.log_decision,
            zone_id, schedule_source, target_mode, target_setpoint_c,
            ext_temp_c, head_temp_c, offset_c, corrected_setpoint_c, command_issued,
        )
        if command_issued:
            payload = f"mode={target_mode},setpoint={corrected_setpoint_c}"
            await self.hass.async_add_executor_job(
                self._store.log_device_command,
                zone_id, "set_temperature" if target_mode != "off" else "turn_off",
                payload, decision_id,
            )

        return ZoneData(
            ext_temp_c=ext_temp_c,
            head_temp_c=head_temp_c,
            offset_c=offset_c,
            target_setpoint_c=target_setpoint_c,
            target_mode=target_mode,
            corrected_setpoint_c=corrected_setpoint_c,
            override_active=bool(override),
            schedule_source=schedule_source,
            last_command_c=corrected_setpoint_c if command_issued else None,
            error=None,
            enabled=True,
        )

    async def _handle_hard_alert(self, zone_id: str, ext_temp_c: float) -> None:
        now = dt_util.utcnow()
        last = self._last_hard_alert.get(zone_id)
        if last and (now - last).total_seconds() < HARD_ALERT_RATE_LIMIT_MIN * 60:
            return
        self._last_hard_alert[zone_id] = now
        LOGGER.error(
            "HARD ALERT zone %s: ext_temp=%.1f°C >= %.1f°C — forcing all cooling",
            zone_id, ext_temp_c, HARD_ALERT_THRESHOLD_C,
        )
        await self.hass.async_add_executor_job(
            self._store.log_safety_event,
            zone_id, "hard_alert",
            f"Temperature critical: {ext_temp_c}°C >= {HARD_ALERT_THRESHOLD_C}°C",
        )

    async def _maybe_log_sensor(
        self, zone_id: str, source: str, metric: str, value: float
    ) -> None:
        key = f"{zone_id}:{source}:{metric}"
        prev = self._last_sensor_values.get(key)
        if prev != value:
            self._last_sensor_values[key] = value
            await self.hass.async_add_executor_job(
                self._store.log_sensor_event, zone_id, source, metric, value, None
            )

    async def _log_sense_zone(self, zone_id: str, entity_id: str) -> None:
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unavailable", "unknown"):
            try:
                val = float(state.state)
                await self._maybe_log_sensor(zone_id, "sense", "temperature", val)
            except (ValueError, TypeError):
                pass
