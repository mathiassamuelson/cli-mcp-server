"""Forwarded identity over the real SSE transport.

The unit rules live in test_identity.py. What can only be checked here is the
wiring: that the identity captured on the GET reaches `call_tool`'s envelope
through a contextvar the SDK's task group has to propagate, and that the POST
route is authenticated and bound to the stream it targets.

The refusals are asserted as refusals -- a specific status on a specific route
-- rather than as "the client raised something", because every one of them
fails open if it is simply not wired up.
"""

import json
import logging
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client
from starlette.testclient import TestClient

from cli_mcp import server as srv
from cli_mcp.identity import IdentityMisconfigured

pytestmark = pytest.mark.e2e

SECRET = "proxy-shared-secret"
GOOD = {"X-Auth-Request-Email": "curator@example.com", "X-Trail-Proxy": SECRET}


@pytest.fixture
def identity_config(e2e_config, monkeypatch):
    """e2e_config, with identity switched on and the secret in the env."""
    monkeypatch.setenv("CLI_MCP_TEST_PROXY_SECRET", SECRET)
    e2e_config["server"]["identity"] = {
        "header": "X-Auth-Request-Email",
        "require": True,
        "proxy_header": "X-Trail-Proxy",
        "proxy_secret_env": "CLI_MCP_TEST_PROXY_SECRET",
        "bind_to_session": True,
    }
    monkeypatch.setattr(srv, "IDENTITY_CONFIG", None)
    monkeypatch.setattr(srv, "IDENTITY_CONFIG_LOADED", False)
    srv.SESSION_IDENTITY.clear()
    yield e2e_config
    monkeypatch.setattr(srv, "IDENTITY_CONFIG_LOADED", False)
    srv.SESSION_IDENTITY.clear()


@asynccontextmanager
async def session_with(base, headers, on_session_created=None):
    """Wrapping the client session in a yield fixture trips anyio's
    cancel-scope check on teardown -- see CLAUDE.md."""
    async with sse_client(
        f"{base}/mcp", headers=headers, on_session_created=on_session_created
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# --- the identity reaches the envelope and the record ---------------------


async def test_identity_appears_in_tool_envelope(identity_config, live_server):
    """The contextvar set on the GET must survive into call_tool, which the
    SDK dispatches from its own task group."""
    async with session_with(live_server, GOOD) as session:
        result = await session.call_tool("fake", {"command": "ok hi"})
        body = json.loads(result.content[0].text)

    assert body["status"] == "success"
    assert body["identity"] == "curator@example.com"


async def test_identity_appears_on_denials_too(identity_config, live_server):
    """A denied call is exactly the one worth attributing."""
    async with session_with(live_server, GOOD) as session:
        result = await session.call_tool("fake", {"command": "destroy all"})
        body = json.loads(result.content[0].text)

    assert body["status"] == "denied"
    assert body["identity"] == "curator@example.com"


async def test_identity_is_null_not_absent_when_unconfigured(e2e_config, live_server):
    """With no identity block the key is still present and null. A consumer
    can distinguish "not established here" from a name; it can never read a
    placeholder string as a person."""
    async with session_with(live_server, {}) as session:
        result = await session.call_tool("fake", {"command": "ok hi"})
        body = json.loads(result.content[0].text)

    assert "identity" in body
    assert body["identity"] is None


async def test_logged_line_carries_identity(identity_config, live_server, caplog):
    with caplog.at_level("INFO", logger="cli_mcp.server"):
        async with session_with(live_server, GOOD) as session:
            await session.call_tool("fake", {"command": "ok hi"})

    lines = [r.getMessage() for r in caplog.records if "tool_call" in r.getMessage()]
    assert lines, "no tool_call line was logged"
    assert "identity=curator@example.com" in lines[0]


def test_audit_log_has_somewhere_to_go():
    """The record must survive uvicorn's logging config, not merely be emitted.

    caplog attaches a handler of its own, so the test above passes even when
    the record reaches nothing -- which is exactly what happened. Under
    `bin/server.sh`, uvicorn configures its own `uvicorn.*` loggers and leaves
    root bare, so every one of these lines was dropped by Python's last-resort
    handler while the suite stayed green. Verified by hand against a live
    server: zero `tool_call` lines in the output before this, present after.

    This asserts the two properties that were false then and are true now, and
    it is deliberately not a caplog test, because caplog supplies both of them
    itself and would pass either way.
    """
    audit = logging.getLogger("cli_mcp")
    assert audit.getEffectiveLevel() <= logging.INFO, (
        "cli_mcp logs at INFO; an effective level above it discards every "
        "tool-call record"
    )

    reachable = list(audit.handlers)
    if audit.propagate:
        reachable += logging.getLogger().handlers
    assert reachable, (
        "no handler on cli_mcp or root: the tool-call record is emitted into "
        "nothing"
    )


def test_misconfigured_identity_fails_startup(e2e_config, monkeypatch):
    """A bad identity block must stop the server, not wait for a caller.

    Config loads lazily everywhere else here, which for this block would mean
    a deployment that starts clean, reports healthy, and only discovers the
    problem on the first call -- long after anyone was watching. Asserted
    through the real ASGI lifespan, which is what uvicorn runs; an earlier
    version validated nothing at startup and served happily.
    """
    monkeypatch.delenv("CLI_MCP_MISSING_SECRET", raising=False)
    e2e_config["server"]["identity"] = {
        "header": "X-Auth-Request-Email",
        "proxy_header": "X-Trail-Proxy",
        "proxy_secret_env": "CLI_MCP_MISSING_SECRET",
    }
    monkeypatch.setattr(srv, "IDENTITY_CONFIG_LOADED", False)

    with pytest.raises(IdentityMisconfigured):
        with TestClient(srv.app):
            pass

    monkeypatch.setattr(srv, "IDENTITY_CONFIG_LOADED", False)


def test_good_identity_config_starts(identity_config):
    """The guard above must not be passing because startup always fails."""
    with TestClient(srv.app) as client:
        assert client.get("/health").status_code == 200


# --- refusals on the SSE GET ---------------------------------------------


def _get_sse(base, headers):
    """GET the SSE route as a real client does. Starlette's Mount("/mcp")
    answers a bare /mcp with a 307 to /mcp/, so a request that does not follow
    redirects never reaches the handler and would pass these tests against a
    server with no identity check at all."""
    return httpx.get(
        f"{base}/mcp", headers=headers, timeout=5, follow_redirects=True
    )


async def test_missing_identity_refused_on_connect(identity_config, live_server):
    r = _get_sse(live_server, {"X-Trail-Proxy": SECRET})
    assert r.status_code == 403


async def test_missing_proxy_secret_refused_on_connect(identity_config, live_server):
    r = _get_sse(live_server, {"X-Auth-Request-Email": "curator@example.com"})
    assert r.status_code == 403


async def test_duplicate_identity_header_refused_on_connect(identity_config, live_server):
    r = _get_sse(live_server, [
        ("X-Trail-Proxy", SECRET),
        ("X-Auth-Request-Email", "curator@example.com"),
        ("X-Auth-Request-Email", "attacker@example.com"),
    ])
    assert r.status_code == 403


# --- refusals on the POST route ------------------------------------------


def _post(base, session_id, headers):
    return httpx.post(
        f"{base}/mcp/messages/?session_id={session_id}",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 99, "method": "tools/list"},
        timeout=5,
    )


async def test_post_route_requires_identity(identity_config, live_server):
    """The route that gets forgotten. Authenticate only the GET and the
    session id becomes a bearer token with no expiry."""
    seen = []
    async with session_with(live_server, GOOD, on_session_created=seen.append):
        assert seen, "client never reported a session id"
        assert _post(live_server, seen[0], {"X-Trail-Proxy": SECRET}).status_code == 403


async def test_post_route_requires_proxy_secret(identity_config, live_server):
    seen = []
    async with session_with(live_server, GOOD, on_session_created=seen.append):
        r = _post(live_server, seen[0], {"X-Auth-Request-Email": "curator@example.com"})
        assert r.status_code == 403


async def test_post_bound_to_the_identity_that_opened_the_stream(
    identity_config, live_server
):
    """A second authenticated user may not drive the first one's stream.

    Without this the call would run, and be recorded against the identity that
    opened the stream -- naming the wrong person with nothing to show for it.
    """
    seen = []
    async with session_with(live_server, GOOD, on_session_created=seen.append):
        r = _post(
            live_server,
            seen[0],
            {"X-Auth-Request-Email": "someone-else@example.com", "X-Trail-Proxy": SECRET},
        )
        assert r.status_code == 404
        assert "Could not find session" in r.text


async def test_post_with_matching_identity_accepted(identity_config, live_server):
    """The binding must not reject the legitimate case -- otherwise the tests
    above pass because nothing works at all."""
    seen = []
    async with session_with(live_server, GOOD, on_session_created=seen.append):
        assert _post(live_server, seen[0], GOOD).status_code == 202


async def test_post_to_unknown_session_refused(identity_config, live_server):
    async with session_with(live_server, GOOD):
        r = _post(live_server, "0" * 32, GOOD)
        assert r.status_code == 404


async def test_bind_to_session_can_be_disabled(identity_config, live_server, monkeypatch):
    """The toggle is real: with it off, a different authenticated identity may
    post to the stream. Proves the 404 above comes from the binding rather
    than from something incidental."""
    identity_config["server"]["identity"]["bind_to_session"] = False
    monkeypatch.setattr(srv, "IDENTITY_CONFIG_LOADED", False)

    seen = []
    async with session_with(live_server, GOOD, on_session_created=seen.append):
        r = _post(
            live_server,
            seen[0],
            {"X-Auth-Request-Email": "someone-else@example.com", "X-Trail-Proxy": SECRET},
        )
        assert r.status_code == 202


# --- the health route stays reachable -------------------------------------


async def test_health_does_not_require_identity(identity_config, live_server):
    """Health is a liveness check for whatever supervises the process, which
    has no identity to present. It is reachable only to whoever can reach the
    socket, and the proxy is not expected to expose it."""
    r = httpx.get(f"{live_server}/health", timeout=5)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
