#!/usr/bin/env python3
"""Run Resource Monitor's authenticated MCP server over stdio or HTTP."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from resource_monitor import (
    CollectionScheduler,
    LocalSystemCollector,
    PollingAdapter,
    ResourceMonitor,
    SQLiteStore,
    create_mcp_server,
    run_stdio,
    run_streamable_http,
    verify_stdio_client_token,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default=os.environ.get("RM_BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RM_PORT", "8765")))
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("RM_DB", "data/resource-monitor.db")))
    parser.add_argument("--token-file", type=Path, default=Path(os.environ["RM_API_TOKEN_FILE"]) if os.environ.get("RM_API_TOKEN_FILE") else None)
    parser.add_argument(
        "--stdio-client-token",
        default=os.environ.get("RM_MCP_CLIENT_TOKEN"),
        help="Credential presented by a native stdio parent (or set RM_MCP_CLIENT_TOKEN)",
    )
    parser.add_argument("--collection-interval", type=float, default=float(os.environ.get("RM_COLLECTION_INTERVAL", "60")))
    args = parser.parse_args()

    token = os.environ.get("RM_API_TOKEN")
    if args.token_file is not None:
        token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        parser.error("RM_API_TOKEN or RM_API_TOKEN_FILE is required")
    if args.transport == "stdio" and not verify_stdio_client_token(token, args.stdio_client_token):
        parser.error("native stdio requires RM_MCP_CLIENT_TOKEN matching the configured RM token")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(args.db)
    monitor = ResourceMonitor(store)
    monitor.add_adapter(PollingAdapter("local-system", LocalSystemCollector().collect))
    scheduler = CollectionScheduler(monitor, interval_seconds=args.collection_interval)
    scheduler.start()
    server = create_mcp_server(monitor, token)
    try:
        if args.transport == "stdio":
            run_stdio(server, authenticated=True)
        else:
            asyncio.run(run_streamable_http(server))
    finally:
        scheduler.stop()
        store.close()


if __name__ == "__main__":
    main()
