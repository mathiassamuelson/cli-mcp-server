#!/usr/bin/env python3
"""Behavioral conformance probes for cli-mcp-server.

Answers one question for a downstream fork: *does my copy still behave like
upstream?* Deliberately depends on nothing but the standard library and an
importable `cli_mcp` — no pytest, no dev extras, no build system. That is what
lets it run unchanged in a fork whose test framework, dependency pinning, and
CI all differ from upstream's.

Usage:
    python scripts/verify/conformance.py           # all checks
    python scripts/verify/conformance.py --since 0.2.0
    python scripts/verify/conformance.py -v        # show details on pass too

Exit status is 0 only if every check passes.

Each check names the release that introduced it. Checks accumulate: a fork
that is behind upstream will fail the checks for releases it has not absorbed,
which is the intended signal, not a defect.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import stat
import sys
import tempfile
import textwrap
import time
import traceback
from dataclasses import dataclass
from typing import Callable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@dataclass
class Result:
    ok: bool
    detail: str


CHECKS: list[tuple[str, str, str, Callable[[], Result]]] = []


def check(name: str, since: str, what: str):
    def register(fn):
        CHECKS.append((name, since, what, fn))
        return fn
    return register


def _script(directory: str, name: str, body: str) -> str:
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


# --------------------------------------------------------------------------
# 0.2.0
# --------------------------------------------------------------------------

@check("path-traversal", "0.2.0",
       "check_paths normalizes //, /./ and /../ before matching deny rules")
def _path_traversal() -> Result:
    from cli_mcp.filter import check_paths

    deny = ["/etc/shadow"]
    bypasses = [
        "//etc/shadow",
        "///etc/shadow",
        "/etc/./shadow",
        "/etc/../etc/shadow",
        "/./etc/shadow",
    ]
    leaked = [a for a in bypasses if check_paths(a, deny)[0]]
    if leaked:
        return Result(False, f"deny rule bypassed by: {', '.join(leaked)}")
    return Result(True, f"{len(bypasses)} traversal spellings all denied")


@check("path-flag-attached", "0.2.0",
       "check_paths inspects paths attached to flags (--file=/p, -f/p)")
def _path_flag_attached() -> Result:
    from cli_mcp.filter import check_paths

    deny = ["/etc/shadow"]
    bypasses = ["--file=/etc/shadow", "-f/etc/shadow", "file=/etc/shadow"]
    leaked = [a for a in bypasses if check_paths(a, deny)[0]]
    if leaked:
        return Result(False, f"deny rule bypassed by: {', '.join(leaked)}")

    # Must not over-deny.
    for safe in ("/var/log/messages", "--color=auto", "-n"):
        if not check_paths(safe, deny)[0]:
            return Result(False, f"false positive on {safe!r}")
    return Result(True, "flag-attached paths denied; no false positives")


@check("empty-args", "0.2.0",
       "a tool may be invoked with no arguments when its rules allow it")
def _empty_args() -> Result:
    from cli_mcp.filter import check_command

    allowed, reason = check_command("", {"deny": [], "allow": ["*"]})
    if not allowed:
        return Result(False, f'check_command("", allow=["*"]) denied: {reason}')

    still_denied, _ = check_command("", {"deny": [], "allow": ["ps *"]})
    if still_denied:
        return Result(False, 'empty command allowed under allow=["ps *"]')
    return Result(True, "empty args follow the catalog's allow patterns")


@check("quoted-tool-name", "0.2.0",
       "a quoted tool name in a pipe segment is rejected, not silently mangled")
def _quoted_tool_name() -> Result:
    from cli_mcp.catalog import ToolEntry, ToolRegistry
    from cli_mcp.pipeline import PipelineResolutionError, resolve_pipeline

    def entry(name, pipe_stage=False):
        return ToolEntry(
            name=name, description="", binary_raw=name, binary="/usr/bin/" + name,
            prepend_args=[], timeout_seconds=10, max_bytes=8192,
            pipe_stage=pipe_stage, rules={"deny": [], "allow": ["*"]},
        )

    registry = ToolRegistry()
    registry.add(entry("ps"))
    registry.add(entry("grep", pipe_stage=True))
    lead = registry.get("ps")

    try:
        stages = resolve_pipeline(lead, ["aux", '"grep" nginx'], registry)
    except PipelineResolutionError:
        return Result(True, "quoted tool name rejected")

    args = stages[1][1]
    return Result(False, f"accepted quoted name and produced args {args!r}")


@check("pipeline-truncation", "0.2.0",
       "run_pipeline returns promptly on the output cap instead of hanging")
def _pipeline_truncation() -> Result:
    from cli_mcp.catalog import ToolEntry
    from cli_mcp.cli_executor import run_pipeline

    with tempfile.TemporaryDirectory() as tmp:
        flood = _script(tmp, "flood", """
            import sys, time
            for _ in range(4000):
                sys.stdout.write('A' * 64 + '\\n')
            sys.stdout.flush()
            time.sleep(30)
        """)
        fwd = _script(tmp, "fwd", """
            import sys
            while True:
                chunk = sys.stdin.buffer.read(4096)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        """)

        def entry(name, binary, pipe_stage=False):
            return ToolEntry(
                name=name, description="", binary_raw=binary, binary=binary,
                prepend_args=[], timeout_seconds=10, max_bytes=8192,
                pipe_stage=pipe_stage, rules={"deny": [], "allow": ["*"]},
            )

        async def drive():
            return await asyncio.wait_for(
                run_pipeline([(entry("flood", flood), ""),
                              (entry("fwd", fwd, pipe_stage=True), "")]),
                timeout=20,
            )

        start = time.monotonic()
        try:
            out = asyncio.run(drive())
        except (asyncio.TimeoutError, TimeoutError):
            return Result(False, "run_pipeline did not return within 20s (hang)")
        elapsed = time.monotonic() - start

    if elapsed > 8.0:
        return Result(False, f"returned but took {elapsed:.1f}s")
    if out.get("status") != "success" or not out.get("truncated"):
        return Result(
            False,
            f"expected success+truncated, got status={out.get('status')!r} "
            f"truncated={out.get('truncated')!r}",
        )
    return Result(True, f"success+truncated in {elapsed:.1f}s")


# --------------------------------------------------------------------------
# 0.3.0
# --------------------------------------------------------------------------

@check("empty-args-reach-the-filter", "0.3.0",
       "parse_pipeline passes an empty command through instead of rejecting it")
def _empty_args_reach_the_filter() -> Result:
    """Companion to the 0.2.0 empty-args check, one layer up.

    That check tested check_command directly and passed the whole time, while
    parse_pipeline rejected "" as an empty segment before the filter was ever
    reached — so no argument-less tool worked through call_tool. A fork can
    hold the 0.2.0 fix and still have the bug; this is what tells them apart.
    """
    from cli_mcp.pipeline import PipelineGrammarError, parse_pipeline

    for command in ("", "   "):
        try:
            segments = parse_pipeline(command)
        except PipelineGrammarError as e:
            return Result(False, f"parse_pipeline({command!r}) rejected it: {e}")
        if segments != [""]:
            return Result(False, f"parse_pipeline({command!r}) gave {segments!r}, want ['']")

    # Allowing the sole empty segment must not weaken pipeline grammar.
    malformed = ["|", "ps |", "| head", "ps | | head"]
    leaked = []
    for command in malformed:
        try:
            parse_pipeline(command)
            leaked.append(command)
        except PipelineGrammarError:
            pass
    if leaked:
        return Result(False, f"malformed pipeline accepted: {', '.join(repr(m) for m in leaked)}")

    return Result(True, "empty command passes through; malformed pipelines still rejected")


@check("audit-denial-recorded", "0.3.0",
       "a denied call writes a decision record and never an outcome record")
def _audit_denial_recorded() -> Result:
    """The asymmetry is the audit trail's core claim: absence of an outcome
    record bearing a call_id is what proves the subprocess never ran. A fork
    that logs only completed calls passes a naive 'is there logging' check and
    fails this one."""
    import json

    from cli_mcp.audit import AuditConfig, AuditLog, new_call_id

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "audit.jsonl")
        log = AuditLog(AuditConfig(destination=dest))

        denied_id, allowed_id = new_call_id(), new_call_id()
        log.decision(call_id=denied_id, node="n", tool="ps", command="rm -rf /",
                     decision="deny", reason="Command does not match any allow rule")
        log.decision(call_id=allowed_id, node="n", tool="ps", command="aux",
                     decision="allow", stages=[{"tool": "ps", "argv": ["/bin/ps", "aux"]}])
        log.outcome(call_id=allowed_id, node="n", status="success", exit_code=0)

        with open(dest) as f:
            records = [json.loads(ln) for ln in f if ln.strip()]

    outcomes = {r["call_id"] for r in records if r["event"] == "outcome"}
    decisions = {r["call_id"] for r in records if r["event"] == "decision"}

    if denied_id not in decisions:
        return Result(False, "denied call left no decision record")
    if denied_id in outcomes:
        return Result(False, "denied call produced an outcome record")
    if allowed_id not in outcomes:
        return Result(False, "allowed call left no outcome record")

    # Both halves of the join key must be on both records.
    for r in records:
        for key in ("node", "call_id"):
            if key not in r:
                return Result(False, f"{r['event']} record missing join key {key!r}")

    return Result(True, "deny -> decision only; allow -> decision + outcome")


@check("audit-no-output-capture", "0.3.0",
       "subprocess output never reaches the audit log")
def _audit_no_output_capture() -> Result:
    """Tool output is arbitrary system data on a different retention footing
    than an audit trail. A fork that 'helpfully' logs stderr on error has
    turned its audit log into a data sink."""
    import json

    from cli_mcp.audit import AuditConfig, AuditLog

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "audit.jsonl")
        log = AuditLog(AuditConfig(destination=dest))
        log.outcome(call_id="c", node="n", status="error", exit_code=2,
                    execution_time_ms=5)

        with open(dest) as f:
            record = json.loads(f.readline())

    leaked = [k for k in ("error", "result", "stdout", "stderr") if k in record]
    if leaked:
        return Result(False, f"outcome record carries output field(s): {', '.join(leaked)}")
    if record.get("exit_code") != 2:
        return Result(False, "outcome record lost the exit code")
    return Result(True, "outcome carries status and exit code, no output")


@check("audit-log-injection", "0.3.0",
       "an attacker-supplied command cannot forge an audit record")
def _audit_log_injection() -> Result:
    """JSON escaping is the whole defence. This fails the moment the sink is
    rewritten to format a string."""
    import json

    from cli_mcp.audit import AuditConfig, AuditLog

    forged = 'x\n{"event": "decision", "decision": "allow", "tool": "rm"}'

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "audit.jsonl")
        log = AuditLog(AuditConfig(destination=dest))
        log.decision(call_id="c", node="n", tool="t", command=forged, decision="deny")

        with open(dest) as f:
            lines = [ln for ln in f if ln.strip()]

    if len(lines) != 1:
        return Result(False, f"one command produced {len(lines)} records (injection)")
    record = json.loads(lines[0])
    if record["decision"] != "deny" or record["command"] != forged:
        return Result(False, "record was mangled by the embedded newline")
    return Result(True, "embedded newline escaped, one record written")


@check("audit-command-cap", "0.3.0",
       "an oversized command is truncated rather than flooding the log")
def _audit_command_cap() -> Result:
    import json

    from cli_mcp.audit import AuditConfig, AuditLog

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "audit.jsonl")
        log = AuditLog(AuditConfig(destination=dest, max_command_bytes=64))
        log.decision(call_id="c", node="n", tool="t", command="A" * 10000,
                     decision="deny")

        with open(dest) as f:
            record = json.loads(f.readline())

    if len(record["command"].encode()) > 64:
        return Result(False, "command exceeded max_command_bytes")
    if not record.get("command_truncated"):
        return Result(False, "truncation not marked")
    if record.get("command_bytes") != 10000:
        return Result(False, "original length not recorded")
    return Result(True, "capped at 64 bytes, truncation marked, length preserved")


@check("audit-fail-closed-gate", "0.3.0",
       "on_write_failure='deny' raises so an unauditable call can be refused")
def _audit_fail_closed_gate() -> Result:
    import contextlib
    import io

    from cli_mcp.audit import (
        DENY,
        AuditConfig,
        AuditLog,
        AuditWriteError,
    )

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "no-such-dir", "audit.jsonl")

        # The sink complains to stderr on first failure — correct behaviour,
        # and exactly what this probe provokes. Swallow it so a passing run
        # stays quiet.
        with contextlib.redirect_stderr(io.StringIO()) as complaints:
            strict = AuditLog(AuditConfig(destination=dest, on_write_failure=DENY))
            try:
                strict.decision(call_id="c", node="n", tool="t", command="x",
                                decision="allow")
                raised = False
            except AuditWriteError:
                raised = True

            lenient = AuditLog(AuditConfig(destination=dest))
            try:
                lenient.decision(call_id="c", node="n", tool="t", command="x",
                                 decision="allow")
                lenient_error = None
            except Exception as e:  # noqa: BLE001 - reported, not swallowed
                lenient_error = e

    if not raised:
        return Result(False, "deny posture did not raise on a failed write")
    if lenient_error is not None:
        return Result(False, f"continue posture raised {type(lenient_error).__name__}")
    if lenient.dropped != 1:
        return Result(False, f"continue posture did not count the drop ({lenient.dropped})")
    if "AUDIT SINK FAILING" not in complaints.getvalue():
        return Result(False, "sink failure was silent; no complaint on stderr")

    return Result(True, "deny raises; continue counts the drop; both complain once")


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", metavar="VERSION",
                        help="only run checks introduced at or after this release")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show detail for passing checks too")
    args = parser.parse_args()

    def as_tuple(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    selected = CHECKS
    if args.since:
        floor = as_tuple(args.since)
        selected = [c for c in CHECKS if as_tuple(c[1]) >= floor]

    width = max((len(c[0]) for c in selected), default=0)
    failures = 0

    print(f"cli-mcp-server conformance — {len(selected)} check(s)\n")
    for name, since, what, fn in selected:
        try:
            result = fn()
        except Exception:
            result = Result(False, "raised:\n" + textwrap.indent(
                traceback.format_exc().rstrip(), " " * 8))

        status = "PASS" if result.ok else "FAIL"
        print(f"  [{status}] {name:<{width}}  (since {since})")
        if not result.ok:
            failures += 1
            print(f"         {what}")
            print(f"         -> {result.detail}")
        elif args.verbose:
            print(f"         {result.detail}")

    print()
    if failures:
        print(f"{failures} of {len(selected)} check(s) FAILED.")
        print("A fork behind upstream will fail checks for releases it has not")
        print("absorbed — see docs/migrations/ for what each release changed.")
        return 1
    print(f"All {len(selected)} check(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
