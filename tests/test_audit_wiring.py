"""server.call_tool() must leave an audit record on every path out.

call_tool has eight exits — seven refusals plus the execute path — and the
failure mode this file exists to catch is a new one being added without a
record. Each test asserts the exact record count, not merely that *a* record
appeared.

The denial tests also assert absence of effect: the stub binary appends to a
marker file when it runs, so an empty marker is what proves the subprocess
never started. That makes "decision without outcome" verifiably mean what the
schema claims it means, rather than being a property of the logging alone.
"""

import json
import stat
import textwrap

import pytest

from cli_mcp import server as srv
from cli_mcp.audit import PRINCIPAL


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ran.log"
    audit_path = tmp_path / "audit.jsonl"

    # Records every invocation to an absolute path baked in at write time —
    # the executor's sanitized env means the stub cannot be told at runtime.
    fake = bin_dir / "fakebin"
    fake.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        with open({str(marker)!r}, "a") as f:
            f.write(" ".join(sys.argv[1:]) + "\\n")
        print(json.dumps({{"argv": sys.argv[1:]}}))
    """))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    upper = bin_dir / "upperbin"
    upper.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write(sys.stdin.read().upper())\n"
    )
    upper.chmod(upper.stat().st_mode | stat.S_IEXEC)

    failing = bin_dir / "failbin"
    failing.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
    failing.chmod(failing.stat().st_mode | stat.S_IEXEC)

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "audit.yaml").write_text(textwrap.dedent("""\
        - name: fake
          description: Fake tool.
          binary: fakebin
          prepend_args: ["sub"]
          timeout_seconds: 5
          rules:
            deny: ["destroy*"]
            allow: ["ok*"]
        - name: pathy
          description: Tool with a path deny rule.
          binary: fakebin
          timeout_seconds: 5
          rules:
            allow: ["*"]
          path_rules:
            deny: ["/etc/shadow"]
        - name: upper
          description: Uppercase stdin.
          binary: upperbin
          pipe_stage: true
          timeout_seconds: 5
          rules:
            allow: ["*"]
        - name: brokenpipe
          description: Pipe stage whose binary is absent.
          binary: not-installed
          pipe_stage: true
          rules:
            allow: ["*"]
        - name: missing
          description: Tool whose binary is absent.
          binary: not-installed
          rules:
            allow: ["*"]
        - name: failing
          description: Tool that exits nonzero.
          binary: failbin
          timeout_seconds: 5
          rules:
            allow: ["*"]
    """))

    cfg = {
        "server": {"host": "127.0.0.1", "port": 0, "node_name": "audit-node"},
        "catalog": {"path": str(catalog), "search_paths": [str(bin_dir)]},
        "audit": {"destination": str(audit_path)},
    }
    monkeypatch.setattr(srv, "CONFIG", cfg)
    monkeypatch.setattr(srv, "REGISTRY", None)
    monkeypatch.setattr(srv, "AUDIT", None)

    class Env:
        def records(self, event=None):
            if not audit_path.exists():
                return []
            out = [json.loads(ln) for ln in audit_path.read_text().splitlines() if ln.strip()]
            return [r for r in out if event is None or r["event"] == event]

        @property
        def ran(self) -> list[str]:
            return marker.read_text().splitlines() if marker.exists() else []

    yield Env()

    monkeypatch.setattr(srv, "REGISTRY", None)
    monkeypatch.setattr(srv, "AUDIT", None)


def envelope(out) -> dict:
    return json.loads(out[0]["text"])


# -- the seven refusals --------------------------------------------------

REFUSALS = [
    pytest.param("nosuchtool", "x", "error", "Unknown tool", id="unknown-tool"),
    pytest.param("missing", "anything", "error", "Tool unavailable", id="unhealthy-tool"),
    pytest.param("fake", "ok; rm -rf /", "denied", "forbidden metacharacter", id="grammar"),
    pytest.param("fake", "ok | nosuchstage", "denied", "unknown tool", id="resolution"),
    pytest.param("fake", "ok | brokenpipe x", "error", "not found", id="unhealthy-segment"),
    pytest.param("fake", "destroy everything", "denied", "deny rule", id="command-deny"),
    pytest.param("pathy", "--file=/etc/shadow", "denied", "path matches deny rule", id="path-deny"),
]


@pytest.mark.parametrize("tool,command,status,fragment", REFUSALS)
async def test_refusal_records_a_decision_and_never_an_outcome(
    env, tool, command, status, fragment
):
    body = envelope(await srv.call_tool(tool, {"command": command}))
    assert body["status"] == status
    assert fragment.lower() in body["error"].lower()

    decisions = env.records("decision")
    outcomes = env.records("outcome")

    assert len(decisions) == 1, "every exit writes exactly one decision record"
    assert outcomes == [], "a refused call must never produce an outcome record"

    assert decisions[0]["decision"] == ("deny" if status == "denied" else "error")
    assert decisions[0]["tool"] == tool
    assert decisions[0]["command"] == command
    assert decisions[0]["node"] == "audit-node"

    # The asymmetry is only meaningful if it tracks reality.
    assert env.ran == [], "no subprocess may run on a refused call"


def test_every_refusal_path_is_covered():
    """Guards the parametrization against call_tool growing an eighth exit.

    If a refusal path is added without a case above, this count is the thing
    that fails — the new path would otherwise just silently go unaudited.
    """
    import inspect

    source = inspect.getsource(srv.call_tool)
    assert source.count("return denied(") == len(REFUSALS)


# -- the execute path ----------------------------------------------------

async def test_allowed_call_records_decision_then_outcome(env):
    body = envelope(await srv.call_tool("fake", {"command": "ok hello"}))
    assert body["status"] == "success"

    decisions = env.records("decision")
    outcomes = env.records("outcome")
    assert len(decisions) == 1
    assert len(outcomes) == 1

    decision, outcome = decisions[0], outcomes[0]
    assert decision["decision"] == "allow"
    assert (decision["node"], decision["call_id"]) == (outcome["node"], outcome["call_id"])
    assert outcome["status"] == "success"
    assert outcome["exit_code"] == 0
    assert outcome["execution_time_ms"] >= 0

    assert env.ran == ["sub ok hello"]


async def test_decision_records_the_resolved_argv(env):
    """The command string is what was asked for; argv is what was run."""
    await srv.call_tool("fake", {"command": "ok hello"})

    stages = env.records("decision")[0]["stages"]
    assert len(stages) == 1
    assert stages[0]["tool"] == "fake"
    assert stages[0]["argv"][1:] == ["sub", "ok", "hello"]
    assert stages[0]["argv"][0].endswith("fakebin")     # resolved, not the bare name
    # And it is the argv the subprocess actually received.
    assert env.ran == [" ".join(stages[0]["argv"][1:])]


async def test_pipeline_records_every_stage(env):
    await srv.call_tool("fake", {"command": "ok hi | upper"})

    stages = env.records("decision")[0]["stages"]
    assert [s["tool"] for s in stages] == ["fake", "upper"]
    assert stages[1]["argv"][0].endswith("upperbin")


async def test_failed_execution_records_an_outcome_without_failure_text(env):
    """A nonzero exit is still an executed call: decision AND outcome."""
    # NB: a bare "" command cannot be used here — parse_pipeline rejects it as
    # an empty segment before the filter is reached, so the 0.2.0 empty-args
    # fix in check_command is unreachable through call_tool. Pre-existing and
    # unrelated to auditing; not fixed here to keep this change single-purpose.
    body = envelope(await srv.call_tool("failing", {"command": "x"}))
    assert body["status"] == "error"

    outcome = env.records("outcome")[0]
    assert outcome["status"] == "error"
    assert outcome["exit_code"] == 3
    # Subprocess output never reaches the audit log, not even on failure.
    assert "error" not in outcome


# -- startup record ------------------------------------------------------

async def test_startup_record_precedes_the_first_call(env):
    await srv.call_tool("fake", {"command": "ok hi"})

    records = env.records()
    assert records[0]["event"] == "startup"
    assert records[0]["node"] == "audit-node"

    by_name = {t["name"]: t for t in records[0]["tools"]}
    assert by_name["fake"]["allow"] == ["ok*"]
    assert by_name["fake"]["deny"] == ["destroy*"]
    assert by_name["pathy"]["path_deny"] == ["/etc/shadow"]
    assert by_name["missing"]["healthy"] is False
    assert "not-installed" in by_name["missing"]["unhealthy_reason"]


async def test_startup_record_is_written_once(env):
    for _ in range(3):
        await srv.call_tool("fake", {"command": "ok hi"})
    assert len(env.records("startup")) == 1


# -- principal -----------------------------------------------------------

async def test_principal_is_unattributed_without_a_connection(env):
    await srv.call_tool("fake", {"command": "ok hi"})
    principal = env.records("decision")[0]["principal"]
    assert principal["authenticated"] is False


async def test_principal_is_carried_from_the_contextvar(env):
    """What handle_sse sets is what the decision record reports."""
    PRINCIPAL.set({"authenticated": False, "transport": "sse", "peer": "10.0.0.9"})
    try:
        await srv.call_tool("fake", {"command": "ok hi"})
    finally:
        PRINCIPAL.set(None)

    principal = env.records("decision")[0]["principal"]
    assert principal["peer"] == "10.0.0.9"
    assert principal["authenticated"] is False


# -- failure posture at the call boundary --------------------------------

async def test_fail_closed_refuses_the_call_and_runs_nothing(env, monkeypatch, tmp_path):
    """on_write_failure='deny' must gate execution, not just logging."""
    srv.AUDIT = None
    srv.CONFIG["audit"] = {
        "destination": str(tmp_path / "no-such-dir" / "audit.jsonl"),
        "on_write_failure": "deny",
    }

    body = envelope(await srv.call_tool("fake", {"command": "ok hi"}))
    assert body["status"] == "error"
    assert "audit sink unavailable" in body["error"]
    assert env.ran == [], "an unauditable call must not execute"


async def test_fail_open_executes_and_counts_the_drop(env, tmp_path):
    srv.AUDIT = None
    srv.CONFIG["audit"] = {
        "destination": str(tmp_path / "no-such-dir" / "audit.jsonl"),
        "on_write_failure": "continue",
    }

    body = envelope(await srv.call_tool("fake", {"command": "ok hi"}))
    assert body["status"] == "success"
    assert env.ran == ["sub ok hi"]
    assert srv.get_audit().dropped > 0
