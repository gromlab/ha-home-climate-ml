"""Coordinator for ClimateML."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EXTERNAL_SENSOR_MAX_C, EXTERNAL_SENSOR_MIN_C, LOGGER
from .schedule import ScheduleBlock, get_block, parse_schedule_from_options
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
        entry_id: str,
        store: ClimateDataStore,
        zones: list[dict],
        sense_only: dict[str, str],
        update_interval: timedelta,
        params: dict,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self._entry_id = entry_id
        self._store = store
        self._zones = zones
        self._sense_only = sense_only
        self.params = params

        setpoint_min = params["setpoint_min_c"]
        setpoint_max = params["setpoint_max_c"]
        self._schedules: dict[str, dict[str, list[ScheduleBlock]]] = {
            z["id"]: parse_schedule_from_options(z, setpoint_min, setpoint_max)
            for z in zones
        }

        self._overrides: dict[str, dict] = {}
        self._force_cool_active: set[str] = set()
        self._last_hard_alert: dict[str, datetime] = {}
        self._last_sensor_values: dict[str, Any] = {}
        self._zone_enabled: dict[str, bool] = {z["id"]: True for z in zones}

        # Master enable: survives coordinator reload via hass.data; resets to OFF on HA restart
        hass.data.setdefault(DOMAIN, {})
        self._master_enabled: bool = hass.data[DOMAIN].get(f"{entry_id}_master", False)

    @property
    def zones(self) -> list[dict]:
        return self._zones

    @property
    def zone_enabled(self) -> dict[str, bool]:
        return self._zone_enabled

    # ------------------------------------------------------------------ master / zone enable

    def set_master_enabled(self, enabled: bool) -> None:
        self._master_enabled = enabled
        self.hass.data[DOMAIN][f"{self._entry_id}_master"] = enabled
        LOGGER.info("Master enabled=%s", enabled)

    def set_zone_enabled(self, zone_id: str, enabled: bool) -> None:
        self._zone_enabled[zone_id] = enabled
        LOGGER.debug("Zone %s enabled=%s", zone_id, enabled)

    # ------------------------------------------------------------------ overrides

    def set_override(self, zone_id: str, setpoint_c: float | None, mode: str) -> None:
        override_minutes = self.params.get("override_duration_minutes", 120)
        expires_at = dt_util.utcnow() + timedelta(minutes=override_minutes)
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

        for zone in self._zones:
            zone_id = zone["id"]
            try:
                zone_data = await self._process_zone(zone_id, zone, now)
                results[zone_id] = zone_data
                all_failed = False
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Zone %s decision failed: %s", zone_id, exc)
                results[zone_id] = ZoneData(
                    ext_temp_c=None, head_temp_c=None, offset_c=None,
                    target_setpoint_c=self.params.get("default_setpoint_c", 21.0),
                    target_mode="off", corrected_setpoint_c=None, override_active=False,
                    schedule_source="error", last_command_c=None, error=str(exc),
                    enabled=self._master_enabled and self._zone_enabled.get(zone_id, True),
                )

        for sense_id, sensor_entity in self._sense_only.items():
            await self._log_sense_zone(sense_id, sensor_entity)

        if all_failed and self._zones:
            raise UpdateFailed("All zones failed in this decision cycle")

        return results

    async def _process_zone(self, zone_id: str, zone: dict, now: datetime) -> ZoneData:
        if not (self._master_enabled and self._zone_enabled.get(zone_id, True)):
            return ZoneData(
                ext_temp_c=None, head_temp_c=None, offset_c=None,
                target_setpoint_c=self.params.get("default_setpoint_c", 21.0),
                target_mode="off", corrected_setpoint_c=None, override_active=False,
                schedule_source="disabled", last_command_c=None, error=None,
                enabled=False,
            )

        head_entity = zone["head_entity"]
        external_entity = zone["sensor_entity"]

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

        default_setpoint = self.params.get("default_setpoint_c", 21.0)
        override = self._overrides.get(zone_id)
        if override:
            target_mode = override["mode"]
            target_setpoint_c = override["setpoint_c"] or default_setpoint
            schedule_source = "override"
        else:
            sched_mode, sched_setpoint = get_block(self._schedules, zone_id, now)
            target_mode = sched_mode
            target_setpoint_c = sched_setpoint or default_setpoint
            schedule_source = "schedule"

        force_cool_threshold = self.params.get("force_cool_threshold_c", 30.0)
        force_cool_clear = self.params.get("force_cool_clear_c", 28.0)
        setpoint_min = self.params.get("setpoint_min_c", 16.0)
        setpoint_max = self.params.get("setpoint_max_c", 28.0)
        offset_clamp = self.params.get("offset_clamp_c", 5.0)
        tolerance = self.params.get("setpoint_tolerance_c", 0.5)

        if ext_valid and ext_temp_c is not None:
            if ext_temp_c >= force_cool_threshold:
                if zone_id not in self._force_cool_active:
                    self._force_cool_active.add(zone_id)
                    await self.hass.async_add_executor_job(
                        self._store.log_safety_event, zone_id, "warning",
                        f"Force cool: ext_temp={ext_temp_c}°C >= {force_cool_threshold}°C",
                    )
                target_mode = "cool"
                target_setpoint_c = setpoint_min
                schedule_source = "safety"
            elif ext_temp_c < force_cool_clear and zone_id in self._force_cool_active:
                self._force_cool_active.discard(zone_id)
                LOGGER.info("Zone %s force cool cleared (ext_temp=%.1f°C)", zone_id, ext_temp_c)

        command_issued = False
        corrected_setpoint_c: float | None = None
        offset_c: float | None = None

        if target_mode == "off":
            await self.hass.services.async_call(
                "climate", "turn_off", {"entity_id": head_entity}, blocking=True
            )
            command_issued = True
        else:
            if ext_valid and ext_temp_c is not None and head_temp_c is not None:
                raw_offset = head_temp_c - ext_temp_c
                offset_c = max(-offset_clamp, min(offset_clamp, raw_offset))
            else:
                offset_c = 0.0

            corrected_setpoint_c = max(setpoint_min, min(setpoint_max, target_setpoint_c + offset_c))

            current_head_setpoint: float | None = None
            if head_state:
                try:
                    current_head_setpoint = float(head_state.attributes.get("temperature", "nan"))
                except (ValueError, TypeError):
                    current_head_setpoint = None

            should_command = (
                current_head_setpoint is None
                or abs(corrected_setpoint_c - current_head_setpoint) >= tolerance
            )

            if should_command:
                if head_state and head_state.state != "cool":
                    await self.hass.services.async_call(
                        "climate", "set_hvac_mode",
                        {"entity_id": head_entity, "hvac_mode": "cool"}, blocking=True,
                    )
                await self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": head_entity, "temperature": corrected_setpoint_c}, blocking=True,
                )
                command_issued = True

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
            ext_temp_c=ext_temp_c, head_temp_c=head_temp_c, offset_c=offset_c,
            target_setpoint_c=target_setpoint_c, target_mode=target_mode,
            corrected_setpoint_c=corrected_setpoint_c, override_active=bool(override),
            schedule_source=schedule_source,
            last_command_c=corrected_setpoint_c if command_issued else None,
            error=None, enabled=True,
        )

    async def _maybe_log_sensor(self, zone_id: str, source: str, metric: str, value: float) -> None:
        key = f"{zone_id}:{source}:{metric}"
        prev = self._last_sensor_values.get(key)
        if prev != value:
            self._last_sensor_values[key] = value
            await self.hass.async_add_executor_job(
                self._store.log_sensor_event, zone_id, source, metric, value, None
            )

    async def _log_sense_zone(self, zone_id: str, entity_id: str) -> None:
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unavailable", "unknown"):
            try:
                val = float(state.state)
                await self._maybe_log_sensor(zone_id, "sense", "temperature", val)
            except (ValueError, TypeError):
                pass
