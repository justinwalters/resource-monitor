# Resource Monitor staleness semantics

RM never presents an old observation as current. Every projected node and model record carries
`observed_at` and `freshness_seconds`; the caller supplies `max_age_seconds` (default: 300).

- `current`: the timestamp is timezone-aware, not in the future, and its age is less than or
  equal to the freshness budget.
- `stale`: the timestamp is valid but older than the budget. The projected health is explicitly
  `stale`, and aggregate health is `degraded`.
- `invalid`: the timestamp is malformed, timezone-less, or in the future. RM does not trust the
  observation; projected health becomes `unknown`, and an aggregate with no usable records is
  `unknown`.

The boundary is inclusive: an observation exactly `max_age_seconds` old is `current`; it becomes
`stale` only when its age exceeds the budget. Missing records are `unknown`, not zero, healthy, or
current. Provider quota windows use the same conservative classification and are not duplicated
into the health projection.

`/v1/health` is read-only. Its response's RM status describes the ability to read the store, while
record freshness describes the age of the observed record; these are separate signals.
