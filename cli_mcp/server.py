"""MCP server exposing catalog-driven CLI tools via SSE transport.

Loads config from CLI_MCP_CONFIG env var, ~/.config/cli-mcp-server/config.yaml,
or /etc/cli-mcp-server/config.yaml. Tools are declared as YAML entries in the
catalog directory (config["catalog"]["path"]); the server registers one MCP
Tool per entry and dispatches call_tool() through the registry.
"""

import json
import os
from pathlib import Path

import yaml
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse

from .audit import (
    PRINCIPAL,
    AuditLog,
    AuditWriteError,
    connection_principal,
    new_call_id,
    parse_audit_config,
)
from .catalog import load_catalog, ToolRegistry
from .filter import check_command, check_paths
from .cli_executor import build_argv, run_tool, run_pipeline
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


CONFIG: dict | None = None
REGISTRY: ToolRegistry | None = None
AUDIT: AuditLog | None = None
mcp_server = Server("cli-mcp-server")
sse_transport = SseServerTransport("/messages/")


def get_config() -> dict:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()
    return CONFIG


def get_audit() -> AuditLog:
    global AUDIT
    if AUDIT is None:
        AUDIT = AuditLog(parse_audit_config(get_config().get("audit")))
    return AUDIT


def _startup_tools(registry: ToolRegistry) -> list[dict]:
    """The permitted surface, as the startup record describes it.

    Rule patterns are recorded verbatim rather than counted: the question this
    answers is "what was this node permitting when that call came in", and a
    count cannot answer it.
    """
    tools = []
    for e in registry:
        tool = {
            "name": e.name,
            "binary_raw": e.binary_raw,
            "binary": e.binary,
            "healthy": e.healthy,
            "pipe_stage": e.pipe_stage,
            "timeout_seconds": e.timeout_seconds,
            "max_bytes": e.max_bytes,
            "allow": e.rules.get("allow", []),
            "deny": e.rules.get("deny", []),
            "path_deny": e.path_deny,
        }
        if not e.healthy:
            tool["unhealthy_reason"] = e.unhealthy_reason
        tools.append(tool)
    return tools


def get_registry() -> ToolRegistry:
    global REGISTRY
    if REGISTRY is None:
        cat_cfg = get_config().get("catalog") or {}
        catalog_path = cat_cfg.get("path", "/etc/cli-mcp-server/catalog/")
        REGISTRY = load_catalog(
            catalog_path,
            defaults=cat_cfg.get("defaults") or {},
            search_paths=cat_cfg.get("search_paths") or DEFAULT_SEARCH_PATHS,
        )
        # Emitted once per load, so the log is self-describing: a reader can
        # answer what was permitted at the time of a call without recovering
        # the config file as it existed then.
        try:
            get_audit().startup(
                node=get_config()["server"]["node_name"],
                catalog_path=str(catalog_path),
                tools=_startup_tools(REGISTRY),
            )
        except AuditWriteError:
            # Refusing to serve because the startup record did not land would
            # take the node down for a logging fault. The per-call gate is
            # what enforces the deny posture; see call_tool.
            pass
    return REGISTRY


def _envelope(node: str, name: str, command: str | None, **rest) -> list[dict]:
    payload = {"node": node, "tool": name}
    if command is not None:
        payload["command"] = command
    payload.update(rest)
    return [{"type": "text", "text": json.dumps(payload)}]


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


def _audit_stages(stages) -> list[dict]:
    """The resolved argv for each stage, as the audit log records it.

    Built with the executor's own build_argv so what is logged cannot drift
    from what is spawned. A stage whose args will not tokenize is recorded as
    such rather than omitted — a hole in the record is worse than an ugly one.
    """
    recorded = []
    for entry, args in stages:
        try:
            recorded.append({"tool": entry.name, "argv": build_argv(entry, args)})
        except ValueError as e:
            recorded.append({"tool": entry.name, "argv_error": str(e)})
    return recorded


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    config = get_config()
    node_name = config["server"]["node_name"]
    registry = get_registry()
    audit = get_audit()

    command = arguments.get("command", "")
    call_id = new_call_id()

    def record(decision: str, reason: str | None = None, stages=None) -> None:
        """Write the decision record. Raises AuditWriteError under the deny
        posture, which the caller converts into a refusal."""
        audit.decision(
            call_id=call_id,
            node=node_name,
            tool=name,
            command=command,
            decision=decision,
            reason=reason,
            stages=_audit_stages(stages) if stages is not None else None,
        )

    def denied(reason: str, status: str = "denied"):
        record("deny" if status == "denied" else "error", reason)
        return _envelope(node_name, name, command, status=status, error=reason)

    try:
        entry = registry.get(name)
        if entry is None:
            return denied(f"Unknown tool: {name}", status="error")

        if not entry.healthy:
            return denied(
                f"Tool unavailable on this node: {entry.unhealthy_reason}",
                status="error",
            )

        try:
            segments = parse_pipeline(command)
        except PipelineGrammarError as e:
            return denied(str(e))

        try:
            stages = resolve_pipeline(entry, segments, registry)
        except PipelineResolutionError as e:
            return denied(str(e))

        for i, (seg_entry, seg_args) in enumerate(stages):
            if not seg_entry.healthy:
                return denied(
                    f"segment {i} ({seg_entry.name}): {seg_entry.unhealthy_reason}",
                    status="error",
                )
            allowed, reason = check_command(seg_args, seg_entry.rules)
            if not allowed:
                return denied(f"segment {i} ({seg_entry.name}): {reason}")
            ok, preason = check_paths(seg_args, seg_entry.path_deny)
            if not ok:
                return denied(f"segment {i} ({seg_entry.name}): {preason}")

        # Written before the subprocess is spawned, on purpose: a call that
        # wedges or takes the process down still leaves proof it was
        # authorized and started.
        record("allow", stages=stages)

    except AuditWriteError as e:
        # on_write_failure='deny'. Nothing has been spawned at any point that
        # can raise here, so refusing is truthful: the command did not run.
        return _envelope(
            node_name, name, command,
            status="error",
            error=f"call refused: audit sink unavailable ({e})",
        )

    if len(stages) == 1:
        result = await run_tool(entry, command)
    else:
        result = await run_pipeline(stages)

    try:
        audit.outcome(
            call_id=call_id,
            node=node_name,
            status=result.get("status", "unknown"),
            exit_code=result.get("exit_code"),
            execution_time_ms=result.get("execution_time_ms"),
            truncated=result.get("truncated"),
        )
    except AuditWriteError:
        # The deny posture gates *execution*. The subprocess has already run,
        # so converting a real result into a refusal would report a falsehood
        # to the caller; the drop is counted instead and surfaces on the next
        # record that lands.
        pass

    result["node"] = node_name
    result["tool"] = name
    result["command"] = command
    return [{"type": "text", "text": json.dumps(result)}]


async def handle_sse(scope, receive, send):
    # Caller attribution must be established HERE, not in handle_messages.
    # A tool call does not execute in the POST request's task: the message is
    # handed to this session's stream and the handler runs in the task group
    # inside mcp_server.run(), below. anyio tasks inherit context at spawn, so
    # a ContextVar set here reaches call_tool and one set in handle_messages
    # does not. Attribution is therefore connection-scoped.
    #
    # When authentication lands it slots in at this same point:
    # connect_sse already reads scope["user"] and checks AuthenticatedUser.
    PRINCIPAL.set(connection_principal(scope))
    async with sse_transport.connect_sse(scope, receive, send) as streams:
        await mcp_server.run(
            streams[0], streams[1], mcp_server.create_initialization_options()
        )


async def handle_messages(scope, receive, send):
    await sse_transport.handle_post_message(scope, receive, send)


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


app = Starlette(
    routes=[
        Route("/health", health),
        Mount("/mcp/messages", app=handle_messages),
        Mount("/mcp", app=handle_sse),
    ],
)
