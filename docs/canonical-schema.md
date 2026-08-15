# Canonical Resource Monitor schema

The public RM schema is an open-world envelope. New providers, machines,
services, and resource types are represented as data; they do not require
changes to the core package.

## Identity rules

- `record_id`, `event_id`, and `lease_id` are opaque identifiers.
- `subject_id` is stable within the deployment and must not encode a hostname,
  vendor, model, or user unless the deployment explicitly chooses that value.
- `source_id` identifies the adapter or node that produced a record.
- `subject_type` and `event_type` are extensible strings.
- Timestamps are RFC 3339/ISO-8601 with an explicit timezone, normally UTC.
- Secrets, access tokens, cookies, and raw authorization headers are forbidden
  in canonical records.

## Core entities

| Entity | Purpose | Typical `subject_type` |
|---|---|---|
| Host | Hardware, operating-system, power, and network observations | `host` |
| Node | A telemetry-producing computer or runtime boundary | `node` |
| Provider | A service offering capacity, usage, quota, or health data | `provider` |
| Endpoint | A reachable service endpoint and its health | `endpoint` |
| Model | A discoverable model or compute resource | `model` |
| Process | A local process and lifecycle/health state | `process` |
| Usage | A request, task, turn, token, byte, or other consumption record | `usage` |
| Quota | Capacity, limit, window, and remaining-resource state | `quota` |
| Task | Correlation metadata for work consuming resources | `task` |
| Forecast | A projection derived from historical observations | `forecast` |

The same `ResourceSnapshot` envelope carries current or historical values for
these entities. `ResourceEvent` carries transitions such as health changes,
quota warnings, process failures, and recovery. Integrations may add fields to
`values`/`payload`, but must preserve the envelope and schema version.

## Resource Monitor values extension

Resource Monitor uses the existing `ResourceSnapshot` envelope. It does not
create a second flat record shape and does not promote provider-specific
attributes to top-level envelope fields. A snapshot's `subject_id`,
`subject_type`, `observed_at`, and `source_id` retain their envelope
meanings; the following keys are optional members of `values`:

| Key | Type | Meaning |
|---|---|---|
| `capacity` | number | Current measured or reported capacity in the subject's unit |
| `capacity_max` | number | Maximum capacity in the same unit as `capacity` |
| `capacity_remaining` | number | Capacity currently remaining |
| `reserve` | number | Capacity intentionally held back from ordinary use |
| `usage_rate` | number | Consumption rate; the unit is stated by `scope` or the source contract |
| `usage_total` | number | Cumulative consumption in the source-defined interval |
| `latency` | number | Measured latency in milliseconds |
| `health` | string | One of `online`, `offline`, `degraded`, or `unknown` |
| `availability` | string | One of `available`, `unavailable`, or `unknown` |
| `confidence` | number | A source-reported confidence in the range 0 through 1 |
| `authority` | string | One of `observed`, `derived`, or `authoritative`; integrations must not claim `authoritative` without an authoritative source |
| `node_id` | string | Stable identifier of the node associated with the observation |
| `scope` | string | Unit or aggregation scope for the numeric values |
| `window` | object | Optional interval metadata for rate, total, or quota values |
| `provider` | string | Opaque provider identifier when the subject is provider-backed |
| `host` | string | Opaque host identifier when the observation concerns a host |
| `endpoint` | string | Opaque endpoint identifier when the observation concerns an endpoint |

Numeric values must be finite and non-negative unless the source contract
explicitly defines a signed quantity. `confidence` is always bounded to
0..1. Numeric fields that are not meaningful for a subject are omitted rather
than emitted as zero. A source must state the unit and aggregation interval
for values whose meaning depends on either; it must not infer a unit from a
field name alone.

The optional `window` object has this shape when present:

~~~json
{
  "starts_at": "2026-08-11T21:00:00Z",
  "ends_at": "2026-08-11T22:00:00Z",
  "duration_seconds": 3600
}
~~~

Its timestamps use the same RFC 3339 rule as `observed_at`. A window may
contain `duration_seconds`, or both timestamps, but a producer must not claim
an interval it did not observe or receive from the source.

For example, a provider capacity observation is represented as:

~~~json
{
  "record_id": "opaque-record-id",
  "schema_version": 1,
  "subject_id": "opaque-provider-id",
  "subject_type": "provider",
  "observed_at": "2026-08-11T21:30:00Z",
  "source_id": "collector-id",
  "values": {
    "capacity": 100,
    "capacity_remaining": 42,
    "usage_rate": 3.5,
    "scope": "requests_per_hour",
    "health": "online",
    "availability": "available",
    "confidence": 0.98,
    "authority": "observed",
    "provider": "opaque-provider-id"
  }
}
~~~

The top-level envelope remains the compatibility boundary: an integration
must not emit `provider`, `host`, `capacity`, or similar RM keys as
top-level fields. Existing `schema_version: 1` remains valid because this
is an additive `values` extension. A future incompatible change requires a
new schema version and a migration document.

## Freshness and health

Collectors should include `observed_at` and may include these values:

```json
{
  "health": "online",
  "freshness_seconds": 4,
  "confidence": 0.98
}
```

Allowed health values are `online`, `offline`, `degraded`, and `unknown`.
Consumers must treat stale or unknown data as unsuitable for high-risk
capacity decisions.

## Compatibility

The public API must accept unknown fields and preserve forward-compatible
extensions where possible. A breaking change requires a new `schema_version`
and an explicit migration path.

## Agent status reads

Heartbeat leases are written as canonical `AgentHeartbeat` records and may be
read back through a derived status view. A status response should include:

- `agent_id`
- `availability` as `online`, `stale`, or `unknown`
- `lease_id`
- `sent_at`
- `expires_at`
- `metadata` as opaque deployment-defined key/value data
- `checkpoint` when durable recovery context exists

This read model is still integration-neutral. It describes lease freshness and
recovery state, not any specific IDE or provider runtime.
