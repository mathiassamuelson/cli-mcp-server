"""MCP server exposing catalog-driven CLI tools via SSE transport.

Loads config from CLI_MCP_CONFIG env var, ~/.config/cli-mcp-server/config.yaml,
or /etc/cli-mcp-server/config.yaml. Tools are declared as YAML entries in the
catalog directory (config["catalog"]["path"]); the server registers one MCP
Tool per entry and dispatches call_tool() through the registry.
"""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import UUID

import yaml
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse, PlainTextResponse

from .catalog import load_catalog, ToolRegistry
from .filter import check_command, check_paths
from .cli_executor import run_tool, run_pipeline
from .identity import IdentityConfig, IdentityRefused, resolve_identity
from .pipeline import (
    parse_pipeline,
    resolve_pipeline,
    PipelineGrammarError,
    PipelineResolutionError,
)


DEFAULT_SEARCH_PATHS = [
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
    "/usr/local/nom/sbin",
]


def load_config() -> dict:
    config_path = os.environ.get("CLI_MCP_CONFIG")
    if not config_path:
        user_path = Path.home() / ".config" / "cli-mcp-server" / "config.yaml"
        config_path = str(user_path) if user_path.exists() else "/etc/cli-mcp-server/config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


logger = logging.getLogger("cli_mcp.server")


def _configure_audit_logging() -> None:
    """Give the tool-call record somewhere to go.

    uvicorn's default logging config attaches handlers to its own `uvicorn.*`
    loggers and leaves the root logger bare. A plain `logger.info(...)` from
    this module therefore propagates to a root with no handlers, where Python's
    last-resort handler drops anything below WARNING -- so the line vanishes,
    under exactly the invocation the project ships (`bin/server.sh`).

    That failure is invisible from the inside and invisible to a test using
    pytest's caplog, which installs its own handler and so proves only that the
    call was made. The line is the record of who asked for what; a deployment
    that believes it is keeping one and is not is worse off than one that knows
    it is not. So the handler is installed here rather than left to the host.

    A deployment that configures its own handlers -- on `cli_mcp` or on root --
    keeps them; this only fills a vacuum.
    """
    audit = logging.getLogger("cli_mcp")
    if audit.level == logging.NOTSET:
        audit.setLevel(logging.INFO)
    if not audit.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        audit.addHandler(handler)


_configure_audit_logging()

CONFIG: dict | None = None
REGISTRY: ToolRegistry | None = None
IDENTITY_CONFIG: IdentityConfig | None = None
IDENTITY_CONFIG_LOADED = False
mcp_server = Server("cli-mcp-server")
sse_transport = SseServerTransport("/messages/")

# The identity of the caller who opened the current SSE stream. Set in
# handle_sse before mcp_server.run(); call_tool reads it. anyio copies the
# context when it starts a task, so handlers dispatched by the server's task
# group inherit the value set at the run() call site.
IDENTITY: ContextVar[str | None] = ContextVar("cli_mcp_identity", default=None)

# Set for the duration of the connect, so _SessionRegistry can pair the session
# id the transport generates with the identity that opened it.
_CONNECTING_IDENTITY: ContextVar[str | None] = ContextVar(
    "cli_mcp_connecting_identity", default=None
)

# session id -> identity that opened that stream.
SESSION_IDENTITY: dict[UUID, str | None] = {}


class _SessionRegistry(dict):
    """The transport's session table, wrapped so we learn each session id.

    The SDK generates the session id inside `connect_sse` and does not hand it
    back, so there is no supported hook for "this stream belongs to that
    caller". It does register the stream in `_read_stream_writers[session_id]`,
    in the connecting task -- so subclassing the dict gets us the id at the
    moment it exists, with no polling and no race against a concurrent connect.

    This reaches into a private attribute, which is a liability across SDK
    upgrades, so it is arranged to fail loudly rather than open: the attribute
    is asserted to exist at import, and a session with no recorded identity is
    refused by handle_messages rather than allowed. If a future SDK stops using
    this dict, every POST is rejected and the deployment is visibly broken --
    the opposite of an identity check that silently stops running.
    """

    def __init__(self, identities: dict):
        super().__init__()
        self._identities = identities

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._identities[key] = _CONNECTING_IDENTITY.get()

    def __delitem__(self, key):
        self._identities.pop(key, None)
        super().__delitem__(key)

    def pop(self, key, *default):
        self._identities.pop(key, None)
        return super().pop(key, *default)


if not isinstance(getattr(sse_transport, "_read_stream_writers", None), dict):
    raise RuntimeError(
        "SseServerTransport._read_stream_writers is missing or is not a dict; "
        "cli_mcp.server binds forwarded identities to sessions through it. "
        "Refusing to import rather than run with session binding silently off."
    )
sse_transport._read_stream_writers = _SessionRegistry(SESSION_IDENTITY)


def get_config() -> dict:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()
    return CONFIG


def get_identity_config() -> IdentityConfig | None:
    """The `server.identity` block, or None when the mechanism is off.

    Cached separately from CONFIG because None is a meaningful result here and
    would otherwise be indistinguishable from "not loaded yet". Errors from
    from_config are deliberately not caught: a misconfigured identity block
    must stop the server, not degrade it.
    """
    global IDENTITY_CONFIG, IDENTITY_CONFIG_LOADED
    if not IDENTITY_CONFIG_LOADED:
        IDENTITY_CONFIG = IdentityConfig.from_config(get_config().get("server"))
        IDENTITY_CONFIG_LOADED = True
    return IDENTITY_CONFIG


def get_registry() -> ToolRegistry:
    global REGISTRY
    if REGISTRY is None:
        cat_cfg = get_config().get("catalog") or {}
        REGISTRY = load_catalog(
            cat_cfg.get("path", "/etc/cli-mcp-server/catalog/"),
            defaults=cat_cfg.get("defaults") or {},
            search_paths=cat_cfg.get("search_paths") or DEFAULT_SEARCH_PATHS,
        )
    return REGISTRY


def _envelope(node: str, name: str, command: str | None, **rest) -> list[dict]:
    payload = {"node": node, "tool": name}
    if command is not None:
        payload["command"] = command
    # Always present, and null rather than absent or "unknown" when identity is
    # not configured. A reader can tell "this deployment does not establish
    # identity" from "this call was made by someone"; it can never read a
    # placeholder as a person.
    payload["identity"] = IDENTITY.get()
    payload.update(rest)
    _log_call(payload)
    return [{"type": "text", "text": json.dumps(payload)}]


def _log_call(payload: dict) -> None:
    """One line per tool call, carrying who it was made for.

    The envelope goes to the caller; this is the copy that stays on the node,
    and it is the reason identity is recorded at all. Note that `command`
    records what was asked, which for many catalogs is itself sensitive -- it
    is the substance of the question. Deployments should make a deliberate
    retention choice about this log rather than inheriting a default.
    """
    logger.info(
        "tool_call node=%s identity=%s tool=%s status=%s command=%r%s",
        payload.get("node"),
        payload.get("identity"),
        payload.get("tool"),
        payload.get("status"),
        payload.get("command"),
        f" error={payload['error']!r}" if payload.get("error") else "",
    )


@mcp_server.list_tools()
async def list_tools():
    registry = get_registry()
    return [
        Tool(
            name=entry.name,
            description=entry.description,
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Arguments to pass to the tool. May include pipes to other catalog "
                            "tools flagged as pipe stages (e.g., `aux | grep nginx | head -5`)."
                        ),
                    }
                },
                "required": ["command"],
            },
        )
        for entry in registry
    ]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    config = get_config()
    node_name = config["server"]["node_name"]
    registry = get_registry()

    entry = registry.get(name)
    if entry is None:
        return _envelope(node_name, name, None, status="error", error=f"Unknown tool: {name}")

    command = arguments.get("command", "")

    if not entry.healthy:
        return _envelope(
            node_name, name, command,
            status="error",
            error=f"Tool unavailable on this node: {entry.unhealthy_reason}",
        )

    try:
        segments = parse_pipeline(command)
    except PipelineGrammarError as e:
        return _envelope(node_name, name, command, status="denied", error=str(e))

    try:
        stages = resolve_pipeline(entry, segments, registry)
    except PipelineResolutionError as e:
        return _envelope(node_name, name, command, status="denied", error=str(e))

    for i, (seg_entry, seg_args) in enumerate(stages):
        if not seg_entry.healthy:
            return _envelope(
                node_name, name, command,
                status="error",
                error=f"segment {i} ({seg_entry.name}): {seg_entry.unhealthy_reason}",
            )
        allowed, reason = check_command(seg_args, seg_entry.rules)
        if not allowed:
            return _envelope(
                node_name, name, command,
                status="denied",
                error=f"segment {i} ({seg_entry.name}): {reason}",
            )
        ok, preason = check_paths(seg_args, seg_entry.path_deny)
        if not ok:
            return _envelope(
                node_name, name, command,
                status="denied",
                error=f"segment {i} ({seg_entry.name}): {preason}",
            )

    if len(stages) == 1:
        result = await run_tool(entry, command)
    else:
        result = await run_pipeline(stages)

    result["node"] = node_name
    result["tool"] = name
    result["command"] = command
    result["identity"] = IDENTITY.get()
    _log_call(result)
    return [{"type": "text", "text": json.dumps(result)}]


async def handle_sse(scope, receive, send):
    try:
        identity = resolve_identity(Headers(scope=scope), get_identity_config())
    except IdentityRefused as refusal:
        logger.warning("sse connect refused: %s", refusal.reason)
        await PlainTextResponse(refusal.reason, status_code=refusal.status)(
            scope, receive, send
        )
        return

    connecting = _CONNECTING_IDENTITY.set(identity)
    token = IDENTITY.set(identity)
    try:
        async with sse_transport.connect_sse(scope, receive, send) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )
    finally:
        IDENTITY.reset(token)
        _CONNECTING_IDENTITY.reset(connecting)


async def handle_messages(scope, receive, send):
    """Authenticate the POST route too, and bind it to its stream.

    Authenticating only the SSE GET would make the session id a bearer token
    with no expiry: anyone who can reach the socket and has seen an id -- in a
    log, in a proxy trace -- could drive somebody else's stream. Worse, the
    call would be *recorded against the identity that opened the stream*, so
    the record would name the wrong person with no sign anything was amiss.
    """
    config = get_identity_config()
    try:
        identity = resolve_identity(Headers(scope=scope), config)
    except IdentityRefused as refusal:
        logger.warning("message refused: %s", refusal.reason)
        await PlainTextResponse(refusal.reason, status_code=refusal.status)(
            scope, receive, send
        )
        return

    if config is not None and config.bind_to_session:
        session_id = _session_id_of(scope)
        # `not in` rather than `.get() != identity`: an unrecorded session must
        # be refused, not compared against None and allowed when identity is
        # also None.
        if session_id is None or session_id not in SESSION_IDENTITY:
            logger.warning("message refused: unknown or unparseable session id")
            await PlainTextResponse("Could not find session", status_code=404)(
                scope, receive, send
            )
            return
        if SESSION_IDENTITY[session_id] != identity:
            # Answer exactly as if the session did not exist, matching the SDK:
            # confirming that somebody else's session id is real is itself a
            # disclosure.
            logger.warning(
                "message refused: %s presented a session opened by another identity",
                identity,
            )
            await PlainTextResponse("Could not find session", status_code=404)(
                scope, receive, send
            )
            return

    await sse_transport.handle_post_message(scope, receive, send)


def _session_id_of(scope) -> UUID | None:
    raw = Request(scope).query_params.get("session_id")
    if not raw:
        return None
    try:
        return UUID(hex=raw)
    except ValueError:
        return None


async def health(request):
    config = get_config()
    registry = get_registry()
    tools = [
        {"name": e.name, "healthy": e.healthy, **({"reason": e.unhealthy_reason} if not e.healthy else {})}
        for e in registry
    ]
    return JSONResponse(
        {
            "status": "ok",
            "node": config["server"]["node_name"],
            "tools": tools,
        }
    )


@asynccontextmanager
async def _lifespan(_app):
    """Resolve the identity block at startup so a bad one stops the server.

    Everything else here loads lazily on first use, which for the identity
    block would mean a deployment that starts clean, reports healthy, and only
    discovers its misconfiguration when somebody makes the first call -- long
    after whoever deployed it has stopped watching. Since the failure being
    guarded against is "the identity check is not running", finding out late
    is close to not finding out.

    Raising here fails the ASGI lifespan, which uvicorn reports as
    "Application startup failed. Exiting." -- loud, and before the socket
    serves anything.
    """
    get_identity_config()
    yield


app = Starlette(
    routes=[
        Route("/health", health),
        Mount("/mcp/messages", app=handle_messages),
        Mount("/mcp", app=handle_sse),
    ],
    lifespan=_lifespan,
)
