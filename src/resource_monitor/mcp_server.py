"""Read-only MCP server boundary for Resource Monitor.

P5-B intentionally exposes no tools. It establishes the authenticated MCP
server lifecycle so later packets can add read-only resource intelligence
without creating action capabilities.
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer


class StaticBearerTokenVerifier:
    """Verify the existing RM bearer token without exposing it to the SDK."""

    def __init__(self, expected_token: str) -> None:
        if not expected_token:
            raise ValueError("MCP bearer token is required")
        self._expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="resource-monitor",
            scopes=["resource.read"],
        )


def verify_stdio_client_token(expected_token: str, presented_token: str | None) -> bool:
    """Verify the credential supplied to a native stdio child process.

    MCP's stdio transport has no HTTP authorization headers, so the SDK's
    ``token_verifier`` cannot protect it. The launcher performs this
    process-boundary check before starting the MCP server.
    """

    if not expected_token or not presented_token:
        return False
    return hmac.compare_digest(presented_token, expected_token)


def create_mcp_server(monitor: Any, bearer_token: str) -> MCPServer[Any]:
    """Create the authenticated, currently tool-less MCP server.

    monitor is retained by the lifecycle closure as the integration boundary
    for P5-C read-only tools. No mutating capability is registered.
    """

    if monitor is None:
        raise ValueError("monitor is required")

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"monitor": monitor}

    server = MCPServer(
        name="resource-monitor",
        instructions="Read-only resource intelligence; no action tools are available.",
        token_verifier=StaticBearerTokenVerifier(bearer_token),
        auth=AuthSettings(
            issuer_url="https://resource-monitor.invalid",
            resource_server_url="https://resource-monitor.invalid/mcp",
            required_scopes=["resource.read"],
        ),
        lifespan=lifespan,
    )

    @server.tool()
    def list_available_resources(limit: int = 10000) -> dict[str, Any]:
        return {"resources": monitor.list_available_resources(limit=limit)}

    @server.tool()
    def list_resources_near_limit(threshold: float = 0.2, limit: int = 10000) -> dict[str, Any]:
        return {"resources": monitor.list_resources_near_limit(threshold=threshold, limit=limit)}

    @server.tool()
    def get_node_status(node_id: str) -> dict[str, Any]:
        return monitor.get_node_status(node_id)

    @server.tool()
    def get_provider_status(provider: str) -> dict[str, Any]:
        return monitor.get_provider_status(provider)

    @server.tool()
    def get_provider_quota(provider: str, max_age_seconds: float = 300.0) -> dict[str, Any]:
        """Return quota percentages plus provider reset timing from the mini collector.

        Each window may include ``reset_at`` (raw provider value),
        ``reset_at_utc``, ``seconds_until_reset``, and ``reset_status``. The
        response's ``reset_context`` summarizes the nearest reset and whether
        reset coverage is complete, partial, or unknown. A window duration is
        not a substitute for an actual reset time.
        """
        return monitor.get_provider_quota(provider, max_age_seconds=max_age_seconds)

    @server.tool()
    def get_provider_quotas(max_age_seconds: float = 300.0) -> dict[str, Any]:
        """Return quota windows, percentages, and reset context for every provider known to RM."""
        return {"providers": monitor.get_provider_quotas(max_age_seconds=max_age_seconds)}

    @server.tool()
    def evaluate_task_feasibility(task_id: str, requirements: dict[str, Any] | None = None) -> dict[str, Any]:
        return monitor.evaluate_task_feasibility(task_id, requirements)

    @server.tool()
    def get_work_constraints(task_id: str) -> dict[str, Any]:
        return monitor.get_work_constraints(task_id)

    @server.tool()
    def explain_task_failure(task_id: str) -> dict[str, Any]:
        return monitor.explain_task_failure(task_id)

    @server.tool()
    def get_retry_cost_estimate(task_id: str) -> dict[str, Any]:
        return monitor.get_retry_cost_estimate(task_id)

    @server.tool()
    def get_daily_summary() -> dict[str, Any]:
        return monitor.get_daily_summary()

    @server.tool()
    def get_recent_events(limit: int = 100) -> dict[str, Any]:
        return monitor.get_recent_events(limit=limit)

    @server.tool()
    def get_model_health(model_id: str) -> dict[str, Any]:
        return monitor.get_model_health(model_id)

    return server


async def run_streamable_http(server: MCPServer[Any]) -> None:
    """Run the MCP server's authenticated Streamable HTTP lifecycle."""

    await server.run_streamable_http_async()


def run_stdio(server: MCPServer[Any], *, authenticated: bool = False) -> None:
    """Run the server over stdio after the launcher auth gate has passed."""

    if not authenticated:
        raise PermissionError("native stdio must pass the launcher authentication gate")

    asyncio.run(server.run_stdio_async())
