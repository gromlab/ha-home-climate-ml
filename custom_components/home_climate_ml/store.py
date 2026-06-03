"""SQLite data store for Home Climate ML."""
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
CREATE INDEX IF NOT EXISTS idx_sensor_events_time ON sensor_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_sensor_events_zone ON sensor_events(zone_id);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_decisions_zone ON decisions(zone_id);
"""

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

    def log_decision(
        self,
        zone_id: str,
        schedule_source: str,
        target_mode: str,
        target_setpoint_c: float | None,
        external_temp_c: float | None,
        head_temp_c: float | None,
        computed_offset_c: float | None,
        corrected_setpoint_c: float | None,
        command_issued: bool,
        notes: str | None = None,
    ) -> int:
        with _connect(self._db_path) as conn:
            cur = conn.execute(
                """INSERT INTO decisions
                   (timestamp,zone_id,schedule_source,target_mode,target_setpoint_c,
                    external_temp_c,head_temp_c,computed_offset_c,corrected_setpoint_c,
                    command_issued,notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (_utcnow(), zone_id, schedule_source, target_mode, target_setpoint_c,
                 external_temp_c, head_temp_c, computed_offset_c, corrected_setpoint_c,
                 int(command_issued), notes),
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

    def open_override(
        self, zone_id: str, setpoint_c: float | None, mode: str, expires_at: datetime
    ) -> None:
        # Close any open override for this zone first
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
