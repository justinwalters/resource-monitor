from datetime import datetime, timedelta, timezone
import subprocess
import sys

from resource_monitor import ResourceMonitor, ResourceSnapshot, SQLiteStore
from resource_monitor.api import serve
from resource_monitor.heartbeat import AgentAvailability, HeartbeatManager
from resource_monitor.collection import CollectionScheduler, PollingAdapter
from resource_monitor.freshness import Freshness, assess
from resource_monitor.config import AdapterFactoryRegistry, configure_monitor
from resource_monitor.node import NodeReporter
from resource_monitor.adapters import AdapterRegistry


class FixedAdapter:
    source_id = "example-source"

    def collect(self):
        yield ResourceSnapshot(
            subject_id="subject-1",
            subject_type="provider",
            observed_at=datetime.now(timezone.utc),
            values={"capacity_remaining": 0.75},
            source_id=self.source_id,
        )


def test_adapter_data_is_durable_and_provider_neutral(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store, AdapterRegistry())
    monitor.add_adapter(FixedAdapter())

    assert monitor.collect_once() == 1
    assert store.count("snapshots") == 1
    store.close()


def test_agent_heartbeat_persists_and_expires(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    now = datetime.now(timezone.utc)

    heartbeat = monitor.heartbeat("agent-1", "lease-1", 30, now=now)
    restored = store.get_heartbeat("agent-1")

    assert restored == heartbeat
    assert restored.is_live(now=now + timedelta(seconds=29))
    assert not restored.is_live(now=now + timedelta(seconds=30))
    store.close()


def test_history_queries_and_retention(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_snapshot(ResourceSnapshot("subject-1", "provider", old, {"value": 1}, "source"))
    store.save_snapshot(ResourceSnapshot("subject-1", "provider", new, {"value": 2}, "source"))
    assert [row["values"]["value"] for row in store.snapshots("subject-1")] == [2, 1]
    assert store.prune_before(datetime(2025, 6, 1, tzinfo=timezone.utc)) == {"snapshots": 1, "events": 0}
    assert store.count("snapshots") == 1
    store.close()

    reopened = SQLiteStore(tmp_path / "rm.db")
    assert reopened.snapshots("subject-1")[0]["values"]["value"] == 2
    reopened.close()


def test_duplicate_adapter_ids_are_rejected():
    registry = AdapterRegistry()
    registry.register(FixedAdapter())
    try:
        registry.register(FixedAdapter())
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate adapter was accepted")


def test_http_transport_accepts_canonical_snapshot(tmp_path):
    import json
    from threading import Thread
    from urllib.request import Request, urlopen

    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    server = serve(monitor, port=0, bearer_token="test-token")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_port}"
    payload = {
        "subject_id": "provider-1", "subject_type": "provider",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "values": {"capacity_remaining": 0.5}, "source_id": "remote-source",
    }
    request = Request(address + "/v1/snapshots", data=json.dumps(payload).encode(), method="POST", headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"})
    with urlopen(request) as response:
        assert response.status == 202
    assert store.count("snapshots") == 1
    with urlopen(Request(address + "/v1/snapshots?subject_id=provider-1", headers={"Authorization": "Bearer test-token"})) as response:
        result = json.loads(response.read())
        assert result["count"] == 1
        assert result["records"][0]["values"]["capacity_remaining"] == 0.5
    server.shutdown()
    server.server_close()


def test_heartbeat_status_and_checkpoint_survive_reconnect(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    manager = HeartbeatManager(store)
    now = datetime.now(timezone.utc)
    manager.renew("agent-1", "lease-1", 30, now=now)
    store.save_checkpoint("agent-1", "task-1", {"step": 4, "branch": "work"}, updated_at=now)

    online = manager.status("agent-1", now=now)
    stale = manager.status("agent-1", now=now + timedelta(seconds=31))
    assert online.availability == AgentAvailability.ONLINE
    assert online.sent_at == now
    assert online.metadata == {}
    assert stale.availability == AgentAvailability.STALE
    assert stale.checkpoint["checkpoint"]["step"] == 4

    store.close()
    reopened = SQLiteStore(tmp_path / "rm.db")
    restored = HeartbeatManager(reopened).status("agent-1", now=now + timedelta(seconds=31))
    assert restored.availability == AgentAvailability.STALE
    assert restored.checkpoint["task_id"] == "task-1"
    reopened.close()


def test_polling_adapter_and_scheduler_collect_generic_source(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    calls = []

    def collect():
        calls.append(True)
        return [ResourceSnapshot("cloud-1", "provider", datetime.now(timezone.utc), {"remaining": 1}, "cloud-source")]

    monitor.add_adapter(PollingAdapter("cloud-source", collect))
    scheduler = CollectionScheduler(monitor, interval_seconds=0.01)
    assert scheduler.run_once() == 1
    assert calls == [True]
    assert store.count("snapshots") == 1
    assert scheduler.last_error is None
    store.close()


def test_freshness_rejects_stale_future_and_timezone_less_data():
    now = datetime.now(timezone.utc)
    current = assess(now - timedelta(seconds=5), now=now, max_age_seconds=10)
    assert current.state == Freshness.CURRENT
    assert current.reason == "observation is within freshness budget"
    assert assess(now - timedelta(seconds=10), now=now, max_age_seconds=10).state == Freshness.CURRENT
    assert assess(now - timedelta(seconds=11), now=now, max_age_seconds=10).state == Freshness.STALE
    assert assess(now + timedelta(seconds=1), now=now).state == Freshness.INVALID
    assert assess("2026-01-01T00:00:00", now=now).state == Freshness.INVALID


def test_provider_status_prefers_provider_auth_over_newer_quota_and_ignores_other_values_provider(tmp_path):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    try:
        auth = ResourceSnapshot(
            "provider-auth:acme", "provider-auth", now - timedelta(minutes=1),
            {"provider": "acme", "status": "authenticated"}, "provider-auth-source",
        )
        quota = ResourceSnapshot(
            "quota:acme:hour", "quota", now,
            {"provider": "acme", "status": "critical"}, "quota-source",
        )
        usage = ResourceSnapshot(
            "usage:acme", "provider-usage", now + timedelta(seconds=1),
            {"provider": "acme", "status": "critical"}, "usage-source",
        )
        model = ResourceSnapshot(
            "model:acme", "model", now + timedelta(seconds=2),
            {"provider": "acme", "status": "critical"}, "model-source",
        )
        host = ResourceSnapshot(
            "host:acme", "host", now + timedelta(seconds=3),
            {"provider": "acme", "status": "critical"}, "host-source",
        )
        for snapshot in (auth, quota, usage, model, host):
            monitor.ingest_snapshot(snapshot)

        result = monitor.get_provider_status("acme")
        assert result["subject_type"] == "provider-auth"
        assert result["values"]["status"] == "authenticated"
        assert result["source_id"] == "provider-auth-source"
    finally:
        store.close()


def test_provider_status_preserves_provider_identity_and_prefers_provider_auth(tmp_path):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    try:
        provider = ResourceSnapshot(
            "widgetco", "provider", now, {"status": "legacy-ok"}, "provider-source",
        )
        monitor.ingest_snapshot(provider)
        assert monitor.get_provider_status("widgetco") == provider.as_dict()

        auth = ResourceSnapshot(
            "provider-auth:widgetco", "provider-auth", now - timedelta(minutes=1),
            {"provider": "widgetco", "status": "authenticated"}, "auth-source",
        )
        monitor.ingest_snapshot(auth)
        result = monitor.get_provider_status("widgetco")
        assert result["subject_type"] == "provider-auth"
        assert result["source_id"] == "auth-source"
        assert monitor.get_provider_status("missing") == {"provider": "missing", "status": "unknown", "record": None}
    finally:
        store.close()


def test_configuration_builds_deployment_defined_adapter(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    factories = AdapterFactoryRegistry()
    factories.register("test-source", lambda config: PollingAdapter(config["source_id"], lambda: []))
    scheduler = configure_monitor(monitor, {"collection_interval_seconds": 5, "sources": [{"type": "test-source", "source_id": "source-1"}]}, factories)
    assert scheduler.interval_seconds == 5
    assert monitor.adapters.collect_all() == []
    store.close()


def test_node_reporter_posts_snapshot_and_heartbeat_to_rm_server(tmp_path):
    from threading import Thread
    from resource_monitor.heartbeat import HeartbeatManager

    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    server = serve(monitor, port=0, bearer_token="node-token")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reporter = NodeReporter(f"http://127.0.0.1:{server.server_port}", bearer_token="node-token")
    now = datetime.now(timezone.utc)
    reporter.report_snapshot(ResourceSnapshot("node-1", "node", now, {"health": "online"}, "node-source"))
    reporter.report_heartbeat(HeartbeatManager(store).renew("agent-1", "lease-1", 30, now=now))
    assert store.count("snapshots") == 1
    assert store.get_heartbeat("agent-1") is not None
    server.shutdown()
    server.server_close()


def test_http_transport_exposes_agent_status_endpoints(tmp_path):
    import json
    from threading import Thread
    from urllib.request import Request, urlopen

    store = SQLiteStore(tmp_path / "rm.db")
    monitor = ResourceMonitor(store)
    manager = HeartbeatManager(store)
    now = datetime.now(timezone.utc)
    manager.renew("agent-1", "lease-1", 30, now=now, metadata={"role": "client"})
    store.save_checkpoint("agent-1", "task-1", {"step": 2}, updated_at=now)
    server = serve(monitor, port=0, bearer_token="status-token")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_port}"

    with urlopen(Request(address + "/v1/agents/agent-1/status", headers={"Authorization": "Bearer status-token"})) as response:
        status = json.loads(response.read())
        assert status["agent_id"] == "agent-1"
        assert status["availability"] == "online"
        assert status["metadata"]["role"] == "client"
        assert status["checkpoint"]["task_id"] == "task-1"

    with urlopen(Request(address + "/v1/agents?limit=10", headers={"Authorization": "Bearer status-token"})) as response:
        listing = json.loads(response.read())
        assert listing["count"] == 1
        assert listing["records"][0]["agent_id"] == "agent-1"

    server.shutdown()
    server.server_close()


def test_node_heartbeat_cli_rejects_invalid_interval():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/node-heartbeat.py",
            "--agent-id",
            "agent-1",
            "--lease-id",
            "lease-1",
            "--ttl-seconds",
            "30",
            "--interval-seconds",
            "30",
        ],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "interval-seconds" in result.stderr
