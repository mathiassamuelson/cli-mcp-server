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

class _Headers:
    """Minimal stand-in for starlette's Headers: case-insensitive getlist.

    Hand-rolled so this file keeps its stdlib-only promise.
    """

    def __init__(self, pairs):
        self._pairs = [(k.lower(), v) for k, v in pairs]

    def getlist(self, name):
        return [v for k, v in self._pairs if k == name.lower()]


@check("identity-refuses-empty", "0.3.0",
       "an empty forwarded identity is a refusal, never recorded as a value")
def _identity_refuses_empty() -> Result:
    from cli_mcp.identity import IdentityConfig, IdentityRefused, resolve_identity

    cfg = IdentityConfig(header="X-Id", require=True)

    for label, pairs in [
        ("absent", []),
        ("empty", [("X-Id", "")]),
        ("blank", [("X-Id", "   ")]),
    ]:
        try:
            got = resolve_identity(_Headers(pairs), cfg)
        except IdentityRefused:
            continue
        return Result(False, f"{label} identity resolved to {got!r} instead of refusing")

    if resolve_identity(_Headers([("X-Id", "a@b.com")]), cfg) != "a@b.com":
        return Result(False, "a present identity did not resolve")

    return Result(True, "absent/empty/blank all refused; present resolves")


@check("identity-proxy-secret", "0.3.0",
       "a wrong or missing proxy secret is refused before identity is read")
def _identity_proxy_secret() -> Result:
    from cli_mcp.identity import IdentityConfig, IdentityRefused, resolve_identity

    cfg = IdentityConfig(
        header="X-Id", require=True, proxy_header="X-Proxy", proxy_secret="right"
    )

    for label, pairs in [
        ("missing", [("X-Id", "a@b.com")]),
        ("wrong", [("X-Id", "a@b.com"), ("X-Proxy", "wrong")]),
        ("empty", [("X-Id", "a@b.com"), ("X-Proxy", "")]),
    ]:
        try:
            resolve_identity(_Headers(pairs), cfg)
        except IdentityRefused:
            continue
        return Result(False, f"{label} proxy secret was accepted")

    ok = resolve_identity(_Headers([("X-Id", "a@b.com"), ("X-Proxy", "right")]), cfg)
    if ok != "a@b.com":
        return Result(False, f"correct secret did not resolve identity: {ok!r}")

    return Result(True, "missing/wrong/empty secret refused; correct one passes")


@check("identity-duplicate-header", "0.3.0",
       "a repeated identity header is refused rather than resolved to one value")
def _identity_duplicate_header() -> Result:
    from cli_mcp.identity import IdentityConfig, IdentityRefused, resolve_identity

    cfg = IdentityConfig(header="X-Id", require=True)
    pairs = [("X-Id", "real@b.com"), ("X-Id", "forged@b.com")]
    try:
        got = resolve_identity(_Headers(pairs), cfg)
    except IdentityRefused:
        return Result(True, "duplicate identity header refused")
    return Result(False, f"duplicate header resolved to {got!r}")


@check("identity-misconfig-refuses-load", "0.3.0",
       "an unset proxy-secret env var stops config load instead of accepting all")
def _identity_misconfig() -> Result:
    from cli_mcp.identity import IdentityConfig, IdentityMisconfigured

    var = "CLI_MCP_CONFORMANCE_SECRET"
    os.environ.pop(var, None)
    block = {
        "identity": {
            "header": "X-Id",
            "proxy_header": "X-Proxy",
            "proxy_secret_env": var,
        }
    }
    try:
        IdentityConfig.from_config(block)
    except IdentityMisconfigured:
        pass
    else:
        return Result(False, "unset secret env var loaded without complaint")

    os.environ[var] = "s3cret"
    try:
        cfg = IdentityConfig.from_config(block)
        if cfg.proxy_secret != "s3cret":
            return Result(False, "secret not read from the environment")
    finally:
        os.environ.pop(var, None)

    return Result(True, "unset secret refuses to load; set secret is read")


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
