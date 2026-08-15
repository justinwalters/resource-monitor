# MCP SDK dependency decision

## Decision

Use the official Model Context Protocol Python SDK, package mcp, on its stable v2
line with a bounded requirement of mcp>=2,<3 when the MCP server is implemented in
P5-B. The server should use the v2 public API and Streamable HTTP transport for the
network boundary; stdio remains useful for local development and Inspector checks.

Raise the RM Python floor from >=3.9 to >=3.10 before declaring the dependency
installable, and make the Python-version and installation changes together with the
server skeleton so they can be tested as one unit.

## Compatibility finding

The RM package previously declared requires-python >=3.9. The official SDK's current
v2 package declares Python >=3.10, so it cannot be installed into an older environment
without first providing a supported Python 3.10+ runtime. The SDK v1 line also
declares Python >=3.10 and is maintenance-only, so retaining v1 would not preserve the
older runtime floor and would add a migration later.

## Rationale

- The SDK is maintained by the MCP project and is the canonical Python implementation.
- v2.0.0 is the current stable release and supports the current MCP specification as
  well as earlier revisions.
- The bounded major-version requirement prevents a future v3 resolution from silently
  changing the server API.
- Delaying the dependency declaration until P5-B keeps this investigation non-invasive
  and avoids making the existing Python 3.9 service uninstallable before a replacement
  runtime is provisioned.

## Sources checked

- Official SDK repository: https://github.com/modelcontextprotocol/python-sdk
- Official v2 release: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0
- Official v1 branch metadata: https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/v1.x/pyproject.toml

