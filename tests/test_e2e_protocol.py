"""End-to-end over an in-memory MCP session: real protocol, no sockets.

Drives the actual `mcp_server` handlers through a real ClientSession and
JSON-RPC serialization, so decorator wiring, input schemas, and result
coercion are exercised — none of which `test_server_catalog.py` covers,
since it calls `call_tool()` as a plain function.

Transport-agnostic by construction: if the server ever moves off SSE, this
file keeps working while `test_e2e_transport.py` needs rewriting.
"""

import json
from contextlib import asynccontextmanager

from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from cli_mcp import server as srv


@asynccontextmanager
async def mcp_session():
    """Connect a client session to the real server object.

    Deliberately not a pytest fixture: pytest-asyncio tears async generator
    fixtures down in a different task than it enters them, which trips
    anyio's cancel-scope check. Entering the context inside each test keeps
    enter and exit in the same task.
    """
    async with create_connected_server_and_client_session(
        srv.mcp_server, raise_exceptions=True
    ) as session:
        await session.initialize()
        yield session


def payload(result) -> dict:
    """Unwrap the JSON envelope from a call_tool result."""
    assert isinstance(result.content[0], TextContent)
    return json.loads(result.content[0].text)


async def test_initialize_reports_server_name(e2e_config):
    async with create_connected_server_and_client_session(srv.mcp_server) as session:
        info = await session.initialize()
        assert info.serverInfo.name == "cli-mcp-server"


async def test_list_tools_advertises_catalog(e2e_config):
    async with mcp_session() as session:
        tools = (await session.list_tools()).tools
        assert sorted(t.name for t in tools) == ["fake", "missing", "upper"]
        fake = next(t for t in tools if t.name == "fake")
        assert fake.description == "Fake tool for e2e tests."
        assert fake.inputSchema["required"] == ["command"]
        assert fake.inputSchema["properties"]["command"]["type"] == "string"


async def test_call_tool_round_trip(e2e_config):
    async with mcp_session() as session:
        body = payload(await session.call_tool("fake", {"command": "ok hello"}))
        assert body["status"] == "success"
        assert body["node"] == "e2e-node"
        assert body["tool"] == "fake"
        assert body["result"]["argv"] == ["sub", "ok", "hello"]


async def test_denial_is_in_band_not_a_protocol_error(e2e_config):
    """A policy denial must reach the model as data it can read, not as an
    MCP error. Clients surface the two very differently."""
    async with mcp_session() as session:
        result = await session.call_tool("fake", {"command": "destroy all"})
        assert result.isError is False
        body = payload(result)
        assert body["status"] == "denied"
        assert "deny rule" in body["error"]


async def test_shell_metacharacter_rejected(e2e_config):
    async with mcp_session() as session:
        body = payload(await session.call_tool("fake", {"command": "ok; rm -rf /"}))
        assert body["status"] == "denied"


async def test_pipeline_through_protocol(e2e_config):
    async with mcp_session() as session:
        body = payload(await session.call_tool("fake", {"command": "ok hi | upper -"}))
        assert body["status"] == "success"
        assert "ARGV" in body["result"]


async def test_argument_less_pipe_stage(e2e_config):
    """Regression: `| upper` with no args was denied, because resolve_pipeline
    strips the tool name leaving "" and check_command used to reject the empty
    string outright. Affected | wc, | sort, | uniq, | tac, | nl."""
    async with mcp_session() as session:
        body = payload(await session.call_tool("fake", {"command": "ok hi | upper"}))
        assert body["status"] == "success"
        assert "ARGV" in body["result"]


async def test_unhealthy_tool_is_listed_but_errors_on_call(e2e_config):
    async with mcp_session() as session:
        names = [t.name for t in (await session.list_tools()).tools]
        assert "missing" in names
        body = payload(await session.call_tool("missing", {"command": "anything"}))
        assert body["status"] == "error"
        assert "unavailable" in body["error"].lower()
        assert "not-installed" in body["error"]
