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


async def test_audit_principal_over_transport(live_server, audit_records):
    """Caller attribution survives the trip from handle_sse to call_tool.

    Nothing in the in-process suites can catch a regression here. A tool call
    does not run in the POST request's task, so the ContextVar carrying the
    principal has to be set on the SSE connection and inherited by the task
    group inside mcp_server.run(). Only a real transport exercises that.
    """
    async with sse_client(f"{live_server}/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("fake", {"command": "ok hi"})

    decision = audit_records("decision")[0]
    principal = decision["principal"]

    assert principal["transport"] == "sse"
    assert principal["peer"] == "127.0.0.1"
    assert principal["connection"]
    # Present and explicitly false: connections are unauthenticated, and an
    # absent field must never be readable as a trusted caller.
    assert principal["authenticated"] is False


async def test_audit_attributes_calls_to_their_own_connection(live_server, audit_records):
    """Two clients must not be conflated. Connection-scoped attribution means
    each call carries the id of the connection it arrived on."""
    async with sse_client(f"{live_server}/mcp") as (read_a, write_a):
        async with ClientSession(read_a, write_a) as session_a:
            await session_a.initialize()
            await session_a.call_tool("fake", {"command": "ok from-a"})

    async with sse_client(f"{live_server}/mcp") as (read_b, write_b):
        async with ClientSession(read_b, write_b) as session_b:
            await session_b.initialize()
            await session_b.call_tool("fake", {"command": "ok from-b"})

    decisions = audit_records("decision")
    by_command = {d["command"]: d for d in decisions}

    conn_a = by_command["ok from-a"]["principal"]["connection"]
    conn_b = by_command["ok from-b"]["principal"]["connection"]
    assert conn_a != conn_b
