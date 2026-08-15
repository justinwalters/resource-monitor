import threading
import urllib.error
import urllib.request
import json
from datetime import datetime, timedelta, timezone

import pytest

from resource_monitor.api import _is_allowed_bind_host, serve
from resource_monitor.core import ResourceMonitor
from resource_monitor.models import ResourceSnapshot
from resource_monitor.storage import SQLiteStore


def _get(url, token=None):
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        return urllib.request.urlopen(request, timeout=2)
    except urllib.error.HTTPError as exc:
        return exc


@pytest.fixture
def running_server(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    server = serve(ResourceMonitor(store), port=0, bearer_token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def test_every_route_rejects_missing_and_wrong_tokens(running_server):
    for path in ("/health", "/v1/snapshots", "/v1/events", "/v1/agents", "/v1/agents/nope/status", "/v1/quotas", "/v1/providers/acme/quota"):
        assert _get(running_server + path).status == 401
        assert _get(running_server + path, "wrong").status == 401
    assert _get(running_server + "/health", "secret").status == 200


def test_quota_routes_are_authenticated_and_fail_closed_for_unknown_provider(running_server):
    response = _get(running_server + "/v1/providers/acme/quota", "secret")
    assert response.status == 200
    assert response.read() == b'{"max_age_seconds": 300.0, "provider": "acme", "status": "unknown", "windows": []}'
    assert _get(running_server + "/v1/quotas?max_age_seconds=-1", "secret").status == 400


def test_quota_rest_projection_preserves_allowlisted_provenance_only(tmp_path):
    now = datetime.now(timezone.utc)
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    monitor.ingest_snapshot(ResourceSnapshot(
        "quota:acme:hour", "quota", now,
        {"provider": "acme", "window_id": "hour", "unit": "requests", "total": 100, "remaining": 80,
         "source_scope": "coding-api", "feature": "quota-read",
         "source_updated_at": "2026-08-13T04:33:20Z", "window_minutes": 60,
         "api_key": "Bearer REST-secret", "credentials": {"token": "raw-secret"},
         "raw_payload": {"secret": "payload"}},
        "mini-publisher",
    ))
    server = serve(monitor, port=0, bearer_token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = _get(f"http://127.0.0.1:{server.server_port}/v1/providers/acme/quota", "secret")
        payload = json.loads(response.read())
        window = payload["windows"][0]
        assert window["source_scope"] == "coding-api"
        assert window["feature"] == "quota-read"
        assert window["source_updated_at"] == "2026-08-13T04:33:20Z"
        assert window["window_minutes"] == 60
        serialized = json.dumps(payload)
        assert "REST-secret" not in serialized and "raw-secret" not in serialized
        assert "raw_payload" not in window and "api_key" not in window
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def test_health_route_rejects_non_finite_freshness(running_server):
    for value in ("nan", "inf", "-inf"):
        assert _get(running_server + f"/v1/health?max_age_seconds={value}", "secret").status == 400


def test_health_projection_exposes_pressure_model_health_and_freshness(tmp_path):
    now = datetime.now(timezone.utc)
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    store.save_snapshot(ResourceSnapshot(
        "node-1", "host", now, {"health": "online", "cpu_percent": 12.5, "memory_percent": 40}, "local",
    ))
    store.save_snapshot(ResourceSnapshot(
        "model-1", "model", now, {"health": "healthy"}, "model-source",
    ))
    server = serve(monitor, port=0, bearer_token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = _get(f"http://127.0.0.1:{server.server_port}/v1/health", "secret")
        payload = json.loads(response.read())
        assert payload["status"] == "ok"
        assert payload["rm"]["status"] == "ok"
        assert payload["nodes"][0]["pressure"]["cpu_percent"] == 12.5
        assert payload["models"][0]["health"] == "healthy"
        assert payload["rm"]["source_id"] == "resource-monitor"
        assert "freshness_seconds" in payload["models"][0]
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def test_health_projection_marks_stale_records_degraded(tmp_path):
    now = datetime.now(timezone.utc)
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    store.save_snapshot(ResourceSnapshot("node-1", "node", now - timedelta(seconds=301), {"health": "online"}, "local"))
    server = serve(monitor, port=0, bearer_token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = _get(f"http://127.0.0.1:{server.server_port}/v1/health?max_age_seconds=300", "secret")
        payload = json.loads(response.read())
        assert payload["status"] == "degraded"
        assert payload["nodes"][0]["health"] == "stale"
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def test_latest_snapshots_keeps_subject_type_when_ids_are_reused(tmp_path):
    now = datetime.now(timezone.utc)
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        store.save_snapshot(ResourceSnapshot("shared", "host", now - timedelta(seconds=2), {"health": "online"}, "host-source"))
        store.save_snapshot(ResourceSnapshot("shared", "quota", now, {"used": 99}, "quota-source"))
        records = store.latest_snapshots(subject_type="host")
        assert len(records) == 1
        assert records[0]["subject_type"] == "host"
        assert records[0]["source_id"] == "host-source"
    finally:
        store.close()


def test_latest_snapshots_breaks_equal_timestamp_ties_deterministically(tmp_path):
    observed_at = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        store.save_snapshot(ResourceSnapshot("race", "node", observed_at, {"health": "offline"}, "writer-offline", record_id="race-offline"))
        store.save_snapshot(ResourceSnapshot("race", "node", observed_at, {"health": "online"}, "writer-online", record_id="race-online"))
        records = store.latest_snapshots(subject_type="node")
        assert len(records) == 1
        assert records[0]["source_id"] == "writer-online"
    finally:
        store.close()


def test_bind_policy_refuses_lan_and_wildcard():
    assert _is_allowed_bind_host("127.0.0.1")
    assert _is_allowed_bind_host("100.108.244.87")
    assert _is_allowed_bind_host("fd7a:115c:a1e0::fc3a:f458")
    for host in ("0.0.0.0", "192.168.0.115", "localhost.example"):
        assert not _is_allowed_bind_host(host)


def test_server_requires_token():
    store = SQLiteStore(":memory:")
    try:
        with pytest.raises(ValueError, match="required"):
            serve(ResourceMonitor(store), port=0)
    finally:
        store.close()


def test_server_refuses_lan_even_with_token():
    store = SQLiteStore(":memory:")
    try:
        with pytest.raises(ValueError, match="LAN"):
            serve(ResourceMonitor(store), host="192.168.0.115", port=0, bearer_token="secret")
    finally:
        store.close()
