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
    BAND_HYSTERESIS_DEFAULT,
    COMFORT_LEVEL_DEFAULTS,
    CONTROLLER_DEFAULTS,
    DOMAIN,
    EXTERNAL_SENSOR_MAX_C,
    EXTERNAL_SENSOR_MIN_C,
    LOGGER,
)
from .schedule import ScheduleBlock, get_block, get_current_block_detail, get_next_transition, parse_schedule_from_options
from .store import ClimateDataStore


class ZoneData(TypedDict):
    ext_temp_c: float | None
    head_temp_c: float | None
    offset_c: float | None
    active_comfort_level: int
    comfort_source: str          # "vacation" | "override" | "schedule" | "default"
    band_min: float
    band_max: float
    commanded_setpoint: float | None
    command_issued: bool
    override_active: bool
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

        # Comfort level definitions: {1: {name, min_c, max_c}, ...}
        raw_levels = self._controller_config.get("comfort_levels") or COMFORT_LEVEL_DEFAULTS
        self._comfort_levels: dict[int, dict] = {
            int(cl["level"]): cl for cl in raw_levels
        }

        self._band_hysteresis: float = float(
            self._controller_config.get("band_hysteresis_c", BAND_HYSTERESIS_DEFAULT)
        )
        self._vacation_comfort_level: int = int(
            self._controller_config.get("vacation_comfort_level", 5)
        )

        # Zone default comfort level: seed from subentry data, overwritten by SELECT restore
        self._zone_default_level: dict[str, int] = {
            z["id"]: int(z.get("default_comfort_level", 3)) for z in zones
        }

        self._overrides: dict[str, dict] = {}
        self._last_sensor_values: dict[str, Any] = {}
        self._zone_enabled: dict[str, bool] = {z["id"]: True for z in zones}
        self._decision_log: dict[str, deque] = {z["id"]: deque(maxlen=30) for z in zones}

        # dT/dt tracking: deque of (epoch_seconds, temp_c) tuples
        self._zone_last_temps: dict[str, deque] = {z["id"]: deque(maxlen=3) for z in zones}

        # ML shadow mode
        self._ml_model: Any | None = None
        self._zone_ml_confidence: dict[str, float] = {z["id"]: 0.0 for z in zones}

        # Ensure hass.data[DOMAIN] exists before any reads — must precede master/vacation reads
        hass.data.setdefault(DOMAIN, {})

        # Master/vacation: survive coordinator reload via hass.data; reset on cold HA restart
        self._master_enabled: bool = hass.data[DOMAIN].get(f"{entry_id}_master", False)
        self._vacation_mode: bool = hass.data[DOMAIN].get(f"{entry_id}_vacation", False)

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
    def comfort_levels(self) -> dict[int, dict]:
        return self._comfort_levels

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

    def set_zone_default_level(self, zone_id: str, level: int) -> None:
        self._zone_default_level[zone_id] = level
        LOGGER.debug("Zone %s default comfort level set to %s", zone_id, level)

    def set_ml_model(self, model: Any) -> None:
        self._ml_model = model
        LOGGER.info("ML model loaded: %s", type(model).__name__)

    # ------------------------------------------------------------------ comfort level resolution

    def _resolve_comfort_level(
        self, zone_id: str, now: datetime
    ) -> tuple[int, float, float, str]:
        """Return (level, band_min, band_max, source) for zone at given time."""
        if self._vacation_mode:
            level = self._vacation_comfort_level
            source = "vacation"
        elif zone_id in self._overrides:
            level = self._overrides[zone_id].get(
                "comfort_level", self._zone_default_level.get(zone_id, 3)
            )
            source = "override"
        else:
            sched_level = get_block(self._schedules, zone_id, now)
            if sched_level is not None:
                level = sched_level
                source = "schedule"
            else:
                level = self._zone_default_level.get(zone_id, 3)
                source = "default"

        band = self._comfort_levels.get(level, {})
        band_min = float(band.get("min_c", 19.5))
        band_max = float(band.get("max_c", 22.0))
        return level, band_min, band_max, source

    # ------------------------------------------------------------------ overrides

    def set_override(self, zone_id: str, comfort_level: int) -> None:
        override_minutes = self.params.get("override_duration_minutes", 120)
        expires_at = dt_util.utcnow() + timedelta(minutes=override_minutes)
        self._cancel_override_timer(zone_id)
        self._overrides[zone_id] = {
            "comfort_level": comfort_level,
            "expires_at": expires_at,
            "unsub": async_track_point_in_time(
                self.hass, lambda _now, zid=zone_id: self._expire_override(zid), expires_at
            ),
        }
        self.hass.async_add_executor_job(
            self._store.open_override, zone_id, comfort_level, expires_at
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
            # Guard: pre-migration rows have no comfort_level — close them
            if row.get("comfort_level") is None:
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
                "comfort_level": row["comfort_level"],
                "expires_at": expires_at,
                "unsub": async_track_point_in_time(
                    self.hass,
                    lambda _now, zid=zone_id: self._expire_override(zid),
                    expires_at,
                ),
            }
            LOGGER.debug(
                "Restored override for zone %s (level %s), expires %s",
                zone_id, row["comfort_level"], expires_at,
            )

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

        # Resolve comfort levels for all zones, then sort by level ascending (Goal 6)
        resolved: list[tuple[dict, int, float, float, str]] = []
        for zone in self._zones:
            level, band_min, band_max, source = self._resolve_comfort_level(zone["id"], now)
            resolved.append((zone, level, band_min, band_max, source))
        resolved.sort(key=lambda x: x[1])  # ascending: Level 1 (tightest) processed first

        for zone, level, band_min, band_max, comfort_source in resolved:
            zone_id = zone["id"]
            try:
                zone_data = await self._process_zone(
                    zone_id, zone, now,
                    level, band_min, band_max, comfort_source,
                    guard_avg, guard_threshold, guard_active,
                )
                results[zone_id] = zone_data
                all_failed = False
                self._decision_log[zone_id].append({
                    "time": now.isoformat(timespec="seconds"),
                    "comfort_level": level,
                    "comfort_source": comfort_source,
                    "band_min": band_min,
                    "band_max": band_max,
                    "room_temp_c": zone_data.get("ext_temp_c"),
                    "offset_c": zone_data.get("offset_c"),
                    "commanded_setpoint": zone_data.get("commanded_setpoint"),
                    "command_issued": zone_data.get("command_issued"),
                    "ml_action": zone_data.get("ml_predicted_action"),
                    "ml_confidence": zone_data.get("ml_confidence"),
                })
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Zone %s decision failed: %s", zone_id, exc)
                results[zone_id] = ZoneData(
                    ext_temp_c=None, head_temp_c=None, offset_c=None,
                    active_comfort_level=level, comfort_source=comfort_source,
                    band_min=band_min, band_max=band_max,
                    commanded_setpoint=None, command_issued=False,
                    override_active=bool(self._overrides.get(zone_id)),
                    error=str(exc), enabled=self._master_enabled and self._zone_enabled.get(zone_id, True),
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
        level: int,
        band_min: float,
        band_max: float,
        comfort_source: str,
        guard_avg: float | None,
        guard_threshold: float,
        guard_active: bool,
    ) -> ZoneData:
        enabled = self._master_enabled and self._zone_enabled.get(zone_id, True)

        if not enabled:
            head_entity = zone["head_entity"]
            await self.hass.services.async_call(
                "climate", "turn_off", {"entity_id": head_entity}, blocking=True
            )
            return ZoneData(
                ext_temp_c=None, head_temp_c=None, offset_c=None,
                active_comfort_level=level, comfort_source=comfort_source,
                band_min=band_min, band_max=band_max,
                commanded_setpoint=None, command_issued=False,
                override_active=bool(self._overrides.get(zone_id)),
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
                offset_c = raw_offset

        setpoint_min = float(self._controller_config.get("setpoint_min_c", CONTROLLER_DEFAULTS["setpoint_min_c"]))
        setpoint_max = float(self._controller_config.get("setpoint_max_c", CONTROLLER_DEFAULTS["setpoint_max_c"]))
        tolerance = float(self.params.get("setpoint_tolerance_c", 0.5))

        # Current head setpoint (for tolerance gate)
        current_head_setpoint: float | None = None
        if head_state and head_state.state not in ("unavailable", "unknown"):
            try:
                current_head_setpoint = float(head_state.attributes.get("temperature", "nan"))
            except (ValueError, TypeError):
                pass

        commanded_setpoint: float | None = None
        command_issued = False

        if ext_temp_c is None:
            # Sensor unavailable — suppress; do not act on stale data
            pass
        elif ext_temp_c < band_min:
            # Room too cold — suppress; Samsung's internal hysteresis already stops
            # the compressor before the room drops this far.  Forcing a cool command
            # here would run the compressor on a cold room.
            pass
        else:
            # Room at or above band_min (in-band or above) — always target band_max + offset.
            # When above band: this actively cools.
            # When in-band: this raises the setpoint above the room so Samsung stops
            # maintaining any stale lower setpoint and lets the room warm naturally.
            target_sp = max(setpoint_min, min(setpoint_max, band_max + offset_c))
            commanded_setpoint = target_sp
            if current_head_setpoint is None or abs(target_sp - current_head_setpoint) >= tolerance:
                if head_state and head_state.state != "cool":
                    await self.hass.services.async_call(
                        "climate", "set_hvac_mode",
                        {"entity_id": head_entity, "hvac_mode": "cool"}, blocking=True,
                    )
                await self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": head_entity, "temperature": target_sp}, blocking=True,
                )
                command_issued = True

        # ML shadow prediction (non-blocking; never affects commands)
        ml_result = await self._ml_predict(zone_id, {
            "ext_temp_c": ext_temp_c,
            "band_min": band_min,
            "band_max": band_max,
            "offset_c": offset_c,
            "comfort_level": level,
        })
        ml_action = ml_result.get("action") if ml_result else None
        ml_confidence = ml_result.get("confidence") if ml_result else None
        self._zone_ml_confidence[zone_id] = ml_confidence or 0.0

        decision_id = await self.hass.async_add_executor_job(
            self._store.log_decision,
            zone_id, comfort_source, "cool" if commanded_setpoint is not None else "suppress",
            commanded_setpoint, ext_temp_c, head_temp_c, offset_c, commanded_setpoint, command_issued,
            None,  # notes
            level, band_min, band_max, ml_action, ml_confidence,
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
            active_comfort_level=level,
            comfort_source=comfort_source,
            band_min=band_min,
            band_max=band_max,
            commanded_setpoint=commanded_setpoint,
            command_issued=command_issued,
            override_active=bool(self._overrides.get(zone_id)),
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
                zone_state.get("band_min") or 0.0,
                zone_state.get("band_max") or 0.0,
                zone_state.get("offset_c") or 0.0,
                float(zone_state.get("comfort_level") or 3),
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
