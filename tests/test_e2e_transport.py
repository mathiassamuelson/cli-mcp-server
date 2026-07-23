"""End-to-end over the real SSE transport against uvicorn.

This is the `bin/server.sh` path: `uvicorn cli_mcp.server:app`. Covers ASGI
routing, the SSE endpoint handshake, and /health — none of which the
in-process suites touch.

Marked `e2e` because each test binds a socket and starts a server thread;
`pytest -m "not e2e"` skips them.
"""

import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client

pytestmark = pytest.mark.e2e


async def test_health_endpoint(live_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{live_server}/health", timeout=5)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["node"] == "e2e-node"

    by_name = {t["name"]: t for t in body["tools"]}
    assert by_name["fake"]["healthy"] is True
    assert by_name["missing"]["healthy"] is False
    assert "not-installed" in by_name["missing"]["reason"]


async def test_sse_handshake_and_tool_call(live_server):
    """Pins the SSE mount wiring.

    Three things must agree for this to pass, and nothing else in the suite
    checks any of them:
      * SseServerTransport("/messages/") in server.py
      * Mount("/mcp", handle_sse), which sets ASGI root_path="/mcp" — the SDK
        advertises root_path + endpoint, i.e. "/mcp/messages/", to the client
      * Mount("/mcp/messages", handle_messages), which must serve that path
        AND must precede the "/mcp" mount, or the broader mount swallows it
    """
    async with sse_client(f"{live_server}/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            assert "fake" in [t.name for t in tools]

            result = await session.call_tool("fake", {"command": "ok hi"})
            body = json.loads(result.content[0].text)
            assert body["status"] == "success"
            assert body["result"]["argv"] == ["sub", "ok", "hi"]


async def test_denial_over_transport(live_server):
    async with sse_client(f"{live_server}/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("fake", {"command": "destroy all"})
            assert json.loads(result.content[0].text)["status"] == "denied"
