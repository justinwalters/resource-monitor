"""Small JSON HTTP transport for canonical RM ingestion.

The API is deliberately an adapter around the core. It carries no provider,
host, model, or consumer-specific logic.
"""

from __future__ import annotations

import json
from datetime import datetime
from http import HTTPStatus
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .core import ResourceMonitor
from .heartbeat import HeartbeatManager
from .models import ResourceEvent, ResourceSnapshot


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0 or length > 2_000_000:
        raise ValueError("request body must be between 1 byte and 2 MB")
    payload = json.loads(handler.rfile.read(length))
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def _write_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(monitor: ResourceMonitor, bearer_token: str | None = None) -> type[BaseHTTPRequestHandler]:
    heartbeat_manager = HeartbeatManager(monitor.store)
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            # Every route, including /health, is authenticated. This avoids making
            # health data an accidental bypass when the service is tailnet-reachable.
            return bearer_token is not None and self.headers.get("Authorization") == f"Bearer {bearer_token}"

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                _write_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                _write_json(self, HTTPStatus.OK, {"status": "ok"})
                return
            query = parse_qs(parsed.query)
            if parsed.path == "/v1/health":
                try:
                    max_age = float(query.get("max_age_seconds", ["300"])[0])
                    if not math.isfinite(max_age):
                        raise ValueError("max_age_seconds must be finite")
                    _write_json(self, HTTPStatus.OK, monitor.get_health_projection(max_age_seconds=max_age))
                except (TypeError, ValueError) as exc:
                    _write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parsed.path in {"/v1/snapshots", "/v1/events"}:
                try:
                    limit = int(query.get("limit", ["100"])[0])
                    subject_id = query.get("subject_id", [None])[0]
                    records = monitor.store.snapshots(subject_id, limit=limit) if parsed.path.endswith("snapshots") else monitor.store.events(subject_id, limit=limit)
                    _write_json(self, HTTPStatus.OK, {"records": records, "count": len(records), "limit": limit})
                except (TypeError, ValueError) as exc:
                    _write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parsed.path == "/v1/agents":
                try:
                    limit = int(query.get("limit", ["100"])[0])
                    records = [status.as_dict() for status in heartbeat_manager.statuses(limit=limit)]
                    _write_json(self, HTTPStatus.OK, {"records": records, "count": len(records), "limit": limit})
                except (TypeError, ValueError) as exc:
                    _write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parsed.path == "/v1/quotas" or (parsed.path.startswith("/v1/providers/") and parsed.path.endswith("/quota")):
                try:
                    raw_age = query.get("max_age_seconds", ["300"])[0]
                    max_age = float(raw_age)
                    if parsed.path == "/v1/quotas":
                        records = monitor.get_provider_quotas(max_age_seconds=max_age)
                    else:
                        provider = unquote(parsed.path[len("/v1/providers/"):-len("/quota")]).strip("/")
                        if not provider:
                            raise ValueError("provider is required")
                        records = monitor.get_provider_quota(provider, max_age_seconds=max_age)
                    _write_json(self, HTTPStatus.OK, records if isinstance(records, dict) else {"records": records, "count": len(records)})
                except (TypeError, ValueError) as exc:
                    _write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parsed.path.startswith("/v1/agents/") and parsed.path.endswith("/status"):
                agent_id = parsed.path[len("/v1/agents/"):-len("/status")].strip("/")
                if not agent_id:
                    _write_json(self, HTTPStatus.BAD_REQUEST, {"error": "agent_id is required"})
                    return
                _write_json(self, HTTPStatus.OK, heartbeat_manager.status(agent_id).as_dict())
                return
            _write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                _write_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                payload = _read_json(self)
                if self.path == "/v1/snapshots":
                    monitor.ingest_snapshot(ResourceSnapshot(
                        subject_id=payload["subject_id"], subject_type=payload["subject_type"],
                        observed_at=datetime.fromisoformat(payload["observed_at"]),
                        values=payload["values"], source_id=payload["source_id"],
                        schema_version=payload.get("schema_version", 1), record_id=payload.get("record_id", ResourceSnapshot.__dataclass_fields__["record_id"].default_factory()),
                    ))
                elif self.path == "/v1/events":
                    monitor.ingest_event(ResourceEvent(
                        event_type=payload["event_type"], subject_id=payload["subject_id"],
                        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
                        payload=payload["payload"], source_id=payload["source_id"],
                        severity=payload.get("severity", "info"), schema_version=payload.get("schema_version", 1),
                        event_id=payload.get("event_id", ResourceEvent.__dataclass_fields__["event_id"].default_factory()),
                    ))
                elif self.path == "/v1/heartbeats":
                    heartbeat_manager.renew(
                        payload["agent_id"], payload["lease_id"],
                        max(1, int((datetime.fromisoformat(payload["expires_at"]) - datetime.fromisoformat(payload["sent_at"])).total_seconds())),
                        now=datetime.fromisoformat(payload["sent_at"]), metadata=payload.get("metadata", {}),
                    )
                else:
                    _write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                _write_json(self, HTTPStatus.ACCEPTED, {"accepted": True})
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                _write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def log_message(self, *_args: object) -> None:
            return

    return Handler


def _is_allowed_bind_host(host: str) -> bool:
    import ipaddress

    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 4:
        return address in ipaddress.ip_network("100.64.0.0/10")
    return address in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def serve(monitor: ResourceMonitor, host: str = "127.0.0.1", port: int = 8765, bearer_token: str | None = None) -> ThreadingHTTPServer:
    if not _is_allowed_bind_host(host):
        raise ValueError("RM must bind to 127.0.0.1/::1 or an explicit Tailscale 100.x/fd7a:115c:a1e0:: address; LAN and wildcard binds are refused")
    if bearer_token is None:
        raise ValueError("RM_API_TOKEN is required; refusing to start without authentication")
    server = ThreadingHTTPServer((host, port), make_handler(monitor, bearer_token))
    return server
