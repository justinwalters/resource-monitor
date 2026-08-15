"""Canonical, integration-neutral telemetry records.

These models deliberately avoid naming any IDE, harness, vendor, model, or
machine. Integrations attach those identities as opaque configuration values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HealthState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResourceSnapshot:
    subject_id: str
    subject_type: str
    observed_at: datetime
    values: Mapping[str, Any]
    source_id: str
    schema_version: int = SCHEMA_VERSION
    record_id: str = field(default_factory=lambda: str(uuid4()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "schema_version": self.schema_version,
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "observed_at": self.observed_at.isoformat(),
            "source_id": self.source_id,
            "values": dict(self.values),
        }


@dataclass(frozen=True)
class ResourceEvent:
    event_type: str
    subject_id: str
    occurred_at: datetime
    payload: Mapping[str, Any]
    source_id: str
    severity: str = "info"
    schema_version: int = SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: str(uuid4()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "subject_id": self.subject_id,
            "occurred_at": self.occurred_at.isoformat(),
            "source_id": self.source_id,
            "severity": self.severity,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class AgentHeartbeat:
    agent_id: str
    lease_id: str
    sent_at: datetime
    expires_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, agent_id: str, lease_id: str, ttl_seconds: int, *, now: datetime | None = None, metadata: Mapping[str, Any] | None = None) -> "AgentHeartbeat":
        sent_at = now or utc_now()
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        return cls(agent_id, lease_id, sent_at, sent_at + timedelta(seconds=ttl_seconds), metadata or {})

    def is_live(self, *, now: datetime | None = None) -> bool:
        return (now or utc_now()) < self.expires_at
