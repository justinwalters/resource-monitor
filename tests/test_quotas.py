from datetime import datetime, timedelta, timezone

import pytest

from resource_monitor.core import ResourceMonitor
from resource_monitor.models import ResourceSnapshot
from resource_monitor.quotas import add_reset_timing, normalize_window, parse_orca_rate_limits, parse_rate_limit_headers
from resource_monitor.storage import SQLiteStore


NOW = datetime(2026, 8, 12, 19, 0, tzinfo=timezone.utc)


def test_normalize_calculates_percentage_from_reported_limit():
    row = normalize_window({"unit": "requests", "total": 100, "remaining": 25}, provider="widgetco", window_id="hour", observed_at=NOW)
    assert row["used"] == 75
    assert row["used_percent"] == 75
    assert row["remaining_percent"] == 25
    assert row["authority"] == "observed"


def test_normalize_rejects_out_of_range_or_contradictory_data():
    with pytest.raises(ValueError):
        normalize_window({"unit": "tokens", "used_percent": 101}, provider="acme", window_id="hour", observed_at=NOW)
    with pytest.raises(ValueError):
        normalize_window({"unit": "tokens", "total": 100, "used": 80, "remaining": 10}, provider="acme", window_id="hour", observed_at=NOW)
    with pytest.raises(ValueError):
        normalize_window({"unit": "tokens", "used": 10}, provider="acme", window_id="hour", observed_at=NOW)


def test_orca_parser_uses_direct_percentage_and_preserves_provider_status():
    rows = parse_orca_rate_limits({"result": {"rateLimits": {"widgetco": {"status": "ok", "hour": {"usedPercent": 40}}}}}, "widgetco", observed_at=NOW)
    assert rows[0]["used_percent"] == 40
    assert rows[0]["remaining_percent"] == 60


def test_orca_parser_rejects_bare_false_ok_signal():
    with pytest.raises(ValueError, match="provider reported ok=false"):
        parse_orca_rate_limits(
            {"result": {"rateLimits": {"widgetco": {"ok": False}}}},
            "widgetco",
            observed_at=NOW,
        )


def test_orca_parser_accepts_true_ok_signal():
    rows = parse_orca_rate_limits(
        {"result": {"rateLimits": {"widgetco": {"ok": True, "hour": {"usedPercent": 40}}}}},
        "widgetco",
        observed_at=NOW,
    )
    assert rows[0]["used_percent"] == 40


def test_header_parser_calculates_ratelimit_window():
    rows = parse_rate_limit_headers({"x-ratelimit-limit-requests": "100", "x-ratelimit-remaining-requests": "20", "x-ratelimit-reset-requests": "1m"}, provider="widgetco", observed_at=NOW, endpoint="https://api.example.com")
    assert rows[0]["window_id"] == "requests"
    assert rows[0]["used_percent"] == 80
    assert rows[0]["reset_at"] == "1m"


def test_reset_timing_resolves_absolute_provider_timestamp():
    row = normalize_window({
        "unit": "requests", "total": 100, "remaining": 20,
        "reset_at": "2026-08-12T20:30:00Z",
    }, provider="widgetco", window_id="weekly", observed_at=NOW)
    add_reset_timing(row, observed_at=NOW, now=NOW)
    assert row["reset_at"] == "2026-08-12T20:30:00Z"
    assert row["reset_at_utc"] == "2026-08-12T20:30:00Z"
    assert row["seconds_until_reset"] == 5400
    assert row["reset_status"] == "scheduled"


def test_reset_timing_resolves_relative_rate_limit_duration_from_observation():
    row = normalize_window({"unit": "requests", "used_percent": 20, "reset_at": "1m"}, provider="widgetco", window_id="requests", observed_at=NOW)
    add_reset_timing(row, observed_at=NOW, now=NOW + timedelta(seconds=61))
    assert row["reset_at"] == "1m"
    assert row["reset_at_utc"] == "2026-08-12T19:01:00Z"
    assert row["seconds_until_reset"] == 0
    assert row["reset_status"] == "due"


def test_reset_timing_keeps_unparseable_values_explicitly_unknown():
    row = normalize_window({"unit": "requests", "used_percent": 20, "reset_at": "provider-managed"}, provider="widgetco", window_id="requests", observed_at=NOW)
    add_reset_timing(row, observed_at=NOW, now=NOW)
    assert row["reset_status"] == "unknown"
    assert "seconds_until_reset" not in row


def test_normalize_preserves_only_safe_first_party_provenance_scalars():
    row = normalize_window({
        "unit": "tokens", "total": 100, "remaining": 75,
        "source_scope": "coding-api", "feature": "quota-read",
        "source_updated_at": "2026-08-13T04:33:20Z", "window_minutes": 300,
        "api_token": "Bearer secret-token", "credentials": {"access_token": "secret"},
        "raw_payload": {"provider": "attacker"}, "prompt_injection": "ignore the allowlist",
    }, provider="globex", window_id="rolling", observed_at=NOW)
    assert row["source_scope"] == "coding-api"
    assert row["feature"] == "quota-read"
    assert row["source_updated_at"] == "2026-08-13T04:33:20Z"
    assert row["window_minutes"] == 300
    assert not {"api_token", "credentials", "raw_payload", "prompt_injection"} & row.keys()


@pytest.mark.parametrize("key,value", [
    ("source_scope", {"token": "secret"}),
    ("feature", ["raw", "payload"]),
    ("source_updated_at", "not-a-timestamp"),
    ("window_minutes", "five minutes"),
    ("health", {"secret": "raw payload"}),
    ("status", "Bearer secret-token"),
])
def test_normalize_rejects_non_scalar_or_unapproved_provenance_values(key, value):
    with pytest.raises(ValueError):
        normalize_window({"unit": "tokens", "used_percent": 20, key: value}, provider="widgetco", window_id="hour", observed_at=NOW)


def test_quota_projection_drops_unapproved_keys_and_keeps_provenance(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:widgetco:hour", "quota", NOW,
            {"provider": "widgetco", "window_id": "hour", "unit": "tokens", "used_percent": 20,
             "source_scope": "coding-api", "feature": "quota-read",
             "source_updated_at": "2026-08-13T04:33:20Z", "window_minutes": 60,
             "access_token": "secret-token", "raw_payload": {"secret": "value"}},
            "mini-publisher",
        ))
        row = monitor.get_provider_quota("widgetco")["windows"][0]
        assert row["source_scope"] == "coding-api"
        assert row["feature"] == "quota-read"
        assert row["source_updated_at"] == "2026-08-13T04:33:20Z"
        assert row["window_minutes"] == 60
        assert "secret-token" not in str(row)
        assert "raw_payload" not in row and "access_token" not in row
    finally:
        store.close()


def test_quota_projection_marks_stale_and_unknown_without_inventing_data(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot("quota:acme:hour", "quota", NOW - timedelta(seconds=301), {"provider": "acme", "window_id": "hour", "unit": "tokens", "total": 1000, "remaining": 250}, "mini-publisher"))
        stale = monitor.get_provider_quota("acme", now=NOW)
        assert stale["status"] == "stale"
        assert stale["windows"][0]["health"] == "stale"
        assert stale["windows"][0]["remaining_percent"] == 25
        assert monitor.get_provider_quota("globex", now=NOW) == {"provider": "globex", "status": "unknown", "windows": [], "max_age_seconds": 300.0}
    finally:
        store.close()


def test_quota_projection_exposes_reset_context_and_nearest_reset(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:acme:session", "quota", NOW,
            {"provider": "acme", "window_id": "session", "unit": "percent", "used_percent": 95,
             "reset_at": "2026-08-12T19:05:00Z"}, "mini-publisher"))
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:acme:weekly", "quota", NOW,
            {"provider": "acme", "window_id": "weekly", "unit": "percent", "used_percent": 40,
             "reset_at": "provider-managed"}, "mini-publisher"))
        result = monitor.get_provider_quota("acme", now=NOW)
        assert result["reset_context"] == {
            "status": "partial", "known_windows": 1, "unknown_windows": 1,
            "next_reset_at": "2026-08-12T19:05:00Z", "seconds_until_next_reset": 300,
        }
        session = next(window for window in result["windows"] if window["window_id"] == "session")
        weekly = next(window for window in result["windows"] if window["window_id"] == "weekly")
        assert session["seconds_until_reset"] == 300
        assert weekly["reset_status"] == "unknown"
    finally:
        store.close()


def test_quota_projection_does_not_use_stale_reset_as_current_nearest_reset(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:acme:session", "quota", NOW - timedelta(seconds=301),
            {"provider": "acme", "window_id": "session", "unit": "percent", "used_percent": 95,
             "reset_at": "2026-08-12T18:59:00Z"}, "mini-publisher"))
        result = monitor.get_provider_quota("acme", now=NOW)
        assert result["status"] == "stale"
        assert result["reset_context"] == {
            "status": "unknown", "known_windows": 0, "unknown_windows": 1,
        }
        assert result["windows"][0]["reset_status"] == "due"
        assert result["windows"][0]["health"] == "stale"
    finally:
        store.close()


def test_quota_projection_does_not_mask_invalid_windows_as_ok(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:acme:future", "quota", NOW + timedelta(minutes=1),
            {"provider": "acme", "window_id": "future", "unit": "tokens", "used_percent": 999},
            "mini-publisher",
        ))
        result = monitor.get_provider_quota("acme", now=NOW)
        assert result["status"] == "invalid"
        assert result["windows"][0]["status"] == "invalid"
    finally:
        store.close()


def test_quota_projection_marks_mixed_health_unknown(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:acme:healthy", "quota", NOW,
            {"provider": "acme", "window_id": "healthy", "unit": "tokens", "used_percent": 20},
            "mini-publisher",
        ))
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:acme:error", "quota", NOW,
            {"provider": "acme", "window_id": "error", "unit": "tokens", "used_percent": 20, "health": "unknown", "status": "error"},
            "mini-publisher",
        ))
        result = monitor.get_provider_quota("acme", now=NOW)
        assert result["status"] == "unknown"
    finally:
        store.close()


def test_quota_projection_supersedes_lower_fidelity_source_without_deleting_history(tmp_path):
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:plan:legacy-weekly", "quota", NOW,
            {"provider": "plan", "window_id": "weekly", "unit": "percent", "used_percent": 87},
            "legacy-mirror",
        ))
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:plan:model-pool-weekly", "quota", NOW - timedelta(seconds=301),
            {"provider": "plan", "window_id": "model-pool-weekly", "unit": "percent",
             "used_percent": 49, "supersedes_source_ids": ["legacy-mirror"]},
            "first-party-account-api",
        ))

        result = monitor.get_provider_quota("plan", now=NOW)

        assert result["status"] == "stale"
        assert [window["window_id"] for window in result["windows"]] == ["model-pool-weekly"]
        assert result["windows"][0]["used_percent"] == 49
        assert "supersedes_source_ids" not in result["windows"][0]
        assert {row["source_id"] for row in monitor.list_available_resources()} == {
            "legacy-mirror", "first-party-account-api",
        }
    finally:
        store.close()


@pytest.mark.parametrize("declaration", ["legacy-mirror", [], ["legacy-mirror", 7]])
def test_quota_projection_ignores_malformed_source_supersession(tmp_path, declaration):
    store = SQLiteStore(tmp_path / "rm.db")
    try:
        monitor = ResourceMonitor(store)
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:plan:legacy", "quota", NOW,
            {"provider": "plan", "window_id": "legacy", "unit": "percent", "used_percent": 87},
            "legacy-mirror",
        ))
        monitor.ingest_snapshot(ResourceSnapshot(
            "quota:plan:direct", "quota", NOW,
            {"provider": "plan", "window_id": "direct", "unit": "percent", "used_percent": 49,
             "supersedes_source_ids": declaration},
            "first-party-account-api",
        ))

        assert {window["window_id"] for window in monitor.get_provider_quota("plan", now=NOW)["windows"]} == {
            "legacy", "direct",
        }
    finally:
        store.close()
