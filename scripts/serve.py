#!/usr/bin/env python3
"""Run the Resource Monitor HTTP service."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from resource_monitor import CollectionScheduler, LocalSystemCollector, PollingAdapter, ResourceMonitor, SQLiteStore, serve


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("RM_BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RM_PORT", "8765")))
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("RM_DB", "data/resource-monitor.db")))
    parser.add_argument("--token-file", type=Path, default=Path(os.environ["RM_API_TOKEN_FILE"]) if os.environ.get("RM_API_TOKEN_FILE") else None)
    parser.add_argument("--collection-interval", type=float, default=float(os.environ.get("RM_COLLECTION_INTERVAL", "60")))
    args = parser.parse_args()
    token = os.environ.get("RM_API_TOKEN")
    if args.token_file is not None:
        token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        parser.error("RM_API_TOKEN or RM_API_TOKEN_FILE is required")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(args.db)
    monitor = ResourceMonitor(store)
    monitor.add_adapter(PollingAdapter("local-system", LocalSystemCollector().collect))
    scheduler = CollectionScheduler(monitor, interval_seconds=args.collection_interval)
    server = serve(monitor, host=args.host, port=args.port, bearer_token=token)
    scheduler.start()
    print(f"resource-monitor listening on {args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
