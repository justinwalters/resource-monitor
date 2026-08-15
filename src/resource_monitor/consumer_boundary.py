"""Downstream consumer boundary for the RM MCP server.

RM must not become a single point of failure. If RM is unreachable,
unresponsive, or refuses the connection, a consumer calling through this
boundary must receive the exact conservative fallback below instead of a
raised exception:

    {"resource_intelligence": "unavailable", "fallback": "conservative"}

This module is a client, not a server change. It does not add, alter, or
otherwise touch any MCP tool; it only wraps the existing authenticated
Streamable HTTP path so a downstream consumer can call an RM tool and always
get a value back.
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

FALLBACK_RESPONSE: dict[str, str] = {
    "resource_intelligence": "unavailable",
    "fallback": "conservative",
}


def _validate(url: str, bearer_token: str, tool_name: str, connect_timeout_seconds: float) -> None:
    if not url:
        raise ValueError("url is required")
    if not bearer_token:
        raise ValueError("bearer_token is required")
    if not tool_name:
        raise ValueError("tool_name is required")
    if connect_timeout_seconds <= 0:
        raise ValueError("connect_timeout_seconds must be positive")


def _validate_stdio(command: str, bearer_token: str, tool_name: str, connect_timeout_seconds: float) -> None:
    if not command:
        raise ValueError("command is required")
    if not bearer_token:
        raise ValueError("bearer_token is required")
    if not tool_name:
        raise ValueError("tool_name is required")
    if connect_timeout_seconds <= 0:
        raise ValueError("connect_timeout_seconds must be positive")


async def call_resource_intelligence(
    url: str,
    bearer_token: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    connect_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Call one read-only RM MCP tool, degrading to a conservative fallback.

    Any failure to reach or complete the round trip with RM — connection
    refused, timeout, transport error, or a tool-reported error — produces
    exactly FALLBACK_RESPONSE. Input validation errors (a malformed call, not
    an RM outage) still raise, since those are the consumer's own bug, not
    RM's absence.
    """

    _validate(url, bearer_token, tool_name, connect_timeout_seconds)

    try:
        with anyio.fail_after(connect_timeout_seconds):
            headers = {"Authorization": f"Bearer {bearer_token}"}
            async with create_mcp_http_client(headers=headers) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments or {})
    except Exception:
        return dict(FALLBACK_RESPONSE)

    if result.is_error:
        return dict(FALLBACK_RESPONSE)

    return {
        "resource_intelligence": "available",
        "data": result.structured_content if result.structured_content is not None else {},
    }


async def call_resource_intelligence_stdio(
    command: str,
    bearer_token: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    command_args: list[str] | None = None,
    cwd: str | None = None,
    connect_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Call one RM tool through the native launcher with conservative fallback."""

    _validate_stdio(command, bearer_token, tool_name, connect_timeout_seconds)

    try:
        with anyio.fail_after(connect_timeout_seconds):
            server = StdioServerParameters(
                command=command,
                args=command_args or [],
                cwd=cwd,
                env={
                    "RM_API_TOKEN": bearer_token,
                    "RM_MCP_CLIENT_TOKEN": bearer_token,
                },
            )
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments or {})
    except Exception:
        return dict(FALLBACK_RESPONSE)

    if result.is_error:
        return dict(FALLBACK_RESPONSE)

    return {
        "resource_intelligence": "available",
        "data": result.structured_content if result.structured_content is not None else {},
    }
