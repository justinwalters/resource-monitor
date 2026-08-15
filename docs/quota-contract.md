# Provider quota contract

RM receives provider quota observations from a collector and serves them to
downstream consumers such as Orca. A consumer only reads this contract; it
does not authenticate to providers or run a second quota poller.

## Read endpoints

- `GET /v1/providers/{provider}/quota`
- `GET /v1/quotas`
- Optional query: `max_age_seconds` (default `300`)

Every response is bearer-authenticated. A provider response has `windows[]`;
each window may contain `used`, `total`, `remaining`, `used_percent`,
`remaining_percent`, `unit`, `authority`, `confidence`, `health`,
`freshness_seconds`, `reset_at`, `reset_at_utc`, `seconds_until_reset`,
`reset_status`, `endpoint`, `source_id`, and the optional
first-party provenance fields `source_scope`, `feature`, `source_updated_at`,
and `window_minutes`. These are an explicit scalar allowlist; arbitrary input
keys, nested raw payloads, credentials, and tokens are never projected.

When a provider supplies an absolute reset timestamp, RM preserves it as
`reset_at` and normalizes it to `reset_at_utc`. Relative rate-limit values such
as `1m` are resolved from the observation timestamp. `seconds_until_reset` is
computed when the read model is served, and `reset_status` is `scheduled`,
`due`, or `unknown`. Provider responses also include `reset_context` with the
nearest reset, its countdown, and known/unknown window counts. A window's
nominal duration is never treated as its reset time; unknown reset data stays
unknown so a downstream consumer can make a conservative decision.

`used_percent` is accepted directly only when the provider reports a finite
value from 0 through 100. Otherwise RM calculates it from a reported limit and
used/remaining value. Local token totals without a provider limit produce no
percentage. Contradictory or out-of-range values are rejected and projected as
`health: unknown`, never silently corrected.

Fresh observations are `health: online` and stale observations are returned as
`health: stale`, with `status: stale` and `freshness_seconds`; unknown
providers return `windows: []` and `status: unknown`. This keeps the raw
observation available while preventing a stale percentage from looking live.

## Supported source shapes

The normalization helpers accept:

- Orca's `result.rateLimits.<provider>.<window>` shape, including direct
  `usedPercent` values and provider status/error fields.
- HTTP `x-ratelimit-*` limit/remaining/reset headers used by common provider
  REST APIs.

Provider credentials and network polling remain outside public RM. A collector
must submit a canonical `quota` snapshot with a non-secret `source_id` and
endpoint identifier.
