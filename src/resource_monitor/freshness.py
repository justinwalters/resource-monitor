"""Explicit freshness classification for safety-sensitive consumers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math


class Freshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True)
class FreshnessResult:
    state: Freshness
    age_seconds: float | None
    reason: str


def assess(observed_at: datetime | str, *, now: datetime | None = None, max_age_seconds: float = 300.0) -> FreshnessResult:
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    try:
        timestamp = datetime.fromisoformat(observed_at) if isinstance(observed_at, str) else observed_at
        if timestamp.tzinfo is None:
            return FreshnessResult(Freshness.INVALID, None, "timestamp has no timezone")
        age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
    except (TypeError, ValueError):
        return FreshnessResult(Freshness.INVALID, None, "timestamp is not valid RFC 3339")
    if age < 0:
        return FreshnessResult(Freshness.INVALID, age, "timestamp is in the future")
    if age > max_age_seconds:
        return FreshnessResult(Freshness.STALE, age, "observation exceeds freshness budget")
    return FreshnessResult(Freshness.CURRENT, age, "observation is within freshness budget")
