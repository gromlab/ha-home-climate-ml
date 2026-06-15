"""Coordinator for ClimateML."""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONTROLLER_DEFAULTS,
    DEFAULT_SETPOINT_C,
    DOMAIN,
    EXTERNAL_SENSOR_MAX_C,
    EXTERNAL_SENSOR_MIN_C,
    LOGGER,
    STARVATION_COOLDOWN_SECONDS,
    STARVATION_DEMAND_THRESHOLD,
    STARVATION_MIN_SAMPLES,
    STARVATION_SUPPRESS_SECONDS,
    STARVATION_THERMAL_TARGET,
    STARVATION_WATCH_SECONDS,
    VACATION_SETPOINT_DEFAULT,
)
from .schedule import ScheduleBlock, get_block, get_current_block_detail, get_next_transition, parse_schedule_from_options
from .store import ClimateDataStore


class ZoneData(TypedDict):
    ext_temp_c: float | None
    head_temp_c: float | None
    offset_c: float | None
    active_setpoint_c: float | None
    setpoint_source: str
    active_mode: str
    mode_source: str
    commanded_setpoint: float | None
    command_issued: bool
    override_active: bool
    idle: bool
    starvation_suppressed: bool
    error: str | None
    enabled: bool
    ml_predicted_action: str | None
    ml_confidence: float | None


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

        self._schedules: dict[str, dict[str, list[ScheduleBlock]]] = {
            z["id"]: parse_schedule_from_options(z)
            for z in zones
        }

        self._vacation_setpoint_c: float = float(
            self._controller_config.get("vacation_setpoint_c", VACATION_SETPOINT_DEFAULT)
        )

        # Zone default setpoints and modes: seeded from subentry, runtime changes via hass.data
        hass.data.setdefault(DOMAIN, {})
        self._zone_default_setpoint_c: dict[str, float] = {}
        self._zone_default_mode: dict[str, str] = {}
        for z in zones:
            zid = z["id"]
            cold_sp = float(z.get("default_setpoint_c", DEFAULT_SETPOINT_C))
            cold_mode = str(z.get("default_mode", "eco"))
            # Restore runtime value from hass.data (survives coordinator reload)
            self._zone_default_setpoint_c[zid] = hass.data[DOMAIN].get(
                f"{entry_id}_default_setpoint_{zid}", cold_sp
            )
            self._zone_default_mode[zid] = hass.data[DOMAIN].get(
                f"{entry_id}_default_mode_{zid}", cold_mode
            )

        self._overrides: dict[str, dict] = {}
        self._last_sensor_values: dict[str, Any] = {}

        # Zone enable state — survives coordinator reload via hass.data; reset on cold HA restart
        self._master_enabled: bool = hass.data[DOMAIN].get(f"{entry_id}_master", False)
        self._vacation_mode: bool = hass.data[DOMAIN].get(f"{entry_id}_vacation", False)
        self._zone_enabled: dict[str, bool] = {z["id"]: True for z in zones}

        self._decision_log: dict[str, deque] = {z["id"]: deque(maxlen=30) for z in zones}

        # dT/dt tracking: deque of (epoch_seconds, temp_c) tuples, newest at index 0
        self._zone_last_temps: dict[str, deque] = {z["id"]: deque(maxlen=3) for z in zones}


        # Starvation logic state (in-memory only; resets to inactive on HA restart — intentional fail-open)
        # REVIEW: single-priority-zone — supports exactly one priority zone
        self._starvation: dict[str, Any] = {
            "watch_start": None,       # datetime | None
            "active": False,           # bool
            "suppression_start": None, # datetime | None
            "cooldown_until": None,    # datetime | None
        }
        # maxlen=9: covers 40 min at 5-min ticks, tolerating 1-2 missed ticks in 30-min window
        # REVIEW: single-priority-zone
        self._zone_thermal_delta_history: dict[str, deque] = {
            z["id"]: deque(maxlen=9) for z in zones
        }

        # ML shadow mode
        self._ml_model: Any | None = None
        self._zone_ml_confidence: dict[str, float] = {z["id"]: 0.0 for z in zones}

    # ------------------------------------------------------------------ properties

    @property
    def zones(self) -> list[dict]:
        return self._zones

    @property
    def controller_subentry_id(self) -> str | None:
        return self._controller_subentry_id

    @property
    def schedules(self) -> dict[str, dict[str, list[ScheduleBlock]]]:
        return self._schedules

    @property
    def vacation_mode(self) -> bool:
        return self._vacation_mode

    # ------------------------------------------------------------------ decision log helpers

    def get_decision_log(self, zone_id: str) -> list[dict]:
        return list(reversed(list(self._decision_log.get(zone_id, []))))

    def get_current_block(self, zone_id: str) -> ScheduleBlock | None:
        return get_current_block_detail(self._schedules, zone_id, dt_util.now())

    def get_next_transition(self, zone_id: str) -> datetime | None:
        return get_next_transition(self._schedules, zone_id, dt_util.now())

    def get_zone_default_setpoint(self, zone_id: str) -> float:
        return self._zone_default_setpoint_c.get(zone_id, DEFAULT_SETPOINT_C)

    def get_zone_default_mode(self, zone_id: str) -> str:
        return self._zone_default_mode.get(zone_id, "eco")

    def get_zone_ml_confidence(self, zone_id: str) -> float:
        return self._zone_ml_confidence.get(zone_id, 0.0)

    # ------------------------------------------------------------------ master / zone enable

    def set_master_enabled(self, enabled: bool) -> None:
        self._master_enabled = enabled
        self.hass.data[DOMAIN][f"{self._entry_id}_master"] = enabled
        LOGGER.info("Master enabled=%s", enabled)

    def set_zone_enabled(self, zone_id: str, enabled: bool) -> None:
        self._zone_enabled[zone_id] = enabled
        LOGGER.debug("Zone %s enabled=%s", zone_id, enabled)

    def set_vacation_mode(self, active: bool) -> None:
        self._vacation_mode = active
        self.hass.data[DOMAIN][f"{self._entry_id}_vacation"] = active
        LOGGER.info("Vacation mode=%s", active)

    def is_zone_enabled(self, zone_id: str) -> bool:
        return self._zone_enabled.get(zone_id, True)

    def set_zone_default_setpoint(self, zone_id: str, sp: float) -> None:
        self._zone_default_setpoint_c[zone_id] = sp
        self.hass.data[DOMAIN][f"{self._entry_id}_default_setpoint_{zone_id}"] = sp
        LOGGER.debug("Zone %s default setpoint set to %.1f", zone_id, sp)

    def set_zone_default_mode(self, zone_id: str, mode: str) -> None:
        self._zone_default_mode[zone_id] = mode
        self.hass.data[DOMAIN][f"{self._entry_id}_default_mode_{zone_id}"] = mode
        LOGGER.debug("Zone %s default mode set to %s", zone_id, mode)

    def set_ml_model(self, model: Any) -> None:
        self._ml_model = model
        LOGGER.info("ML model loaded: %s", type(model).__name__)

    # ------------------------------------------------------------------ override helpers

    def has_active_override(self, zone_id: str) -> bool:
        return zone_id in self._overrides

    def cancel_override(self, zone_id: str) -> None:
        self._cancel_override_timer(zone_id)
        self._overrides.pop(zone_id, None)
        self.hass.async_add_executor_job(self._store.close_override, zone_id, "cancelled")
        LOGGER.debug("Override cancelled for zone %s", zone_id)

    # ------------------------------------------------------------------ setpoint resolution

    def _resolve_setpoint(
        self, zone_id: str, now: datetime
    ) -> tuple[float, str, str, str]:
        """Return (setpoint_c, setpoint_source, mode, mode_source)."""
        if self._vacation_mode:
            return (self._vacation_setpoint_c, "vacation", "comfort", "vacation")

        if zone_id in self._overrides:
            ov = self._overrides[zone_id]
            return (
                float(ov.get("setpoint_c", self._zone_default_setpoint_c.get(zone_id, DEFAULT_SETPOINT_C))),
                "override",
                str(ov.get("mode", "comfort")),
                "override",
            )

        block = get_block(self._schedules, zone_id, now)
        if block is not None:
            return (float(block["setpoint_c"]), "schedule", str(block["mode"]), "schedule")

        return (
            self._zone_default_setpoint_c.get(zone_id, DEFAULT_SETPOINT_C),
            "default",
            self._zone_default_mode.get(zone_id, "eco"),
            "default",
        )

    # ------------------------------------------------------------------ overrides

    def set_override(self, zone_id: str, setpoint_c: float, mode: str = "comfort") -> None:
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
            if row.get("setpoint_c") is None:
                await self.hass.async_add_executor_job(
                    self._store.close_override, row["zone_id"], "migrated"
                )
                continue
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
                "mode": row.get("mode", "comfort"),
                "expires_at": expires_at,
                "unsub": async_track_point_in_time(
                    self.hass,
                    lambda _now, zid=zone_id: self._expire_override(zid),
                    expires_at,
                ),
            }
            LOGGER.debug(
                "Restored override for zone %s (%.1f°C %s), expires %s",
                zone_id, row["setpoint_c"], row.get("mode", "comfort"), expires_at,
            )

    # ------------------------------------------------------------------ dT/dt accessor

    def _get_dT_dt(self, zone_id: str) -> float | None:
        """Return rate of change of room temp in °C/min (positive = warming). None if < 2 readings."""
        dq = self._zone_last_temps.get(zone_id)
        if not dq or len(dq) < 2:
            return None
        dt_min = (dq[0][0] - dq[1][0]) / 60.0  # seconds → minutes
        if dt_min <= 0:
            return None
        return (dq[0][1] - dq[1][1]) / dt_min

    # ------------------------------------------------------------------ starvation logic

    def _run_starvation_logic(self, now: datetime) -> set[str]:
        """
        REVIEW: single-priority-zone — supports exactly one is_starvation_priority zone.
        Future ML will replace this with dynamic multi-zone prioritisation.
        Returns set of zone_ids that should be suppressed this tick.
        """
        # REVIEW: single-priority-zone
        priority_zone = next(
            (z for z in self._zones if z.get("is_starvation_priority")), None
        )
        if priority_zone is None:
            return set()

        pz_id = priority_zone["id"]

        # Guard: skip starvation entirely if priority zone is not enabled and actively cooling
        pz_enabled = self._zone_enabled.get(pz_id, False) and self._master_enabled
        pz_head_entity = priority_zone.get("head_entity", "")
        pz_head_state = self.hass.states.get(pz_head_entity) if pz_head_entity else None
        pz_cooling = pz_head_state and pz_head_state.state == "cool"
        if not (pz_enabled and pz_cooling):
            # Priority zone not running — clear any active suppression
            if self._starvation["active"]:
                LOGGER.debug("Starvation: priority zone not cooling — clearing suppression")
            self._starvation["active"] = False
            self._starvation["suppression_start"] = None
            return set()

        # Read sensors
        demand_delta = self._read_entity_float(priority_zone.get("demand_delta_entity"))
        thermal_delta = self._read_entity_float(priority_zone.get("head_thermal_delta_entity"))
        now_ts = now.timestamp()

        # Update rolling thermal_delta history for priority zone
        # REVIEW: single-priority-zone
        if thermal_delta is not None:
            self._zone_thermal_delta_history[pz_id].appendleft((now_ts, thermal_delta))

        if self._starvation["active"]:
            # Check exit conditions
            achieved = thermal_delta is not None and thermal_delta <= STARVATION_THERMAL_TARGET
            timed_out = (
                self._starvation["suppression_start"] is not None
                and (now - self._starvation["suppression_start"]).total_seconds() >= STARVATION_SUPPRESS_SECONDS
            )
            if achieved or timed_out:
                reason = "achieved" if achieved else "timeout"
                LOGGER.info("Starvation suppression ended (%s) for priority zone %s", reason, pz_id)
                self._starvation["active"] = False
                self._starvation["suppression_start"] = None
                # Cooldown before re-watching — prevents immediate re-trigger
                self._starvation["cooldown_until"] = now + timedelta(seconds=STARVATION_COOLDOWN_SECONDS)
                self._starvation["watch_start"] = None
                return set()
            # Still suppressing — return all non-priority zones
            # REVIEW: single-priority-zone
            return {z["id"] for z in self._zones if z["id"] != pz_id}

        # Not currently active — check watch/trigger logic
        in_cooldown = (
            self._starvation["cooldown_until"] is not None
            and now < self._starvation["cooldown_until"]
        )
        if in_cooldown:
            return set()

        if demand_delta is not None and demand_delta < STARVATION_DEMAND_THRESHOLD:
            if self._starvation["watch_start"] is None:
                self._starvation["watch_start"] = now
                LOGGER.debug("Starvation: started 30-min watch for zone %s (demand_delta=%.2f)", pz_id, demand_delta)
            elif (now - self._starvation["watch_start"]).total_seconds() >= STARVATION_WATCH_SECONDS:
                # Scan window for minimum thermal_delta — require enough samples
                cutoff = now_ts - STARVATION_WATCH_SECONDS
                window = [
                    v for (t, v) in self._zone_thermal_delta_history.get(pz_id, [])
                    if t >= cutoff
                ]
                if len(window) < STARVATION_MIN_SAMPLES:
                    LOGGER.debug(
                        "Starvation: watch window has only %d samples (need %d) — waiting",
                        len(window), STARVATION_MIN_SAMPLES,
                    )
                elif min(window) > STARVATION_THERMAL_TARGET:
                    # Never achieved deep cooling in 30 min — trigger suppression
                    LOGGER.info(
                        # REVIEW: single-priority-zone
                        "Starvation: triggering suppression for priority zone %s "
                        "(thermal_delta min=%.2f, demand_delta=%.2f)",
                        pz_id, min(window), demand_delta,
                    )
                    self._starvation["active"] = True
                    self._starvation["suppression_start"] = now
                    return {z["id"] for z in self._zones if z["id"] != pz_id}
                else:
                    # Achieved deep cooling during window — reset clock
                    self._starvation["watch_start"] = None
        else:
            # Demand dropped — reset watch clock
            if self._starvation["watch_start"] is not None:
                LOGGER.debug("Starvation: demand dropped for zone %s — resetting watch", pz_id)
            self._starvation["watch_start"] = None

        return set()

    def _read_entity_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unavailable", "unknown"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return None

    # ------------------------------------------------------------------ decision loop

    async def _async_update_data(self) -> dict[str, ZoneData]:
        now = dt_util.now()
        results: dict[str, ZoneData] = {}
        all_failed = True

        # Sensor guard: collect all valid zone temp readings + extra controller temps
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

        # Starvation logic — runs before zone loop, returns set of suppressed zone IDs
        suppressed_zones = self._run_starvation_logic(now)

        for zone in self._zones:
            zone_id = zone["id"]
            try:
                zone_data = await self._process_zone(
                    zone_id, zone, now,
                    guard_avg, guard_threshold, guard_active,
                    starvation_suppressed=(zone_id in suppressed_zones),
                )
                results[zone_id] = zone_data
                all_failed = False
                self._decision_log[zone_id].append({
                    "time": now.isoformat(timespec="seconds"),
                    "active_setpoint_c": zone_data.get("active_setpoint_c"),
                    "setpoint_source": zone_data.get("setpoint_source"),
                    "active_mode": zone_data.get("active_mode"),
                    "mode_source": zone_data.get("mode_source"),
                    "room_temp_c": zone_data.get("ext_temp_c"),
                    "offset_c": zone_data.get("offset_c"),
                    "commanded_setpoint": zone_data.get("commanded_setpoint"),
                    "command_issued": zone_data.get("command_issued"),
                    "idle": zone_data.get("idle"),
                    "starvation_suppressed": zone_data.get("starvation_suppressed"),
                    "ml_action": zone_data.get("ml_predicted_action"),
                    "ml_confidence": zone_data.get("ml_confidence"),
                })
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Zone %s decision failed: %s", zone_id, exc)
                results[zone_id] = ZoneData(
                    ext_temp_c=None, head_temp_c=None, offset_c=None,
                    active_setpoint_c=None, setpoint_source="error",
                    active_mode="eco", mode_source="error",
                    commanded_setpoint=None, command_issued=False,
                    override_active=bool(self._overrides.get(zone_id)),
                    idle=False, starvation_suppressed=False,
                    error=str(exc),
                    enabled=self._master_enabled and self._zone_enabled.get(zone_id, True),
                    ml_predicted_action=None, ml_confidence=None,
                )

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
        starvation_suppressed: bool = False,
    ) -> ZoneData:
        enabled = self._master_enabled and self._zone_enabled.get(zone_id, True)

        if not enabled:
            head_entity = zone["head_entity"]
            head_state = self.hass.states.get(head_entity)
            if head_state and head_state.state not in ("off", "unavailable"):
                await self.hass.services.async_call(
                    "climate", "turn_off", {"entity_id": head_entity}, blocking=True
                )
            return ZoneData(
                ext_temp_c=None, head_temp_c=None, offset_c=None,
                active_setpoint_c=None, setpoint_source="disabled",
                active_mode="eco", mode_source="disabled",
                commanded_setpoint=None, command_issued=False,
                override_active=bool(self._overrides.get(zone_id)),
                idle=False, starvation_suppressed=False,
                error=None, enabled=False,
                ml_predicted_action=None, ml_confidence=None,
            )

        head_entity = zone["head_entity"]
        head_state = self.hass.states.get(head_entity)
        ext_state = self.hass.states.get(zone["sensor_entity"])

        ext_temp_c: float | None = None
        head_temp_c: float | None = None

        if ext_state and ext_state.state not in ("unavailable", "unknown"):
            try:
                val = float(ext_state.state)
                if EXTERNAL_SENSOR_MIN_C <= val <= EXTERNAL_SENSOR_MAX_C:
                    ext_temp_c = val
                    await self._maybe_log_sensor(zone_id, "external", "temperature", value_numeric=val)
                else:
                    LOGGER.warning("Zone %s external sensor out of range: %s°C", zone_id, val)
            except (ValueError, TypeError):
                LOGGER.warning("Zone %s external sensor non-numeric: %s", zone_id, ext_state.state)

        if head_state and head_state.state not in ("unavailable", "unknown"):
            try:
                head_temp_c = float(head_state.attributes.get("current_temperature", "nan"))
                await self._maybe_log_sensor(zone_id, "head", "temperature", value_numeric=head_temp_c)
            except (ValueError, TypeError):
                head_temp_c = None

        # Log EVA in/out
        for eva_key, eva_label in (("eva_in_entity", "eva_in"), ("eva_out_entity", "eva_out")):
            entity_id = zone.get(eva_key, "")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown"):
                    try:
                        await self._maybe_log_sensor(zone_id, eva_label, "temperature", value_numeric=float(state.state))
                    except (ValueError, TypeError):
                        pass

        # Log demand_delta and head_thermal_delta sensors
        for sensor_key, sensor_label in (
            ("demand_delta_entity", "demand_delta"),
            ("head_thermal_delta_entity", "head_thermal_delta"),
        ):
            entity_id = zone.get(sensor_key, "")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown"):
                    try:
                        await self._maybe_log_sensor(zone_id, sensor_label, "delta_c", value_numeric=float(state.state))
                    except (ValueError, TypeError):
                        pass

        # Log occupancy, door, window (binary: 1.0 = active/open, 0.0 = inactive/closed)
        for entity_key, log_source in (
            ("occupancy_entity", "occupancy"),
            ("door_entity", "door"),
            ("window_entity", "window"),
        ):
            entity_id = zone.get(entity_key, "")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown"):
                    await self._maybe_log_sensor(
                        zone_id, log_source, "presence",
                        value_numeric=1.0 if state.state == "on" else 0.0,
                    )

        # dT/dt tracking
        if ext_temp_c is not None:
            epoch = now.timestamp()
            dq = self._zone_last_temps[zone_id]
            dq.appendleft((epoch, ext_temp_c))
            if len(dq) >= 2:
                dt_min = (dq[0][0] - dq[1][0]) / 60.0
                if dt_min > 0:
                    dT_dt_5min = (dq[0][1] - dq[1][1]) / dt_min
                    await self._maybe_log_sensor(zone_id, "derived", "dT_dt_5min", value_numeric=round(dT_dt_5min, 4))
            if len(dq) >= 3:
                dt_min = (dq[0][0] - dq[2][0]) / 60.0
                if dt_min > 0:
                    dT_dt_15min = (dq[0][1] - dq[2][1]) / dt_min
                    await self._maybe_log_sensor(zone_id, "derived", "dT_dt_15min", value_numeric=round(dT_dt_15min, 4))

        # Compute head offset (head internal sensor - room sensor)
        offset_c: float = 0.0
        if ext_temp_c is not None and head_temp_c is not None:
            raw_offset = head_temp_c - ext_temp_c
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
                # Clamp to >= 0: only compensate when head sensor reads warmer than room
                # (head sensor in a warm dead-zone). Never let negative offset lower the
                # commanded setpoint below what the head already reads — that causes the
                # Samsung to think it has achieved its target and stop cooling prematurely.
                offset_c = max(0.0, raw_offset)

        setpoint_min = float(self._controller_config.get("setpoint_min_c", CONTROLLER_DEFAULTS["setpoint_min_c"]))
        setpoint_max = float(self._controller_config.get("setpoint_max_c", CONTROLLER_DEFAULTS["setpoint_max_c"]))
        tolerance = float(self.params.get("setpoint_tolerance_c", 0.5))
        current_head_setpoint: float | None = None
        if head_state and head_state.state not in ("unavailable", "unknown"):
            try:
                current_head_setpoint = float(head_state.attributes.get("temperature", "nan"))
            except (ValueError, TypeError):
                pass

        # --- Starvation suppression overrides normal control ---
        if starvation_suppressed:
            # Raise setpoint to head's current temp + 1°C to minimise refrigerant draw.
            # Suppression takes precedence over active overrides (system constraint > user preference).
            commanded_setpoint: float | None = None
            command_issued = False
            if head_temp_c is not None:
                suppress_sp = head_temp_c + 1.0
                suppress_sp = max(setpoint_min, min(setpoint_max, suppress_sp))
                commanded_setpoint = suppress_sp
                if current_head_setpoint is None or abs(suppress_sp - current_head_setpoint) >= tolerance:
                    if head_state and head_state.state != "cool":
                        await self.hass.services.async_call(
                            "climate", "set_hvac_mode",
                            {"entity_id": head_entity, "hvac_mode": "cool"}, blocking=True,
                        )
                    await self.hass.services.async_call(
                        "climate", "set_temperature",
                        {"entity_id": head_entity, "temperature": suppress_sp}, blocking=True,
                    )
                    command_issued = True
            # else: head_temp_c unavailable — leave head as-is, no crash

            setpoint_c, sp_source, mode, mode_source = self._resolve_setpoint(zone_id, now)
            await self.hass.async_add_executor_job(
                self._store.log_decision,
                zone_id, sp_source, "suppressed",
                commanded_setpoint, ext_temp_c, head_temp_c, offset_c, commanded_setpoint,
                command_issued, None,
                setpoint_c, mode, mode_source, None, None, True,
            )
            return ZoneData(
                ext_temp_c=ext_temp_c, head_temp_c=head_temp_c,
                offset_c=offset_c if (ext_temp_c is not None and head_temp_c is not None) else None,
                active_setpoint_c=setpoint_c, setpoint_source=sp_source,
                active_mode=mode, mode_source=mode_source,
                commanded_setpoint=commanded_setpoint, command_issued=command_issued,
                override_active=bool(self._overrides.get(zone_id)),
                idle=False, starvation_suppressed=True,
                error=None, enabled=True,
                ml_predicted_action=None, ml_confidence=None,
            )

        # --- Normal control ---
        setpoint_c, sp_source, mode, mode_source = self._resolve_setpoint(zone_id, now)

        # Scheduled off block — shut head down for this time window
        if mode == "off":
            if head_state and head_state.state not in ("off", "unavailable"):
                await self.hass.services.async_call(
                    "climate", "turn_off", {"entity_id": head_entity}, blocking=True
                )
            ml_result = await self._ml_predict(zone_id, {"ext_temp_c": ext_temp_c, "setpoint_c": setpoint_c, "offset_c": offset_c, "mode": mode})
            ml_action = ml_result.get("action") if ml_result else None
            ml_confidence = ml_result.get("confidence") if ml_result else None
            self._zone_ml_confidence[zone_id] = ml_confidence or 0.0
            await self.hass.async_add_executor_job(
                self._store.log_decision,
                zone_id, sp_source, "scheduled_off",
                None, ext_temp_c, head_temp_c, offset_c, None,
                False, None,
                setpoint_c, mode, mode_source, ml_action, ml_confidence, False,
            )
            return ZoneData(
                ext_temp_c=ext_temp_c, head_temp_c=head_temp_c,
                offset_c=offset_c if head_temp_c is not None else None,
                active_setpoint_c=setpoint_c, setpoint_source=sp_source,
                active_mode=mode, mode_source=mode_source,
                commanded_setpoint=None, command_issued=False,
                override_active=bool(self._overrides.get(zone_id)),
                idle=True, starvation_suppressed=False,
                error=None, enabled=True,
                ml_predicted_action=ml_action, ml_confidence=ml_confidence,
            )

        # Occupancy auto-upgrade: eco → comfort when occupied
        # Unavailable/unknown occupancy silently stays eco (intentional safe default)
        if mode == "eco":
            occ_entity = zone.get("occupancy_entity", "")
            if occ_entity:
                occ_state = self.hass.states.get(occ_entity)
                if occ_state and occ_state.state == "on":
                    mode = "comfort"
                    mode_source = "occupancy"
        # Note: mode_source stays "schedule"/"default" when mode was already "comfort"

        target_sp = max(setpoint_min, min(setpoint_max, setpoint_c + offset_c))

        commanded_setpoint = None
        command_issued = False

        if ext_temp_c is None:
            # Sensor unavailable — suppress; do not act on stale data
            pass
        else:
            # Always command the setpoint — no predictive idle; zone enable/disable controls on/off
            commanded_setpoint = target_sp
            if head_state and head_state.state == "off":
                await self.hass.services.async_call(
                    "climate", "set_hvac_mode",
                    {"entity_id": head_entity, "hvac_mode": "cool"}, blocking=True,
                )
            if current_head_setpoint is None or abs(target_sp - current_head_setpoint) >= tolerance:
                await self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": head_entity, "temperature": target_sp}, blocking=True,
                )
                command_issued = True

        ml_result = await self._ml_predict(zone_id, {"ext_temp_c": ext_temp_c, "setpoint_c": setpoint_c, "offset_c": offset_c, "mode": mode})
        ml_action = ml_result.get("action") if ml_result else None
        ml_confidence = ml_result.get("confidence") if ml_result else None
        self._zone_ml_confidence[zone_id] = ml_confidence or 0.0

        decision_id = await self.hass.async_add_executor_job(
            self._store.log_decision,
            zone_id, sp_source,
            "cool" if commanded_setpoint is not None else "suppress",
            commanded_setpoint, ext_temp_c, head_temp_c, offset_c, commanded_setpoint,
            command_issued, None,
            setpoint_c, mode, mode_source, ml_action, ml_confidence, False,
        )
        if command_issued and commanded_setpoint is not None:
            await self.hass.async_add_executor_job(
                self._store.log_device_command,
                zone_id, "set_temperature", f"setpoint={commanded_setpoint}", decision_id,
            )

        return ZoneData(
            ext_temp_c=ext_temp_c,
            head_temp_c=head_temp_c,
            offset_c=offset_c if (ext_temp_c is not None and head_temp_c is not None) else None,
            active_setpoint_c=setpoint_c,
            setpoint_source=sp_source,
            active_mode=mode,
            mode_source=mode_source,
            commanded_setpoint=commanded_setpoint,
            command_issued=command_issued,
            override_active=bool(self._overrides.get(zone_id)),
            idle=False,
            starvation_suppressed=False,
            error=None,
            enabled=True,
            ml_predicted_action=ml_action,
            ml_confidence=ml_confidence,
        )

    # ------------------------------------------------------------------ ML shadow

    async def _ml_predict(self, zone_id: str, zone_state: dict) -> dict | None:
        """Run ML shadow prediction. Returns {action, confidence} or None if no model."""
        if self._ml_model is None:
            return None
        try:
            features = [
                zone_state.get("ext_temp_c") or 0.0,
                zone_state.get("setpoint_c") or 0.0,
                zone_state.get("offset_c") or 0.0,
                1.0 if zone_state.get("mode") == "comfort" else 0.0,
            ]
            prediction = await self.hass.async_add_executor_job(
                self._ml_model.predict, [features]
            )
            confidence = 0.0
            if hasattr(self._ml_model, "predict_proba"):
                proba = await self.hass.async_add_executor_job(
                    self._ml_model.predict_proba, [features]
                )
                confidence = float(max(proba[0]))
            return {"action": str(prediction[0]), "confidence": confidence}
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("ML predict error for zone %s: %s", zone_id, exc)
            return None

    # ------------------------------------------------------------------ system data

    async def _gather_system_data(self, now: datetime) -> None:
        """Log sun, ODU, outdoor temp, power, and weather data for ML training."""
        sun_state = self.hass.states.get("sun.sun")
        if sun_state:
            elev = sun_state.attributes.get("elevation")
            azim = sun_state.attributes.get("azimuth")
            if elev is not None:
                await self._maybe_log_sensor("_system", "sun", "elevation", value_numeric=float(elev))
            if azim is not None:
                await self._maybe_log_sensor("_system", "sun", "azimuth", value_numeric=float(azim))

        odu_entity = self._controller_config.get("odu_mode_entity", "")
        if odu_entity:
            odu_state = self.hass.states.get(odu_entity)
            if odu_state and odu_state.state not in ("unavailable", "unknown"):
                await self._maybe_log_sensor("_system", "odu", "mode", value_text=odu_state.state)

        outdoor_entity = self._controller_config.get("outdoor_temp_entity", "")
        if outdoor_entity:
            state = self.hass.states.get(outdoor_entity)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    await self._maybe_log_sensor("_system", "outdoor", "temperature", value_numeric=float(state.state))
                except (ValueError, TypeError):
                    pass

        power_entity = self._controller_config.get("power_entity", "")
        if power_entity:
            state = self.hass.states.get(power_entity)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    await self._maybe_log_sensor("_system", "hvac", "power_w", value_numeric=float(state.state))
                except (ValueError, TypeError):
                    pass

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
                        forecast_json = json.dumps(forecast, default=str)
                        await self.hass.async_add_executor_job(
                            self._store.log_weather_forecast, forecast_json
                        )
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Weather forecast unavailable: %s", exc)

    async def _maybe_log_sensor(
        self,
        zone_id: str,
        source: str,
        metric: str,
        value_numeric: float | None = None,
        value_text: str | None = None,
    ) -> None:
        cache_val: Any = value_numeric if value_numeric is not None else value_text
        key = f"{zone_id}:{source}:{metric}"
        if self._last_sensor_values.get(key) != cache_val:
            self._last_sensor_values[key] = cache_val
            await self.hass.async_add_executor_job(
                self._store.log_sensor_event, zone_id, source, metric, value_numeric, value_text
            )
