"""Read-only summary projections over Resource Monitor history."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from typing import Any

from .storage import SQLiteStore


def _require_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_daily_summary(store: SQLiteStore, *, day: date | None = None) -> dict[str, Any]:
    """Return a bounded, UTC-day summary without changing stored history."""

    target = day or datetime.now(timezone.utc).date()
    snapshots = [
        record for record in store.snapshots(limit=10_000)
        if _parse_timestamp(record["observed_at"]).astimezone(timezone.utc).date() == target
    ]
    events = [
        record for record in store.events(limit=10_000)
        if _parse_timestamp(record["occurred_at"]).astimezone(timezone.utc).date() == target
    ]
    return {
        "day": target.isoformat(),
        "status": "known" if snapshots or events else "unknown",
        "snapshots": {
            "count": len(snapshots),
            "by_subject_type": dict(Counter(record["subject_type"] for record in snapshots)),
        },
        "events": {
            "count": len(events),
            "by_event_type": dict(Counter(record["event_type"] for record in events)),
        },
    }


def get_recent_events(store: SQLiteStore, *, limit: int = 100) -> dict[str, Any]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10,000")
    events = store.events(limit=limit)
    return {"status": "known" if events else "unknown", "events": events, "limit": limit}


def get_model_health(store: SQLiteStore, model_id: str) -> dict[str, Any]:
    model_id = _require_text(model_id, "model_id")
    for record in store.latest_snapshots(limit=10_000):
        values = record["values"]
        if not (
            record["subject_type"] == "model" and record["subject_id"] == model_id
        ) and values.get("model_id") != model_id and values.get("model") != model_id:
            continue
        health = values.get("health", values.get("model_health", values.get("status", "unknown")))
        return {"model_id": model_id, "status": "known", "health": health, "record": record}
    return {"model_id": model_id, "status": "unknown", "health": "unknown", "record": None}
