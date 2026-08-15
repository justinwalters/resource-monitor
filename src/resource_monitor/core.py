"""Small orchestration layer over adapters and durable storage."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .adapters import AdapterRegistry, TelemetryAdapter
from .models import AgentHeartbeat, ResourceEvent, ResourceSnapshot, utc_now
from .storage import SQLiteStore
from .heartbeat import HeartbeatManager
from .task_intelligence import (
    evaluate_task_feasibility,
    explain_task_failure,
    get_retry_cost_estimate,
    get_work_constraints,
)
from .summaries import get_daily_summary, get_model_health, get_recent_events
from .health import get_health_projection
from .freshness import Freshness, assess
from .quotas import add_reset_timing, normalize_window, summarize_reset_context


def _declared_quota_source_supersessions(records: list[dict[str, Any]]) -> set[str]:
    """Return well-formed, provider-local source IDs retired by newer collectors.

    The declaration is deliberately generic and is not projected to API callers.
    It lets an authoritative adapter replace a lower-fidelity source while the
    underlying snapshots remain available for audit and historical queries.
    """
    superseded: set[str] = set()
    for record in records:
        source_ids = record["values"].get("supersedes_source_ids")
        if not isinstance(source_ids, list) or not source_ids or len(source_ids) > 32:
            continue
        if not all(
            isinstance(source_id, str) and 0 < len(source_id) <= 256
            for source_id in source_ids
        ):
            continue
        superseded.update(source_id for source_id in source_ids if source_id != record["source_id"])
    return superseded


class ResourceMonitor:
    def __init__(self, store: SQLiteStore, adapters: AdapterRegistry | None = None) -> None:
        self.store = store
        self.adapters = adapters or AdapterRegistry()

    def add_adapter(self, adapter: TelemetryAdapter) -> None:
        self.adapters.register(adapter)

    def collect_once(self) -> int:
        records = self.adapters.collect_all()
        for record in records:
            if isinstance(record, ResourceSnapshot):
                self.store.save_snapshot(record)
            else:
                self.store.save_event(record)
        return len(records)

    def ingest_snapshot(self, snapshot: ResourceSnapshot) -> None:
        self.store.save_snapshot(snapshot)

    def ingest_event(self, event: ResourceEvent) -> None:
        self.store.save_event(event)

    def heartbeat(self, agent_id: str, lease_id: str, ttl_seconds: int, *, now: datetime | None = None, metadata: dict[str, object] | None = None) -> AgentHeartbeat:
        heartbeat = AgentHeartbeat.create(agent_id, lease_id, ttl_seconds, now=now or utc_now(), metadata=metadata)
        self.store.save_heartbeat(heartbeat)
        return heartbeat

    def list_available_resources(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        return self.store.latest_snapshots(limit=limit)

    def list_resources_near_limit(self, *, threshold: float = 0.2, limit: int = 10000) -> list[dict[str, Any]]:
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        result = []
        for record in self.list_available_resources(limit=limit):
            values = record["values"]
            remaining, maximum = values.get("capacity_remaining"), values.get("capacity_max")
            if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
                continue
            if not isinstance(maximum, (int, float)) or isinstance(maximum, bool) or maximum <= 0:
                maximum = values.get("capacity")
            if not isinstance(maximum, (int, float)) or isinstance(maximum, bool) or maximum <= 0:
                continue
            ratio = remaining / maximum
            if ratio <= threshold:
                result.append({**record, "remaining_ratio": ratio, "threshold": threshold})
        return result

    def get_node_status(self, node_id: str) -> dict[str, Any]:
        if not node_id:
            raise ValueError("node_id is required")
        return HeartbeatManager(self.store).status(node_id).as_dict()

    def get_provider_status(self, provider: str) -> dict[str, Any]:
        if not provider:
            raise ValueError("provider is required")
        candidates: dict[int, dict[str, Any]] = {}
        for record in self.store.latest_snapshots(limit=10000):
            subject_type = record["subject_type"]
            values = record["values"]
            subject_id = record["subject_id"]
            if subject_type == "provider-auth" and (
                subject_id in {provider, f"provider-auth:{provider}"}
                or values.get("provider") == provider
            ):
                candidates.setdefault(0, record)
            elif subject_type == "provider" and (
                subject_id == provider or values.get("provider") == provider
            ):
                candidates.setdefault(1, record)
        for priority in (0, 1):
            if priority in candidates:
                return candidates[priority]
        return {"provider": provider, "status": "unknown", "record": None}

    def get_provider_quota(self, provider: str, *, max_age_seconds: float = 300.0, now: datetime | None = None) -> dict[str, Any]:
        """Return the provider's latest quota windows without inventing limits."""
        if not provider:
            raise ValueError("provider is required")
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        effective_now = now or utc_now()
        windows: list[dict[str, Any]] = []
        records = []
        for record in self.store.latest_snapshots(subject_type="quota", limit=10000):
            values = record["values"]
            if values.get("provider") != provider and record["subject_id"] not in {provider, f"quota:{provider}"}:
                continue
            records.append(record)
        superseded_sources = _declared_quota_source_supersessions(records)
        for record in records:
            if record["source_id"] in superseded_sources:
                continue
            values = record["values"]
            freshness = assess(record["observed_at"], now=effective_now, max_age_seconds=max_age_seconds)
            observed_at = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
            try:
                window = normalize_window(
                    values,
                    provider=provider,
                    window_id=str(values.get("window_id", values.get("window_name", record["subject_id"]))),
                    observed_at=observed_at,
                    endpoint=values.get("endpoint"),
                )
            except (TypeError, ValueError) as exc:
                windows.append({"provider": provider, "window_id": values.get("window_id", record["subject_id"]), "health": "unknown", "status": "invalid", "error": str(exc), "observed_at": record["observed_at"], "source_id": record["source_id"]})
                continue
            add_reset_timing(window, observed_at=observed_at, now=effective_now)
            window["source_id"] = record["source_id"]
            window["freshness_seconds"] = freshness.age_seconds
            if freshness.state is Freshness.STALE:
                window["health"] = "stale"
                window["status"] = "stale"
            elif freshness.state is Freshness.INVALID:
                window["health"] = "unknown"
                window["status"] = "invalid"
            windows.append(window)
        # Aggregate conservatively: a healthy-looking top-level result must
        # never hide an invalid, stale, or unknown child window.
        if not windows:
            status = "unknown"
        elif any(w.get("status") == "invalid" for w in windows):
            status = "invalid"
        elif any(w.get("health") == "unknown" or w.get("status") == "unknown" for w in windows):
            status = "unknown"
        elif any(w.get("status") == "stale" or w.get("health") == "stale" for w in windows):
            status = "stale"
        else:
            status = "ok"
        result = {"provider": provider, "status": status, "windows": windows, "max_age_seconds": max_age_seconds}
        if windows:
            result["reset_context"] = summarize_reset_context(windows)
        return result

    def get_provider_quotas(self, *, max_age_seconds: float = 300.0, now: datetime | None = None) -> list[dict[str, Any]]:
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        providers = sorted({
            record["values"].get("provider")
            for record in self.store.latest_snapshots(subject_type="quota", limit=10000)
            if isinstance(record["values"].get("provider"), str) and record["values"].get("provider")
        })
        return [self.get_provider_quota(provider, max_age_seconds=max_age_seconds, now=now) for provider in providers]

    def evaluate_task_feasibility(self, task_id: str, requirements: dict[str, Any] | None = None) -> dict[str, Any]:
        return evaluate_task_feasibility(self.store, task_id, requirements)

    def get_work_constraints(self, task_id: str) -> dict[str, Any]:
        return get_work_constraints(self.store, task_id)

    def explain_task_failure(self, task_id: str) -> dict[str, Any]:
        return explain_task_failure(self.store, task_id)

    def get_retry_cost_estimate(self, task_id: str) -> dict[str, Any]:
        return get_retry_cost_estimate(self.store, task_id)

    def get_daily_summary(self) -> dict[str, Any]:
        return get_daily_summary(self.store)

    def get_recent_events(self, *, limit: int = 100) -> dict[str, Any]:
        return get_recent_events(self.store, limit=limit)

    def get_model_health(self, model_id: str) -> dict[str, Any]:
        return get_model_health(self.store, model_id)

    def get_health_projection(self, *, max_age_seconds: float = 300.0, now: datetime | None = None) -> dict[str, Any]:
        return get_health_projection(self.store, max_age_seconds=max_age_seconds, now=now)
