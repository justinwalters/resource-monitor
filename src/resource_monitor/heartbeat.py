"""Agent lease and recovery state derived from durable heartbeats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .models import AgentHeartbeat, utc_now
from .storage import SQLiteStore


class AgentAvailability(str, Enum):
    ONLINE = "online"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AgentStatus:
    agent_id: str
    availability: AgentAvailability
    lease_id: str | None
    sent_at: datetime | None
    expires_at: datetime | None
    metadata: dict | None
    checkpoint: dict | None

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "availability": self.availability.value,
            "lease_id": self.lease_id,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "metadata": self.metadata,
            "checkpoint": self.checkpoint,
        }


class HeartbeatManager:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def renew(self, agent_id: str, lease_id: str, ttl_seconds: int, *, now: datetime | None = None, metadata: dict | None = None) -> AgentHeartbeat:
        heartbeat = AgentHeartbeat.create(agent_id, lease_id, ttl_seconds, now=now or utc_now(), metadata=metadata)
        self.store.save_heartbeat(heartbeat)
        return heartbeat

    def status(self, agent_id: str, *, now: datetime | None = None) -> AgentStatus:
        heartbeat = self.store.get_heartbeat(agent_id)
        checkpoint = self.store.get_checkpoint(agent_id)
        if heartbeat is None:
            return AgentStatus(agent_id, AgentAvailability.UNKNOWN, None, None, None, {}, checkpoint)
        availability = AgentAvailability.ONLINE if heartbeat.is_live(now=now or utc_now()) else AgentAvailability.STALE
        return AgentStatus(
            agent_id,
            availability,
            heartbeat.lease_id,
            heartbeat.sent_at,
            heartbeat.expires_at,
            dict(heartbeat.metadata),
            checkpoint,
        )

    def statuses(self, *, now: datetime | None = None, limit: int = 100) -> list[AgentStatus]:
        return [self.status(agent_id, now=now) for agent_id in self.store.list_agent_ids(limit=limit)]
