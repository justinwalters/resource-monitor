"""Durable local storage for canonical telemetry and heartbeat leases."""

from __future__ import annotations

import json
import sqlite3
from threading import RLock
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import AgentHeartbeat, ResourceEvent, ResourceSnapshot


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                record_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                subject_id TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source_id TEXT NOT NULL,
                values_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS snapshots_subject_time
                ON snapshots(subject_id, observed_at);
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                source_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_subject_time
                ON events(subject_id, occurred_at);
            CREATE TABLE IF NOT EXISTS leases (
                agent_id TEXT PRIMARY KEY,
                lease_id TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                agent_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def save_snapshot(self, record: ResourceSnapshot) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", (record.record_id, record.schema_version, record.subject_id, record.subject_type, record.observed_at.isoformat(), record.source_id, json.dumps(record.values, sort_keys=True)))
            self._db.commit()

    def save_event(self, record: ResourceEvent) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (record.event_id, record.schema_version, record.event_type, record.subject_id, record.occurred_at.isoformat(), record.source_id, record.severity, json.dumps(record.payload, sort_keys=True)))
            self._db.commit()

    def save_heartbeat(self, heartbeat: AgentHeartbeat) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO leases VALUES (?, ?, ?, ?, ?)", (heartbeat.agent_id, heartbeat.lease_id, heartbeat.sent_at.isoformat(), heartbeat.expires_at.isoformat(), json.dumps(heartbeat.metadata, sort_keys=True)))
            self._db.commit()

    def get_heartbeat(self, agent_id: str) -> AgentHeartbeat | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM leases WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return AgentHeartbeat(agent_id, row["lease_id"], datetime.fromisoformat(row["sent_at"]), datetime.fromisoformat(row["expires_at"]), json.loads(row["metadata_json"]))

    def save_checkpoint(self, agent_id: str, task_id: str, checkpoint: dict[str, Any], *, updated_at: datetime) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?)", (agent_id, task_id, json.dumps(checkpoint, sort_keys=True), updated_at.isoformat()))
            self._db.commit()

    def get_checkpoint(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM checkpoints WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return {"agent_id": row["agent_id"], "task_id": row["task_id"], "checkpoint": json.loads(row["checkpoint_json"]), "updated_at": row["updated_at"]}

    def count(self, table: str) -> int:
        if table not in {"snapshots", "events", "leases"}:
            raise ValueError("unsupported table")
        with self._lock:
            return int(self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def list_agent_ids(self, *, limit: int = 100) -> list[str]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10,000")
        with self._lock:
            rows = self._db.execute(
                "SELECT agent_id FROM leases ORDER BY expires_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [str(row["agent_id"]) for row in rows]

    def latest_snapshots(self, *, subject_type: str | None = None, limit: int = 10000) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10,000")
        with self._lock:
            where = "WHERE subject_type = ?" if subject_type is not None else ""
            params: tuple[Any, ...] = (subject_type, limit) if subject_type is not None else (limit,)
            rows = self._db.execute(
                f"""SELECT * FROM (
                        SELECT s.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY subject_id, subject_type
                                   ORDER BY observed_at DESC, rowid DESC
                               ) AS row_number
                        FROM snapshots s {where}
                    ) latest
                    WHERE row_number = 1
                    ORDER BY observed_at DESC LIMIT ?""", params).fetchall()
        return [self._snapshot_dict(row) for row in rows]

    def snapshots(self, subject_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10,000")
        with self._lock:
            if subject_id is None:
                rows = self._db.execute("SELECT * FROM snapshots ORDER BY observed_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM snapshots WHERE subject_id = ? ORDER BY observed_at DESC LIMIT ?", (subject_id, limit)).fetchall()
        return [self._snapshot_dict(row) for row in rows]

    def events(self, subject_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10,000")
        with self._lock:
            if subject_id is None:
                rows = self._db.execute("SELECT * FROM events ORDER BY occurred_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM events WHERE subject_id = ? ORDER BY occurred_at DESC LIMIT ?", (subject_id, limit)).fetchall()
        return [self._event_dict(row) for row in rows]

    def prune_before(self, cutoff: datetime) -> dict[str, int]:
        """Delete historical observations before cutoff; leases are retained."""
        cutoff_text = cutoff.isoformat()
        with self._lock:
            snapshots = self._db.execute("DELETE FROM snapshots WHERE observed_at < ?", (cutoff_text,)).rowcount
            events = self._db.execute("DELETE FROM events WHERE occurred_at < ?", (cutoff_text,)).rowcount
            self._db.commit()
        return {"snapshots": snapshots, "events": events}

    @staticmethod
    def _snapshot_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {"record_id": row["record_id"], "schema_version": row["schema_version"], "subject_id": row["subject_id"], "subject_type": row["subject_type"], "observed_at": row["observed_at"], "source_id": row["source_id"], "values": json.loads(row["values_json"])}

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {"event_id": row["event_id"], "schema_version": row["schema_version"], "event_type": row["event_type"], "subject_id": row["subject_id"], "occurred_at": row["occurred_at"], "source_id": row["source_id"], "severity": row["severity"], "payload": json.loads(row["payload_json"])}
