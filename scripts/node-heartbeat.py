#!/usr/bin/env python3
"""Publish a renewable heartbeat lease to a Resource Monitor service."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Any

from resource_monitor import AgentHeartbeat
from resource_monitor.node import NodeReporter


def parse_metadata(pairs: list[str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"metadata entry must be key=value: {pair}")
        key, value = pair.split("=", 1)
        metadata[key] = value
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("RM_BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("RM_API_TOKEN"))
    parser.add_argument("--agent-id", default=os.environ.get("RM_AGENT_ID"))
    parser.add_argument("--lease-id", default=os.environ.get("RM_LEASE_ID"))
    parser.add_argument("--ttl-seconds", type=int, default=int(os.environ.get("RM_HEARTBEAT_TTL", "90")))
    parser.add_argument("--interval-seconds", type=float, default=float(os.environ.get("RM_HEARTBEAT_INTERVAL", "30")))
    parser.add_argument("--metadata", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()

    if not args.agent_id:
        raise SystemExit("--agent-id or RM_AGENT_ID is required")
    if not args.lease_id:
        raise SystemExit("--lease-id or RM_LEASE_ID is required")
    if args.ttl_seconds <= 0:
        raise SystemExit("--ttl-seconds must be positive")
    if args.interval_seconds <= 0:
        raise SystemExit("--interval-seconds must be positive")
    if args.interval_seconds >= args.ttl_seconds:
        raise SystemExit("--interval-seconds must be lower than --ttl-seconds")

    reporter = NodeReporter(args.base_url, bearer_token=args.token)
    metadata = parse_metadata(args.metadata)
    running = True

    def handle_signal(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        heartbeat = AgentHeartbeat.create(
            agent_id=args.agent_id,
            lease_id=args.lease_id,
            ttl_seconds=args.ttl_seconds,
            metadata=metadata,
        )
        reporter.report_heartbeat(heartbeat)
        print(
            f"heartbeat {heartbeat.agent_id} lease={heartbeat.lease_id} "
            f"expires_at={heartbeat.expires_at.isoformat()}",
            flush=True,
        )
        deadline = time.monotonic() + args.interval_seconds
        while running and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    print("heartbeat loop stopped", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
