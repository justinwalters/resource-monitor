import asyncio

import pytest

from resource_monitor.mcp_server import StaticBearerTokenVerifier, create_mcp_server, run_stdio, verify_stdio_client_token
from resource_monitor.storage import SQLiteStore
from resource_monitor.core import ResourceMonitor
from resource_monitor.models import AgentHeartbeat, ResourceEvent, ResourceSnapshot
from datetime import datetime, timezone


def test_verifier_accepts_only_exact_expected_token():
    verifier = StaticBearerTokenVerifier("secret")
    accepted = asyncio.run(verifier.verify_token("secret"))
    refused = asyncio.run(verifier.verify_token("wrong"))
    assert accepted is not None
    assert accepted.scopes == ["resource.read"]
    assert refused is None


@pytest.mark.parametrize(
    ("expected", "presented", "accepted"),
    [("secret", "secret", True), ("secret", "wrong", False), ("secret", None, False), ("", "secret", False)],
)
def test_stdio_client_credential_is_fail_closed(expected, presented, accepted):
    assert verify_stdio_client_token(expected, presented) is accepted


def test_stdio_runner_requires_an_explicitly_authenticated_launcher():
    store = SQLiteStore(":memory:")
    try:
        server = create_mcp_server(ResourceMonitor(store), "secret")
        with pytest.raises(PermissionError, match="authentication gate"):
            run_stdio(server)
    finally:
        store.close()


def test_server_requires_token_and_registers_read_only_tools():
    store = SQLiteStore(":memory:")
    try:
        monitor = ResourceMonitor(store)
        with pytest.raises(ValueError, match="required"):
            create_mcp_server(monitor, "")
        server = create_mcp_server(monitor, "secret")
        assert server.name == "resource-monitor"
        assert {tool.name for tool in asyncio.run(server.list_tools())} == {
            "list_available_resources", "list_resources_near_limit",
            "get_node_status", "get_provider_status",
            "get_provider_quota", "get_provider_quotas",
            "evaluate_task_feasibility", "get_work_constraints",
            "explain_task_failure", "get_retry_cost_estimate",
            "get_daily_summary", "get_recent_events", "get_model_health",
        }
    finally:
        store.close()


def test_resource_state_methods_are_read_only_and_fail_closed():
    store = SQLiteStore(":memory:")
    try:
        monitor, now = ResourceMonitor(store), datetime.now(timezone.utc)
        monitor.ingest_snapshot(ResourceSnapshot("provider-a", "provider", now, {
            "provider": "provider-a", "capacity": 100, "capacity_remaining": 10,
        }, "test"))
        monitor.ingest_snapshot(ResourceSnapshot("host-a", "host", now, {
            "capacity_max": 100, "capacity_remaining": 80,
        }, "test"))
        store.save_heartbeat(AgentHeartbeat.create("node-a", "lease", 60, now=now))
        before = store.count("snapshots")
        assert len(monitor.list_available_resources()) == 2
        assert [r["subject_id"] for r in monitor.list_resources_near_limit()] == ["provider-a"]
        assert monitor.get_node_status("node-a")["availability"] == "online"
        assert monitor.get_node_status("missing-node")["availability"] == "unknown"
        assert monitor.get_provider_status("provider-a")["subject_id"] == "provider-a"
        assert monitor.get_provider_status("missing")["status"] == "unknown"
        assert monitor.get_provider_quota("missing")["status"] == "unknown"
        assert monitor.get_provider_quotas() == []
        assert store.count("snapshots") == before
        with pytest.raises(ValueError): monitor.list_resources_near_limit(threshold=2)
        with pytest.raises(ValueError): monitor.get_node_status("")
        with pytest.raises(ValueError): monitor.get_provider_status("")
    finally:
        store.close()


def test_mcp_quota_projection_preserves_allowlisted_provenance_only():
    store = SQLiteStore(":memory:")
    try:
        now = datetime.now(timezone.utc)
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:acme:hour", "quota", now,
            {"provider": "acme", "window_id": "hour", "unit": "requests", "total": 100, "remaining": 80,
             "source_scope": "coding-api", "feature": "quota-read",
             "source_updated_at": "2026-08-13T04:33:20Z", "window_minutes": 60,
             "secret_token": "MCP-secret", "raw_payload": {"credential": "raw"}},
            "mini-publisher",
        ))
        server = create_mcp_server(monitor, "secret")
        # Exercise the function registered as the public MCP read tool, not a
        # second copy of the projection logic.
        result = server._tool_manager._tools["get_provider_quota"].fn("acme")
        window = result["windows"][0]
        assert window["source_scope"] == "coding-api"
        assert window["feature"] == "quota-read"
        assert window["source_updated_at"] == "2026-08-13T04:33:20Z"
        assert window["window_minutes"] == 60
        serialized = str(result)
        assert "MCP-secret" not in serialized and "raw" not in serialized
        assert "raw_payload" not in window and "secret_token" not in window
    finally:
        store.close()


def test_task_intelligence_is_advisory_read_only_and_fail_closed():
    store = SQLiteStore(":memory:")
    try:
        monitor, now = ResourceMonitor(store), datetime.now(timezone.utc)
        monitor.ingest_snapshot(ResourceSnapshot("task-1", "task", now, {
            "constraints": {"memory_gb": 16, "provider": "local"},
            "retry_cost_estimate": {"tokens": 120, "seconds": 3},
        }, "test"))
        monitor.ingest_event(ResourceEvent("task_failed", "task-1", now, {"reason": "capacity"}, "test", severity="warning"))
        before = store.count("snapshots")
        assert monitor.evaluate_task_feasibility("task-1", {"memory_gb": 8})["feasible"] is True
        assert monitor.evaluate_task_feasibility("task-1", {"memory_gb": 32})["status"] == "infeasible"
        assert monitor.get_work_constraints("task-1")["status"] == "known"
        assert monitor.explain_task_failure("task-1")["failure"]["event_type"] == "task_failed"
        assert monitor.get_retry_cost_estimate("task-1")["estimate"]["tokens"] == 120
        assert monitor.get_work_constraints("missing")["status"] == "unknown"
        assert monitor.get_retry_cost_estimate("missing")["status"] == "unknown"
        assert store.count("snapshots") == before
        with pytest.raises(ValueError): monitor.evaluate_task_feasibility("")
        with pytest.raises(ValueError): monitor.evaluate_task_feasibility("task-1", [])
    finally:
        store.close()


def test_summary_tools_are_read_only_and_fail_closed():
    store = SQLiteStore(":memory:")
    try:
        monitor, now = ResourceMonitor(store), datetime.now(timezone.utc)
        monitor.ingest_snapshot(ResourceSnapshot("model-1", "model", now, {
            "model_id": "model-1", "health": "healthy",
        }, "test"))
        monitor.ingest_event(ResourceEvent("model_started", "model-1", now, {"ok": True}, "test"))
        before = (store.count("snapshots"), store.count("events"))
        assert monitor.get_daily_summary()["status"] == "known"
        assert monitor.get_daily_summary()["snapshots"]["count"] == 1
        assert monitor.get_recent_events(limit=1)["events"][0]["event_type"] == "model_started"
        assert monitor.get_model_health("model-1")["health"] == "healthy"
        assert monitor.get_model_health("missing")["status"] == "unknown"
        assert store.count("snapshots") == before[0] and store.count("events") == before[1]
        with pytest.raises(ValueError): monitor.get_recent_events(limit=0)
        with pytest.raises(ValueError): monitor.get_model_health("")
    finally:
        store.close()
