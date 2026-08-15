"""Provider quota normalization and read-model helpers.

This module deliberately does not authenticate to providers.  The mini-only
collector owns credentials and submits canonical ``quota`` snapshots; RM
normalizes and serves those observations to consumers on either machine.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

_SAFE_HEALTH = {"online", "offline", "degraded", "unknown", "stale"}
_SAFE_STATUS = {"ok", "available", "authenticated", "error", "unknown", "stale", "invalid"}
_SAFE_PROVENANCE_TEXT = {"source_scope", "feature"}
_RESET_DURATION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([smhd])$", re.IGNORECASE)


def _safe_text(value: Any, name: str, *, max_length: int = 256) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > max_length or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} must be a short non-empty string")
    return value


def _safe_source_timestamp(value: Any) -> str | None:
    value = _safe_text(value, "source_updated_at")
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("source_updated_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("source_updated_at must include a timezone")
    return value

def _number(value: Any, name: str) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def normalize_window(
    raw: Mapping[str, Any], *, provider: str, window_id: str,
    observed_at: datetime, endpoint: str | None = None,
) -> dict[str, Any]:
    """Return one canonical quota window, failing closed on contradictions.

    A direct provider percentage is authoritative only when it is finite and
    in range.  Otherwise percentages are calculated from a reported total and
    used/remaining values.  Token logs without a provider limit are therefore
    not sufficient to produce a quota percentage.
    """
    if not provider or not window_id:
        raise ValueError("provider and window_id are required")
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    unit = raw.get("unit")
    if not isinstance(unit, str) or not unit:
        raise ValueError("unit is required")
    authority = raw.get("authority", "observed")
    if authority not in {"observed", "derived", "authoritative"}:
        raise ValueError("authority is invalid")
    confidence = _number(raw.get("confidence", 1.0), "confidence")
    if confidence is None or confidence > 1:
        raise ValueError("confidence must be between 0 and 1")
    total = _number(raw.get("total", raw.get("capacity_max")), "total")
    used = _number(raw.get("used", raw.get("usage_total")), "used")
    remaining = _number(raw.get("remaining", raw.get("capacity_remaining")), "remaining")
    if total is not None and total <= 0:
        raise ValueError("total must be positive")
    if total is not None and used is not None and remaining is not None:
        tolerance = max(1e-9, total * 1e-6)
        if abs((used + remaining) - total) > tolerance:
            raise ValueError("used and remaining contradict total")
    if total is None and (used is not None or remaining is not None):
        raise ValueError("total is required when used or remaining is reported")

    direct = raw.get("used_percent", raw.get("usedPercent"))
    if direct is not None:
        direct = _number(direct, "used_percent")
        if direct is None or direct > 100:
            raise ValueError("used_percent must be between 0 and 100")
        used_percent = direct
    elif total is not None and used is not None:
        used_percent = used / total * 100
    elif total is not None and remaining is not None:
        used_percent = (total - remaining) / total * 100
    else:
        used_percent = None

    if remaining is None and total is not None and used is not None:
        remaining = total - used
    if used is None and total is not None and remaining is not None:
        used = total - remaining
    if remaining is not None and total is not None and remaining > total:
        raise ValueError("remaining cannot exceed total")
    if used is not None and total is not None and used > total:
        raise ValueError("used cannot exceed total")
    health = raw.get("health", "online")
    if not isinstance(health, str) or health not in _SAFE_HEALTH:
        raise ValueError("health is invalid")
    result: dict[str, Any] = {
        "provider": provider,
        "window_id": window_id,
        "unit": unit,
        "authority": authority,
        "confidence": confidence,
        "health": health,
        "observed_at": observed_at.isoformat(),
    }
    if endpoint:
        result["endpoint"] = _safe_text(endpoint, "endpoint")
    for key, value in (("used", used), ("total", total), ("remaining", remaining), ("used_percent", used_percent)):
        if value is not None:
            result[key] = round(value, 6)
    if used_percent is not None:
        result["remaining_percent"] = round(100 - used_percent, 6)
    for key in ("reset_at", "window"):
        value = _safe_text(raw.get(key), key)
        if value is not None:
            result[key] = value
    status = raw.get("status")
    if status is not None:
        if not isinstance(status, str) or status not in _SAFE_STATUS:
            raise ValueError("status is invalid")
        result["status"] = status

    # Keep provenance intentionally boring: only the named, first-party
    # metadata fields and only their scalar forms may cross the public boundary.
    for key in _SAFE_PROVENANCE_TEXT:
        value = _safe_text(raw.get(key), key)
        if value is not None:
            result[key] = value
    source_updated_at = _safe_source_timestamp(raw.get("source_updated_at"))
    if source_updated_at is not None:
        result["source_updated_at"] = source_updated_at
    window_minutes = raw.get("window_minutes")
    if window_minutes is not None:
        window_minutes = _number(window_minutes, "window_minutes")
        if window_minutes is None:
            raise ValueError("window_minutes must be numeric")
        result["window_minutes"] = round(window_minutes, 6)
    return result


def _resolve_reset_at(value: str, *, observed_at: datetime) -> datetime | None:
    """Resolve an absolute provider timestamp or a relative rate-limit duration."""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    match = _RESET_DURATION_RE.fullmatch(text)
    if not match:
        return None
    amount = float(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    return observed_at.astimezone(timezone.utc) + timedelta(seconds=seconds)


def add_reset_timing(window: dict[str, Any], *, observed_at: datetime, now: datetime) -> dict[str, Any]:
    """Add machine-readable reset timing without replacing the provider value.

    ``reset_at`` remains the raw provider value for auditability.  Absolute
    timestamps and relative values such as ``1m`` also receive a UTC target and
    a non-negative countdown evaluated at the time the read model is served.
    Unparseable or absent reset values remain explicitly unknown.
    """
    raw_reset_at = window.get("reset_at")
    if not isinstance(raw_reset_at, str):
        window["reset_status"] = "unknown"
        return window
    target = _resolve_reset_at(raw_reset_at, observed_at=observed_at)
    if target is None:
        window["reset_status"] = "unknown"
        return window
    current = now.astimezone(timezone.utc)
    seconds_until = max(0.0, (target - current).total_seconds())
    window["reset_at_utc"] = target.isoformat().replace("+00:00", "Z")
    window["seconds_until_reset"] = round(seconds_until, 3)
    window["reset_status"] = "due" if target <= current else "scheduled"
    return window


def summarize_reset_context(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize reset coverage for a provider without treating unknown as zero."""
    known = [
        window for window in windows
        if window.get("reset_status") in {"due", "scheduled"}
        and window.get("health") not in {"stale", "unknown"}
        and window.get("status") not in {"stale", "invalid", "unknown"}
    ]
    unknown_count = len(windows) - len(known)
    context: dict[str, Any] = {
        "status": "unknown" if not known else ("partial" if unknown_count else "complete"),
        "known_windows": len(known),
        "unknown_windows": unknown_count,
    }
    if known:
        next_window = min(known, key=lambda window: window["seconds_until_reset"])
        context["next_reset_at"] = next_window["reset_at_utc"]
        context["seconds_until_next_reset"] = next_window["seconds_until_reset"]
    return context


def parse_orca_rate_limits(payload: Mapping[str, Any], provider: str, *, observed_at: datetime, endpoint: str = "orca://account") -> list[dict[str, Any]]:
    """Parse Orca's ``result.rateLimits.<provider>.<window>`` shape."""
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return []
    rate_limits = result.get("rateLimits")
    provider_limits = rate_limits.get(provider) if isinstance(rate_limits, Mapping) else None
    if not isinstance(provider_limits, Mapping):
        return []
    # Orca integrations may signal a failed provider lookup with a bare
    # boolean rather than the newer string ``status`` field.  Do not silently
    # turn ``ok: false`` into an empty successful-looking observation.
    provider_ok = provider_limits.get("ok")
    if provider_ok is not None and not isinstance(provider_ok, bool):
        raise ValueError("Orca provider ok must be boolean")
    if provider_ok is False:
        raise ValueError(str(provider_limits.get("error") or "Orca provider reported ok=false"))
    provider_status = provider_limits.get("status")
    provider_error = provider_limits.get("error")
    rows: list[dict[str, Any]] = []
    for window_id, raw in provider_limits.items():
        if window_id in {"status", "ok", "error", "updatedAt"} or not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if "usedPercent" in item and "used_percent" not in item:
            item["used_percent"] = item["usedPercent"]
        if "unit" not in item:
            item["unit"] = "percent"
        if "limit" in item and "total" not in item:
            item["total"] = item["limit"]
        if "remainingPercent" in item and "used_percent" not in item:
            item["used_percent"] = 100 - float(item["remainingPercent"])
        if provider_status not in (None, "ok", "available", "authenticated"):
            item.update(health="unknown", status=provider_status, error=provider_error)
        if provider_limits.get("updatedAt"):
            item["source_updated_at"] = provider_limits["updatedAt"]
        rows.append(normalize_window(item, provider=provider, window_id=str(window_id), observed_at=observed_at, endpoint=endpoint))
    return rows


def parse_rate_limit_headers(headers: Mapping[str, Any], *, provider: str, observed_at: datetime, endpoint: str, prefix: str = "x-ratelimit") -> list[dict[str, Any]]:
    """Parse limit/remaining headers from common provider REST APIs."""
    lowered = {str(key).lower(): value for key, value in headers.items()}
    base = prefix.lower().rstrip("-")
    found: list[dict[str, Any]] = []
    limit_keys = [key for key in lowered if key.startswith(base + "-limit")]
    for limit_key in limit_keys:
        suffix = limit_key[len(base + "-limit"):].lstrip("-") or "default"
        remaining_key = f"{base}-remaining-{suffix}" if suffix != "default" else f"{base}-remaining"
        if remaining_key not in lowered:
            continue
        raw: dict[str, Any] = {"unit": "tokens" if suffix == "tokens" else "requests", "total": lowered[limit_key], "remaining": lowered[remaining_key]}
        reset_key = f"{base}-reset-{suffix}" if suffix != "default" else f"{base}-reset"
        if reset_key in lowered:
            raw["reset_at"] = lowered[reset_key]
        found.append(normalize_window(raw, provider=provider, window_id=suffix, observed_at=observed_at, endpoint=endpoint))
    return found
