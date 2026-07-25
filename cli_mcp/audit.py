"""Audit logging for the filter's decisions.

The filter is the product; this is the record that it ran. Every tool call
emits two records correlated by `call_id`:

  * `decision` — written *before* the subprocess is spawned. Carries the
    resolved argv, the allow/deny outcome, and the reason.
  * `outcome` — written after it finishes. Carries status, exit code, elapsed
    time, truncation.

A denied call emits a `decision` and *never* an `outcome`. That asymmetry is
load-bearing: the absence of an outcome record bearing a given call_id is the
machine-checkable proof that the subprocess never ran. Consumers must treat
the join as an outer join and must not read unmatched decisions as dropped
records — see "Audit logging" in README.md.

Two content rules, both deliberate:

  * Subprocess stdout/stderr is never logged, not even on error. Tool output
    is arbitrary system data and belongs to a different retention and access
    class than an audit trail. The audit log records *that* a call failed and
    its exit code; the failure text goes to the caller only.
  * Records are JSON Lines. JSON escaping is what makes an attacker-supplied
    command string safe to write — embedded newlines and terminal escapes
    cannot forge a record boundary. Do not "improve" this into a formatted
    string.

Writes are synchronous. At this call volume that is the right trade (ordered,
no loss window, no drain task), but it does mean a hung filesystem stalls the
event loop — see "Known thin spots" in docs/TESTING.md.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class AuditConfigError(Exception):
    """Raised for a malformed `audit:` block in the server config."""


class AuditWriteError(Exception):
    """Raised when a record could not be written and on_write_failure='deny'."""


STDERR = "stderr"
CONTINUE = "continue"
DENY = "deny"

DEFAULT_MAX_COMMAND_BYTES = 4096


# Connection-scoped caller attribution. Set in server.handle_sse before
# mcp_server.run(); read here when a record is built.
#
# It must be set on the SSE connection, NOT on the POST that carries the tool
# call. A tool call does not execute in the POST request's task — the message
# is handed to the session's stream and the handler runs in the task group
# inside mcp_server.run(), spawned from handle_sse. A ContextVar set in
# handle_messages is therefore invisible here; one set in handle_sse
# propagates, because anyio tasks inherit context at spawn time. This was
# verified against the real transport before the field list was chosen.
#
# Consequence: attribution is connection-scoped, not request-scoped.
PRINCIPAL: ContextVar[dict | None] = ContextVar("audit_principal", default=None)

UNATTRIBUTED: dict = {"authenticated": False, "transport": None}


def new_call_id() -> str:
    """Join key for a call's decision and outcome records.

    uuid4 rather than a counter: a counter resets on restart and collides
    across nodes, which breaks joins in an aggregated multi-node log — the
    deployment this server is built for.
    """
    return uuid.uuid4().hex


def connection_principal(scope: dict) -> dict:
    """Build the caller attribution available from an SSE connection scope.

    Connections are unauthenticated today, so this is deliberately an honest
    partial: it says who the transport *appears* to be, and says plainly that
    nobody verified it. `authenticated` is present and False from day one so
    that adding auth later is a value change rather than a schema change, and
    so a reader cannot mistake an absent field for a trusted caller.

    Proxy headers (X-Forwarded-For and friends) are deliberately NOT consulted
    — they are caller-supplied and would let the caller choose what the audit
    log says about them.
    """
    client = scope.get("client") or (None, None)
    headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
    user_agent = headers.get(b"user-agent")

    principal: dict[str, Any] = {
        "authenticated": False,
        "transport": "sse",
        "connection": new_call_id(),
        "peer": client[0],
        "peer_port": client[1],
    }
    if user_agent is not None:
        principal["user_agent"] = user_agent.decode("utf-8", errors="replace")
    return principal


@dataclass
class AuditConfig:
    enabled: bool = True
    destination: str = STDERR
    max_command_bytes: int = DEFAULT_MAX_COMMAND_BYTES
    on_write_failure: str = CONTINUE


def parse_audit_config(raw: dict | None) -> AuditConfig:
    """Validate and materialize the `audit:` block. Absent block = defaults."""
    if raw is None:
        return AuditConfig()
    if not isinstance(raw, dict):
        raise AuditConfigError(f"'audit' must be a mapping, got {type(raw).__name__}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AuditConfigError("audit.enabled must be a boolean")

    destination = raw.get("destination", STDERR)
    if not isinstance(destination, str) or not destination:
        raise AuditConfigError("audit.destination must be 'stderr' or an absolute path")
    if destination != STDERR and not os.path.isabs(destination):
        raise AuditConfigError(
            f"audit.destination must be 'stderr' or an absolute path, got {destination!r}"
        )

    max_command_bytes = raw.get("max_command_bytes", DEFAULT_MAX_COMMAND_BYTES)
    if not isinstance(max_command_bytes, int) or isinstance(max_command_bytes, bool):
        raise AuditConfigError("audit.max_command_bytes must be an integer")
    if max_command_bytes < 1:
        raise AuditConfigError("audit.max_command_bytes must be >= 1")

    on_write_failure = raw.get("on_write_failure", CONTINUE)
    if on_write_failure not in (CONTINUE, DENY):
        raise AuditConfigError(
            f"audit.on_write_failure must be {CONTINUE!r} or {DENY!r}, "
            f"got {on_write_failure!r}"
        )

    return AuditConfig(
        enabled=enabled,
        destination=destination,
        max_command_bytes=max_command_bytes,
        on_write_failure=on_write_failure,
    )


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _cap(text: str, limit: int) -> tuple[str, int, bool]:
    """Truncate `text` to `limit` UTF-8 bytes without splitting a character.

    Returns (capped, original_byte_length, was_truncated). Commands are
    attacker-controlled and unbounded; an audit log that can be flooded by one
    call is one that can be used to hide the next.
    """
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, len(raw), False
    return raw[:limit].decode("utf-8", errors="ignore"), len(raw), True


@dataclass
class AuditLog:
    """JSON Lines audit sink.

    A file destination is opened per record rather than held open. That costs
    a syscall at this volume and buys two things: log rotation by rename works
    with no SIGHUP handler, and there is no descriptor to leak or to go stale.
    """

    config: AuditConfig
    dropped: int = 0
    _complained: bool = field(default=False, repr=False)

    # -- sink ------------------------------------------------------------

    def _emit(self, record: dict) -> None:
        line = json.dumps(record, default=str)
        if self.config.destination == STDERR:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        else:
            with open(self.config.destination, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def write(self, record: dict) -> None:
        """Write one record. Raises AuditWriteError iff on_write_failure='deny'.

        On a swallowed failure the drop is counted, and the count rides along
        on the next record that does land (`audit_dropped`). Loss stays visible
        in the log itself rather than only in a stderr line nobody kept.
        """
        if not self.config.enabled:
            return

        if self.dropped:
            record = {**record, "audit_dropped": self.dropped}

        try:
            self._emit(record)
        except OSError as e:
            self.dropped += 1
            self._complain(e)
            if self.config.on_write_failure == DENY:
                raise AuditWriteError(f"audit write failed: {e}") from e
            return

        self.dropped = 0

    def _complain(self, exc: Exception) -> None:
        """One line to stderr the first time the sink fails, then silence.

        Bounded on purpose: the common cause is a full disk, and a per-call
        complaint would be the thing that fills the remaining space.
        """
        if self._complained:
            return
        self._complained = True
        try:
            sys.stderr.write(
                f"cli-mcp-server: AUDIT SINK FAILING ({self.config.destination}): {exc}. "
                f"on_write_failure={self.config.on_write_failure}; "
                f"further failures counted, not reported.\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

    # -- records ---------------------------------------------------------

    def startup(self, node: str, catalog_path: str, tools: list[dict]) -> None:
        """Record what this server was configured to permit.

        Emitted once at catalog load so the log is self-describing: a reader
        can answer "what was permitted at the time of that call" without
        recovering the config file as it existed then. The failure posture is
        included because otherwise it is an invisible property of the
        deployment.
        """
        self.write({
            "ts": _now(),
            "event": "startup",
            "node": node,
            "catalog_path": catalog_path,
            "audit": {
                "destination": self.config.destination,
                "on_write_failure": self.config.on_write_failure,
                "max_command_bytes": self.config.max_command_bytes,
            },
            "tools": tools,
        })

    def decision(
        self,
        *,
        call_id: str,
        node: str,
        tool: str,
        command: str,
        decision: str,
        reason: str | None = None,
        stages: list[dict] | None = None,
    ) -> None:
        capped, raw_len, truncated = self._cap_command(command)
        record: dict[str, Any] = {
            "ts": _now(),
            "event": "decision",
            "call_id": call_id,
            "node": node,
            "tool": tool,
            "command": capped,
        }
        if truncated:
            record["command_truncated"] = True
            record["command_bytes"] = raw_len
        record["decision"] = decision
        if reason is not None:
            record["reason"] = reason
        if stages is not None:
            record["stages"] = stages
        record["principal"] = PRINCIPAL.get() or UNATTRIBUTED
        self.write(record)

    def outcome(
        self,
        *,
        call_id: str,
        node: str,
        status: str,
        exit_code: int | None = None,
        execution_time_ms: int | None = None,
        truncated: bool | None = None,
    ) -> None:
        """Outcome of a call that was allowed to run.

        `node` is repeated here rather than being recovered by joining back to
        the decision record, so that every line stands alone under a naive
        grep and the join key is (node, call_id).

        No failure text: see the module docstring. Status and exit code say
        that it failed and how; the text went to the caller.
        """
        record: dict[str, Any] = {
            "ts": _now(),
            "event": "outcome",
            "call_id": call_id,
            "node": node,
            "status": status,
        }
        if exit_code is not None:
            record["exit_code"] = exit_code
        if execution_time_ms is not None:
            record["execution_time_ms"] = execution_time_ms
        if truncated:
            record["truncated"] = True
        self.write(record)

    def _cap_command(self, command: str) -> tuple[str, int, bool]:
        return _cap(command, self.config.max_command_bytes)
