"""Coordinator for ClimateML."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EXTERNAL_SENSOR_MAX_C, EXTERNAL_SENSOR_MIN_C, LOGGER
from .schedule import ScheduleBlock, get_block, get_current_block_detail, get_next_transition, parse_schedule_from_options
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
        update_interval: timedelta,
        params: dict,
        controller_config: dict | None = None,
        controller_subentry_id: str | None = None,
        zone_subentry_map: dict[str, str] | None = None,
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
        self.params = params
        self._controller_config: dict = controller_config or {}
        self._controller_subentry_id = controller_subentry_id
        self.zone_subentry_map: dict[str, str] = zone_subentry_map or {}

        setpoint_min = params["setpoint_min_c"]
        setpoint_max = params["setpoint_max_c"]
        self._schedules: dict[str, dict[str, list[ScheduleBlock]]] = {
            z["id"]: parse_schedule_from_options(z, setpoint_min, setpoint_max)
            for z in zones
        }

        self._overrides: dict[str, dict] = {}
        self._last_sensor_values: dict[str, Any] = {}
        self._zone_enabled: dict[str, bool] = {z["id"]: True for z in zones}
        self._decision_log: dict[str, deque] = {z["id"]: deque(maxlen=30) for z in zones}

        # Master enable: survives coordinator reload via hass.data; resets to OFF on HA restart
        hass.data.setdefault(DOMAIN, {})
        self._master_enabled: bool = hass.data[DOMAIN].get(f"{entry_id}_master", False)

    @property
    def zones(self) -> list[dict]:
        return self._zones

    @property
    def controller_subentry_id(self) -> str | None:
        return self._controller_subentry_id

    @property
    def schedules(self) -> dict[str, dict[str, list[ScheduleBlock]]]:
        return self._schedules

    def get_decision_log(self, zone_id: str) -> list[dict]:
        """Return recent decisions for zone, newest first."""
        return list(reversed(list(self._decision_log.get(zone_id, []))))

    def get_current_block(self, zone_id: str) -> ScheduleBlock | None:
        now = dt_util.now()
        return get_current_block_detail(self._schedules, zone_id, now)

    def get_next_transition(self, zone_id: str) -> datetime | None:
        now = dt_util.now()
        return get_next_transition(self._schedules, zone_id, now)

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

        # --- Sensor guard: collect all valid zone temp readings + extra controller temps ---
        zone_temps: dict[str, float] = {}
        for zone in self._zones:
            zone_id = zone["id"]
            if not (self._master_enabled and self._zone_enabled.get(zone_id, True)):
                continue
            state = self.hass.states.get(zone["sensor_entity"])
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    val = float(state.state)
                    if EXTERNAL_SENSOR_MIN_C <= val <= EXTERNAL_SENSOR_MAX_C:
                        zone_temps[zone_id] = val
                except (ValueError, TypeError):
                    pass

        extra_temps: list[float] = []
        for entity_id in self._controller_config.get("extra_temp_entities") or []:
            state = self.hass.states.get(entity_id)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    val = float(state.state)
                    if EXTERNAL_SENSOR_MIN_C <= val <= EXTERNAL_SENSOR_MAX_C:
                        extra_temps.append(val)
                except (ValueError, TypeError):
                    pass

        all_temps = list(zone_temps.values()) + extra_temps
        guard_active = len(all_temps) >= 2
        guard_avg = sum(all_temps) / len(all_temps) if all_temps else None
        guard_threshold = self.params.get("sensor_guard_threshold_c", 3.0)

        # --- Per-zone decision loop ---
        for zone in self._zones:
            zone_id = zone["id"]
            try:
                zone_data = await self._process_zone(
                    zone_id, zone, now, guard_avg, guard_threshold, guard_active
                )
                results[zone_id] = zone_data
                all_failed = False
                self._decision_log[zone_id].append({
                    "time": now.isoformat(timespec="seconds"),
                    "mode": zone_data["target_mode"],
                    "scheduled_c": zone_data.get("target_setpoint_c"),
                    "corrected_c": zone_data.get("corrected_setpoint_c"),
                    "source": zone_data["schedule_source"],
                    "ext_temp_c": zone_data.get("ext_temp_c"),
                    "offset_c": zone_data.get("offset_c"),
                    "commanded": zone_data.get("last_command_c") is not None,
                })
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Zone %s decision failed: %s", zone_id, exc)
                results[zone_id] = ZoneData(
                    ext_temp_c=None, head_temp_c=None, offset_c=None,
                    target_setpoint_c=self.params.get("default_setpoint_c", 21.0),
                    target_mode="off", corrected_setpoint_c=None, override_active=False,
                    schedule_source="error", last_command_c=None, error=str(exc),
                    enabled=self._master_enabled and self._zone_enabled.get(zone_id, True),
                )

        # --- System-level data gathering (sun, ODU, weather) ---
        await self._gather_system_data(now)

        if all_failed and self._zones:
            raise UpdateFailed("All zones failed in this decision cycle")

        return results

    async def _process_zone(
        self,
        zone_id: str,
        zone: dict,
        now: datetime,
        guard_avg: float | None,
        guard_threshold: float,
        guard_active: bool,
    ) -> ZoneData:
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

        # Log EVA in/out if configured
        for eva_key, eva_label in (("eva_in_entity", "eva_in"), ("eva_out_entity", "eva_out")):
            eva_entity = zone.get(eva_key, "")
            if eva_entity:
                eva_state = self.hass.states.get(eva_entity)
                if eva_state and eva_state.state not in ("unavailable", "unknown"):
                    try:
                        await self._maybe_log_sensor(zone_id, eva_label, "temperature", float(eva_state.state))
                    except (ValueError, TypeError):
                        pass

        # Log occupancy if configured (binary: 1 = occupied, 0 = unoccupied)
        occupancy_entity = zone.get("occupancy_entity", "")
        if occupancy_entity:
            occ_state = self.hass.states.get(occupancy_entity)
            if occ_state and occ_state.state not in ("unavailable", "unknown"):
                await self._maybe_log_sensor(zone_id, "occupancy", "presence", 1.0 if occ_state.state == "on" else 0.0)

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

        setpoint_min = self.params.get("setpoint_min_c", 16.0)
        setpoint_max = self.params.get("setpoint_max_c", 28.0)
        tolerance = self.params.get("setpoint_tolerance_c", 0.5)

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
                # Sensor guard: if this zone's sensor deviates too far from the peer average,
                # treat the reading as suspect and apply zero offset.
                if (
                    guard_active
                    and guard_avg is not None
                    and abs(ext_temp_c - guard_avg) > guard_threshold
                ):
                    LOGGER.warning(
                        "Zone %s sensor guard triggered: %.1f°C vs peer avg %.1f°C (threshold %.1f°C) — offset zeroed",
                        zone_id, ext_temp_c, guard_avg, guard_threshold,
                    )
                    offset_c = 0.0
                else:
                    offset_c = raw_offset
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

    async def _gather_system_data(self, now: datetime) -> None:
        """Log sun, ODU, outdoor temp, and weather data for future ML."""
        # Sun is always available
        sun_state = self.hass.states.get("sun.sun")
        if sun_state:
            elev = sun_state.attributes.get("elevation")
            azim = sun_state.attributes.get("azimuth")
            if elev is not None:
                await self._maybe_log_sensor("_system", "sun", "elevation", float(elev))
            if azim is not None:
                await self._maybe_log_sensor("_system", "sun", "azimuth", float(azim))

        # ODU mode
        odu_entity = self._controller_config.get("odu_mode_entity", "")
        if odu_entity:
            odu_state = self.hass.states.get(odu_entity)
            if odu_state and odu_state.state not in ("unavailable", "unknown"):
                await self._maybe_log_sensor("_system", "odu", "mode", 0)  # state is string, just trigger log

        # Outdoor temp from controller
        outdoor_entity = self._controller_config.get("outdoor_temp_entity", "")
        if outdoor_entity:
            state = self.hass.states.get(outdoor_entity)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    await self._maybe_log_sensor("_system", "outdoor", "temperature", float(state.state))
                except (ValueError, TypeError):
                    pass

        # Power
        power_entity = self._controller_config.get("power_entity", "")
        if power_entity:
            state = self.hass.states.get(power_entity)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    await self._maybe_log_sensor("_system", "hvac", "power_w", float(state.state))
                except (ValueError, TypeError):
                    pass

        # Weather forecast (stored in memory for future ML, not exposed as entity state yet)
        weather_entity = self._controller_config.get("weather_entity", "")
        if weather_entity:
            try:
                response = await self.hass.services.async_call(
                    "weather", "get_forecasts",
                    {"entity_id": weather_entity, "type": "hourly"},
                    blocking=True,
                    return_response=True,
                )
                if isinstance(response, dict):
                    forecast = response.get(weather_entity, {}).get("forecast", [])[:12]
                    if forecast:
                        LOGGER.debug("Weather forecast fetched: %d hourly entries", len(forecast))
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Weather forecast unavailable: %s", exc)

    async def _maybe_log_sensor(self, zone_id: str, source: str, metric: str, value: float) -> None:
        key = f"{zone_id}:{source}:{metric}"
        prev = self._last_sensor_values.get(key)
        if prev != value:
            self._last_sensor_values[key] = value
            if zone_id != "_system":
                await self.hass.async_add_executor_job(
                    self._store.log_sensor_event, zone_id, source, metric, value, None
                )

