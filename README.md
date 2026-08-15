# Resource Monitor

Resource Monitor is a provider-neutral telemetry and health-history core. It
does not depend on an IDE, agent harness, model family, cloud vendor, or
particular computer. Those systems publish data through adapters and consume
the canonical API later.

This is the public repository. It must remain safe to publish and fork.
Personal deployment configuration, credentials, raw operational logs, and
GitHub Pages control-panel data belong in a separate private integration
repository.

## Current slice

- Versioned canonical snapshots and events.
- Pluggable `TelemetryAdapter` protocol.
- Durable SQLite storage with WAL mode.
- Agent heartbeat leases that survive client disconnects and can be checked for
  expiry.
- Canonical schema documentation and JSON Schemas for snapshots, events, and
  heartbeat leases.
- No provider credentials in telemetry records.
- Standard-library runtime with no vendor SDK dependency.

## Boundary rules

Adapters may identify a vendor, service, host, model, process, or harness in
opaque configuration and telemetry values. The core must not import or name
those systems. A new provider or computer should require an adapter and
configuration, not a core change.

The next layer will expose the same records over a versioned HTTP API and MCP
adapter. Storage, collection, and presentation remain separate so an Orca
integration can be added without making Resource Monitor an Orca subsystem.

The private integration repository will consume canonical RM data and own:

- deployment and machine configuration;
- provider credentials and secret-backed adapters;
- raw and sanitized operational history;
- Gantt/control-panel exports and GitHub Pages publishing;
- private retention, backup, and access-control policy.

See [`docs/canonical-schema.md`](docs/canonical-schema.md) and [`schemas/`](schemas/)
for the public compatibility contract.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
```

## Run the service

```bash
export RM_API_TOKEN_FILE="$HOME/.config/resource-monitor/api-token"
python scripts/serve.py --host 127.0.0.1 --port 8765 --db data/resource-monitor.db --collection-interval 60
```

Bind to a private LAN/Tailscale interface only through deployment
configuration. Do not commit the token or expose the service publicly.
The service registers the local CPU, memory, and disk collector and starts its scheduler;
RM_COLLECTION_INTERVAL or --collection-interval controls the polling interval.

## Install as a background service

Use the included `launchd` template to keep the RM service alive on an
always-on host:

```bash
./deploy/install-service.sh \
  --python "$PWD/.venv/bin/python" \
  --host 127.0.0.1 \
  --port 8765 \
  --db "$PWD/data/resource-monitor.db" \
  --dry-run
```

Remove `--dry-run` and add either `--generate-token` or `--token-file PATH`, plus
`--load`, when the rendered plist looks correct:

```bash
./deploy/install-service.sh \
  --python "$PWD/.venv/bin/python" \
  --generate-token \
  --host 127.0.0.1 \
  --port 8765 \
  --db "$PWD/data/resource-monitor.db" \
  --load
```

The installer stores the token at
`~/Library/Application Support/Resource Monitor/api-token` with mode `0600` and
passes only that path to launchd as `RM_API_TOKEN_FILE`. The committed plist never
contains the token, and the token is not visible in `ps` or service logs. To rotate
it, replace the file with a new mode-0600 token and rerun the installer with
`--token-file PATH --load` (or use `--generate-token --load`).

## Run a generic node heartbeat

Any machine or cloud worker can keep an agent lease live without depending on
Orca internals:

```bash
python scripts/node-heartbeat.py \
  --base-url http://127.0.0.1:8765 \
  --agent-id example-node \
  --lease-id interactive-session \
  --ttl-seconds 90 \
  --interval-seconds 30 \
  --metadata role=client \
  --metadata host=example-node
```

The heartbeat loop is generic by design. A future Orca adapter can call the
same API, but the core contract does not require Orca.

## Read agent status

Consumers can read canonical lease state without direct SQLite access:

```bash
curl -H "Authorization: Bearer $RM_API_TOKEN" \
  http://127.0.0.1:8765/v1/agents/example-node/status

curl -H "Authorization: Bearer $RM_API_TOKEN" \
  http://127.0.0.1:8765/v1/agents?limit=100
```

The status payload includes availability, lease identity, last heartbeat
timing, opaque metadata, and any durable checkpoint stored for that agent.

## License

MIT — see [`LICENSE`](LICENSE).
