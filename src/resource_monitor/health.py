"""Read-only health projection for REST consumers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .freshness import Freshness, assess
from .storage import SQLiteStore


def _health(values: dict[str, Any]) -> str:
    if values.get("hung") is True or values.get("status") == "hung":
        return "hung"
    value = values.get("health", values.get("model_health", values.get("status", "unknown")))
    return value if isinstance(value, str) and value else "unknown"


def _record(record: dict[str, Any], *, now: datetime, max_age_seconds: float) -> dict[str, Any]:
    freshness = assess(record["observed_at"], now=now, max_age_seconds=max_age_seconds)
    values = record["values"]
    health = _health(values)
    if freshness.state is Freshness.INVALID:
        health = "unknown"
    elif freshness.state is Freshness.STALE:
        health = "stale"
    return {
        "id": record["subject_id"],
        "health": health,
        "status": "unknown" if health == "unknown" else health,
        "observed_at": record["observed_at"],
        "source_id": record["source_id"],
        "freshness_seconds": freshness.age_seconds,
    }


def get_health_projection(
    store: SQLiteStore,
    *,
    max_age_seconds: float = 300.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project node/model health without reinterpreting provider quota data."""
    import math
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    current = now or datetime.now().astimezone()
    rm_ok = True

    def _latest(subject_type: str) -> list[dict[str, Any]]:
        nonlocal rm_ok
        try:
            return store.latest_snapshots(subject_type=subject_type, limit=10_000)
        except Exception:
            rm_ok = False
            return []

    node_records = _latest("node")
    node_records += _latest("host")
    nodes = [_record(record, now=current, max_age_seconds=max_age_seconds)
             for record in node_records]
    models = [_record(record, now=current, max_age_seconds=max_age_seconds)
              for record in _latest("model")]
    for item, record in zip(nodes, node_records):
        values = record["values"]
        item["pressure"] = {
            key: values[key] for key in ("cpu_percent", "memory_percent", "disk_percent",
                                         "cpu_pressure", "memory_pressure", "disk_pressure")
            if key in values
        }
    states = [item["health"] for item in nodes + models]
    status = "unknown" if not states else (
        "degraded" if any(state in {"stale", "unknown", "hung", "offline"} for state in states)
        else "ok"
    )
    rm_health = "healthy" if rm_ok else "unhealthy"
    rm_status = "ok" if rm_ok else "error"
    return {
        "status": status,
        "observed_at": current.isoformat(),
        "freshness_seconds": 0.0,
        "rm": {"status": rm_status, "health": rm_health, "freshness_seconds": 0.0,
               "source_id": "resource-monitor", "observed_at": current.isoformat()},
        "nodes": nodes,
        "models": models,
        "max_age_seconds": max_age_seconds,
    }
