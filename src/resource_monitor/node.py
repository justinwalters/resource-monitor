"""Outbound reporter for computers or services publishing to RM."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen
from typing import Any

from .models import AgentHeartbeat, ResourceSnapshot


class NodeReporter:
    def __init__(self, base_url: str, *, bearer_token: str | None = None, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(f"{self.base_url}{path}", data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urlopen(request, timeout=self.timeout) as response:
            if response.status >= 300:
                raise RuntimeError(f"RM rejected report with HTTP {response.status}")

    def report_snapshot(self, snapshot: ResourceSnapshot) -> None:
        self._post("/v1/snapshots", snapshot.as_dict())

    def report_heartbeat(self, heartbeat: AgentHeartbeat) -> None:
        self._post("/v1/heartbeats", {
            "agent_id": heartbeat.agent_id,
            "lease_id": heartbeat.lease_id,
            "sent_at": heartbeat.sent_at.isoformat(),
            "expires_at": heartbeat.expires_at.isoformat(),
            "metadata": dict(heartbeat.metadata),
        })

