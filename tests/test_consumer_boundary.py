"""Real MCP client/server exercise of the graceful-degradation contract.

These tests run the actual RM MCP server over Streamable HTTP (uvicorn) and
drive it with the real MCP client SDK — no mocked transport. One run keeps
the server up to prove the happy path still works through this boundary;
the other stops the server (or points at an unreachable port) to prove a
downstream consumer gets the conservative fallback instead of a hard
failure.
"""

import asyncio
import socket
import threading
from datetime import datetime, timezone

import pytest
import uvicorn

from resource_monitor.consumer_boundary import (
    FALLBACK_RESPONSE,
    call_resource_intelligence,
    call_resource_intelligence_stdio,
)
from resource_monitor.core import ResourceMonitor
from resource_monitor.mcp_server import create_mcp_server
from resource_monitor.models import ResourceSnapshot
from resource_monitor.storage import SQLiteStore


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class _RunningServer:
    """A real RM MCP server, over real Streamable HTTP, in a background thread."""

    def __init__(self, bearer_token: str) -> None:
        self.store = SQLiteStore(":memory:")
        self.monitor = ResourceMonitor(self.store)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        server = create_mcp_server(self.monitor, bearer_token)
        config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=self.port, log_level="warning")
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(target=lambda: asyncio.run(self._uvicorn.serve()), daemon=True)

    def start(self) -> None:
        self._thread.start()

        async def wait_started() -> None:
            for _ in range(500):
                if self._uvicorn.started:
                    return
                await asyncio.sleep(0.01)
            raise RuntimeError("RM MCP server did not start in time")

        asyncio.run(wait_started())

    def stop(self) -> None:
        self._uvicorn.should_exit = True
        self._thread.join(timeout=5)
        self.store.close()


@pytest.fixture
def running_server():
    server = _RunningServer("secret")
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_available_rm_returns_resource_intelligence_through_the_real_mcp_path(running_server):
    now = datetime.now(timezone.utc)
    running_server.monitor.ingest_snapshot(
        ResourceSnapshot("model-1", "model", now, {"model_id": "model-1", "health": "healthy"}, "test")
    )

    result = asyncio.run(
        call_resource_intelligence(running_server.url, "secret", "get_model_health", {"model_id": "model-1"})
    )

    assert result["resource_intelligence"] == "available"
    assert result["data"]["health"] == "healthy"


def test_stopped_rm_yields_conservative_fallback_not_a_hard_failure(running_server):
    url = running_server.url
    running_server.stop()

    result = asyncio.run(call_resource_intelligence(url, "secret", "get_model_health", {"model_id": "model-1"}))

    assert result == {"resource_intelligence": "unavailable", "fallback": "conservative"}
    assert result == FALLBACK_RESPONSE


def test_unreachable_rm_yields_conservative_fallback():
    unreachable_url = f"http://127.0.0.1:{_free_port()}/mcp"

    result = asyncio.run(
        call_resource_intelligence(
            unreachable_url, "secret", "get_daily_summary", connect_timeout_seconds=2.0
        )
    )

    assert result == FALLBACK_RESPONSE


def test_wrong_token_against_a_live_server_yields_conservative_fallback(running_server):
    result = asyncio.run(call_resource_intelligence(running_server.url, "wrong-token", "get_daily_summary"))

    assert result == FALLBACK_RESPONSE


def test_consumer_continues_operating_across_repeated_calls_during_an_outage(running_server):
    url = running_server.url
    running_server.stop()

    first = asyncio.run(call_resource_intelligence(url, "secret", "get_daily_summary"))
    second = asyncio.run(call_resource_intelligence(url, "secret", "get_recent_events"))

    assert first == FALLBACK_RESPONSE
    assert second == FALLBACK_RESPONSE


def test_stdio_missing_launcher_yields_conservative_fallback():
    result = asyncio.run(
        call_resource_intelligence_stdio("definitely-not-a-real-rm-launcher", "secret", "get_daily_summary")
    )
    assert result == FALLBACK_RESPONSE


def test_stdio_launcher_failure_yields_conservative_fallback():
    result = asyncio.run(call_resource_intelligence_stdio("/usr/bin/false", "secret", "get_daily_summary"))
    assert result == FALLBACK_RESPONSE


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "", "bearer_token": "secret", "tool_name": "get_daily_summary"},
        {"url": "http://127.0.0.1:1/mcp", "bearer_token": "", "tool_name": "get_daily_summary"},
        {"url": "http://127.0.0.1:1/mcp", "bearer_token": "secret", "tool_name": ""},
        {
            "url": "http://127.0.0.1:1/mcp",
            "bearer_token": "secret",
            "tool_name": "get_daily_summary",
            "connect_timeout_seconds": 0,
        },
    ],
)
def test_invalid_calls_are_refused_rather_than_silently_degraded(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(call_resource_intelligence(**kwargs))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"command": "", "bearer_token": "secret", "tool_name": "get_daily_summary"},
        {"command": "/usr/bin/false", "bearer_token": "", "tool_name": "get_daily_summary"},
        {"command": "/usr/bin/false", "bearer_token": "secret", "tool_name": ""},
        {
            "command": "/usr/bin/false",
            "bearer_token": "secret",
            "tool_name": "get_daily_summary",
            "connect_timeout_seconds": 0,
        },
    ],
)
def test_invalid_stdio_calls_are_refused_rather_than_silently_degraded(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(call_resource_intelligence_stdio(**kwargs))
