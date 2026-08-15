"""Read-only, provider-neutral task intelligence derived from RM records."""

from __future__ import annotations

from typing import Any, Mapping


def _require_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id is required")
    return task_id.strip()


def _task_records(store: Any, task_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    task_id = _require_task_id(task_id)
    return store.snapshots(task_id, limit=10000), store.events(task_id, limit=10000)


def get_work_constraints(store: Any, task_id: str) -> dict[str, Any]:
    snapshots, _ = _task_records(store, task_id)
    for record in snapshots:
        values = record.get("values", {})
        constraints = values.get("constraints")
        if isinstance(constraints, Mapping):
            return {"task_id": task_id, "status": "known", "constraints": dict(constraints), "source": record}
    return {"task_id": task_id, "status": "unknown", "constraints": {}, "source": None}


def explain_task_failure(store: Any, task_id: str) -> dict[str, Any]:
    _, events = _task_records(store, task_id)
    for event in events:
        event_type = str(event.get("event_type", "")).lower()
        payload = event.get("payload", {})
        if "fail" in event_type or (isinstance(payload, Mapping) and payload.get("failed") is True):
            return {"task_id": task_id, "status": "known", "failure": event}
    return {"task_id": task_id, "status": "unknown", "failure": None}


def get_retry_cost_estimate(store: Any, task_id: str) -> dict[str, Any]:
    snapshots, events = _task_records(store, task_id)
    for record in snapshots:
        values = record.get("values", {})
        estimate = values.get("retry_cost_estimate")
        if isinstance(estimate, Mapping):
            return {"task_id": task_id, "status": "known", "estimate": dict(estimate), "source": record}
    for event in events:
        payload = event.get("payload", {})
        estimate = payload.get("retry_cost_estimate") if isinstance(payload, Mapping) else None
        if isinstance(estimate, Mapping):
            return {"task_id": task_id, "status": "known", "estimate": dict(estimate), "source": event}
    return {"task_id": task_id, "status": "unknown", "estimate": None, "source": None}


def evaluate_task_feasibility(store: Any, task_id: str, requirements: Mapping[str, Any] | None = None) -> dict[str, Any]:
    task_id = _require_task_id(task_id)
    requirements = {} if requirements is None else requirements
    if not isinstance(requirements, Mapping):
        raise ValueError("requirements must be an object")
    constraints = get_work_constraints(store, task_id)
    reasons: list[str] = []
    for key, required in requirements.items():
        actual = constraints["constraints"].get(key)
        if actual is None:
            reasons.append(f"missing constraint: {key}")
        elif isinstance(required, (int, float)) and isinstance(actual, (int, float)) and actual < required:
            reasons.append(f"constraint below requirement: {key}")
        elif actual != required and not isinstance(required, (int, float)):
            reasons.append(f"constraint mismatch: {key}")
    status = "infeasible" if reasons else ("feasible" if constraints["status"] == "known" or not requirements else "unknown")
    return {"task_id": task_id, "status": status, "feasible": status == "feasible", "reasons": reasons, "constraints": constraints}
