# Graceful degradation

## Contract

> RM is not allowed to become a single point of failure. If RM is unavailable:
>
> ```json
> { "resource_intelligence": "unavailable", "fallback": "conservative" }
> ```
>
> Downstream consumers continue operating. Orca continues operating. The absence of RM
> must not equal system failure.

This is a client-side contract. RM's MCP server (`resource_monitor.mcp_server`) is
unaffected — it still requires a valid bearer token and exposes only the existing
read-only/advisory tools. This document describes the boundary a downstream consumer
calls through, so that "RM process is stopped" or "RM is unreachable" never surfaces
as an exception to the caller.

## Implementation

`resource_monitor.consumer_boundary.call_resource_intelligence(url, bearer_token,
tool_name, arguments=None, *, connect_timeout_seconds=5.0)`:

- Opens a real MCP `ClientSession` over Streamable HTTP (the same transport
  `run_streamable_http` serves), calls exactly one existing tool, and returns its
  `structured_content` wrapped as `{"resource_intelligence": "available", "data": ...}`.
- On any failure to complete that round trip within `connect_timeout_seconds` —
  connection refused, timeout, transport/protocol error, wrong bearer token, or the
  tool itself reporting an error — returns exactly
  `resource_monitor.consumer_boundary.FALLBACK_RESPONSE`:
  `{"resource_intelligence": "unavailable", "fallback": "conservative"}`. It never
  raises for an RM-side outage.
- Input validation (empty `url`/`bearer_token`/`tool_name`, non-positive timeout) still
  raises `ValueError` before any network attempt — a malformed call from the consumer's
  own code is a bug to fix, not an RM outage to mask.
- Adds no tool, no mutation path, and no change to `mcp_server.py`'s authentication or
  tool surface. It is a consumer, not a server change.

## Test coverage (`tests/test_consumer_boundary.py`)

All tests drive the real MCP client against a real RM MCP server (uvicorn +
Streamable HTTP on an ephemeral loopback port) — no mocked transport:

- RM available: a real tool call returns `resource_intelligence: available` with the
  tool's data.
- RM stopped mid-session: the same call returns the exact conservative fallback.
- RM never reachable (nothing listening on the port): fallback, within the configured
  timeout.
- Wrong bearer token against a live server: fallback, not an auth exception.
- Repeated calls during an outage: the consumer keeps getting fallback responses
  rather than failing after the first one, i.e. it "continues operating."
- Malformed calls (`url`/`bearer_token`/`tool_name` empty, non-positive timeout): raise
  `ValueError` — validation failures are distinct from RM unavailability.
