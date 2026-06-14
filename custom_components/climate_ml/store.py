"""SQLite data store for ClimateML."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS sensor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    zone_id TEXT,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value_numeric REAL,
    value_text TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    schedule_source TEXT NOT NULL,
    target_mode TEXT NOT NULL,
    target_setpoint_c REAL,
    external_temp_c REAL,
    head_temp_c REAL,
    computed_offset_c REAL,
    corrected_setpoint_c REAL,
    command_issued INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    setpoint_c REAL,
    mode TEXT,
    comfort_level INTEGER,
    expired_at TEXT,
    expire_reason TEXT
);
CREATE TABLE IF NOT EXISTS device_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    decision_id INTEGER REFERENCES decisions(id)
);
CREATE TABLE IF NOT EXISTS safety_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    severity TEXT NOT NULL,
    zone_id TEXT,
    description TEXT NOT NULL,
    state_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS user_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    new_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    comfort_level INTEGER,
    room_temp_c REAL,
    band_min REAL,
    band_max REAL
);
CREATE TABLE IF NOT EXISTS weather_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    forecast_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sensor_events_time ON sensor_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_sensor_events_zone ON sensor_events(zone_id);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_decisions_zone ON decisions(zone_id);
CREATE INDEX IF NOT EXISTS idx_cycle_events_time ON cycle_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_cycle_events_zone ON cycle_events(zone_id);
"""

_SCHEMA_MIGRATIONS = [
    "ALTER TABLE decisions ADD COLUMN ml_predicted_action TEXT",
    "ALTER TABLE decisions ADD COLUMN ml_confidence REAL",
    "ALTER TABLE decisions ADD COLUMN comfort_level INTEGER",
    "ALTER TABLE decisions ADD COLUMN band_min REAL",
    "ALTER TABLE decisions ADD COLUMN band_max REAL",
    "ALTER TABLE overrides ADD COLUMN comfort_level INTEGER",
    # v0.6.0 columns
    "ALTER TABLE decisions ADD COLUMN active_setpoint_c REAL",
    "ALTER TABLE decisions ADD COLUMN setpoint_source TEXT",
    "ALTER TABLE decisions ADD COLUMN active_mode TEXT",
    "ALTER TABLE decisions ADD COLUMN mode_source TEXT",
    "ALTER TABLE decisions ADD COLUMN starvation_suppressed INTEGER DEFAULT 0",
]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClimateDataStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with _connect(db_path) as conn:
            conn.executescript(CREATE_TABLES)
            self._migrate_schema(conn)

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Idempotent column additions — safe to run on every startup."""
        for sql in _SCHEMA_MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists

    def log_decision(
        self,
        zone_id: str,
        setpoint_source: str,
        target_mode: str,
        target_setpoint_c: float | None,
        external_temp_c: float | None,
        head_temp_c: float | None,
        computed_offset_c: float | None,
        corrected_setpoint_c: float | None,
        command_issued: bool,
        notes: str | None = None,
        active_setpoint_c: float | None = None,
        active_mode: str | None = None,
        mode_source: str | None = None,
        ml_predicted_action: str | None = None,
        ml_confidence: float | None = None,
        starvation_suppressed: bool = False,
    ) -> int:
        with _connect(self._db_path) as conn:
            cur = conn.execute(
                """INSERT INTO decisions
                   (timestamp,zone_id,schedule_source,target_mode,target_setpoint_c,
                    external_temp_c,head_temp_c,computed_offset_c,corrected_setpoint_c,
                    command_issued,notes,
                    active_setpoint_c,setpoint_source,active_mode,mode_source,
                    ml_predicted_action,ml_confidence,starvation_suppressed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_utcnow(), zone_id, setpoint_source, target_mode, target_setpoint_c,
                 external_temp_c, head_temp_c, computed_offset_c, corrected_setpoint_c,
                 int(command_issued), notes,
                 active_setpoint_c, setpoint_source, active_mode, mode_source,
                 ml_predicted_action, ml_confidence, int(starvation_suppressed)),
            )
            return cur.lastrowid

    def log_sensor_event(
        self,
        zone_id: str | None,
        source: str,
        metric: str,
        value_numeric: float | None = None,
        value_text: str | None = None,
    ) -> None:
        with _connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO sensor_events (timestamp,zone_id,source,metric,value_numeric,value_text)
                   VALUES (?,?,?,?,?,?)""",
                (_utcnow(), zone_id, source, metric, value_numeric, value_text),
            )

    def log_device_command(
        self, zone_id: str, command_type: str, payload: str, decision_id: int | None = None
    ) -> None:
        with _connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO device_commands (timestamp,zone_id,command_type,payload,decision_id)
                   VALUES (?,?,?,?,?)""",
                (_utcnow(), zone_id, command_type, payload, decision_id),
            )

    def log_safety_event(
        self, zone_id: str | None, severity: str, description: str, state_snapshot: str | None = None
    ) -> None:
        with _connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO safety_events (timestamp,severity,zone_id,description,state_snapshot)
                   VALUES (?,?,?,?,?)""",
                (_utcnow(), severity, zone_id, description, state_snapshot),
            )

    def log_user_change(self, zone_id: str, change_type: str, new_value: str) -> None:
        with _connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO user_changes (timestamp,zone_id,change_type,new_value) VALUES (?,?,?,?)",
                (_utcnow(), zone_id, change_type, new_value),
            )

    def log_cycle_event(
        self,
        zone_id: str,
        event_type: str,
        comfort_level: int | None = None,
        room_temp_c: float | None = None,
        band_min: float | None = None,
        band_max: float | None = None,
    ) -> None:
        with _connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO cycle_events
                   (timestamp,zone_id,event_type,comfort_level,room_temp_c,band_min,band_max)
                   VALUES (?,?,?,?,?,?,?)""",
                (_utcnow(), zone_id, event_type, comfort_level, room_temp_c, band_min, band_max),
            )

    def log_weather_forecast(self, forecast_json: str) -> None:
        with _connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO weather_forecast (fetched_at,forecast_json) VALUES (?,?)",
                (_utcnow(), forecast_json),
            )

    def open_override(
        self, zone_id: str, setpoint_c: float, mode: str, expires_at: datetime
    ) -> None:
        self.close_override(zone_id, "updated")
        with _connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO overrides (zone_id,started_at,expires_at,setpoint_c,mode)
                   VALUES (?,?,?,?,?)""",
                (zone_id, _utcnow(), expires_at.isoformat(), setpoint_c, mode),
            )

    def close_override(self, zone_id: str, reason: str) -> None:
        with _connect(self._db_path) as conn:
            conn.execute(
                """UPDATE overrides SET expired_at=?, expire_reason=?
                   WHERE zone_id=? AND expired_at IS NULL""",
                (_utcnow(), reason, zone_id),
            )

    def restore_active_overrides(self) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """SELECT zone_id, setpoint_c, mode, expires_at FROM overrides
                   WHERE expired_at IS NULL AND expires_at > ?""",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def prune(self, sensor_days: int = 90, decision_days: int = 365) -> None:
        from datetime import timedelta
        sensor_cutoff = (datetime.now(timezone.utc) - timedelta(days=sensor_days)).isoformat()
        decision_cutoff = (datetime.now(timezone.utc) - timedelta(days=decision_days)).isoformat()
        with _connect(self._db_path) as conn:
            conn.execute("DELETE FROM sensor_events WHERE timestamp < ?", (sensor_cutoff,))
            conn.execute("DELETE FROM decisions WHERE timestamp < ?", (decision_cutoff,))
            conn.execute("DELETE FROM device_commands WHERE timestamp < ?", (decision_cutoff,))
            conn.execute("DELETE FROM cycle_events WHERE timestamp < ?", (sensor_cutoff,))
            conn.execute("DELETE FROM weather_forecast WHERE fetched_at < ?", (sensor_cutoff,))
