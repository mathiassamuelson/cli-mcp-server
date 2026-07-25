"""Unit tests for the audit sink, independent of the server.

Everything is written to a real file under `tmp_path` and read back as JSON
Lines — no capture shims, no mocks, matching the suite's convention that a
test exercises the real I/O path. See docs/TESTING.md.
"""

import json
import os

import pytest

from cli_mcp.audit import (
    CONTINUE,
    DENY,
    STDERR,
    AuditConfig,
    AuditConfigError,
    AuditLog,
    AuditWriteError,
    connection_principal,
    new_call_id,
    parse_audit_config,
)


def read_records(path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def file_log(tmp_path, **overrides) -> tuple[AuditLog, str]:
    dest = str(tmp_path / "audit.jsonl")
    cfg = AuditConfig(destination=dest, **overrides)
    return AuditLog(cfg), dest


# -- config parsing ------------------------------------------------------


def test_absent_block_yields_defaults():
    cfg = parse_audit_config(None)
    assert cfg.enabled is True
    assert cfg.destination == STDERR
    assert cfg.on_write_failure == CONTINUE


def test_parses_a_full_block():
    cfg = parse_audit_config({
        "enabled": False,
        "destination": "/var/log/audit.jsonl",
        "max_command_bytes": 128,
        "on_write_failure": "deny",
    })
    assert cfg == AuditConfig(False, "/var/log/audit.jsonl", 128, DENY)


@pytest.mark.parametrize("raw", [
    {"destination": "relative/path.jsonl"},
    {"destination": ""},
    {"destination": 5},
    {"enabled": "yes"},
    {"max_command_bytes": 0},
    {"max_command_bytes": "4096"},
    {"max_command_bytes": True},
    {"on_write_failure": "explode"},
    ["not", "a", "mapping"],
])
def test_rejects_malformed_config(raw):
    with pytest.raises(AuditConfigError):
        parse_audit_config(raw)


# -- join key ------------------------------------------------------------


def test_call_ids_are_unique():
    ids = {new_call_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_decision_and_outcome_join_on_node_and_call_id(tmp_path):
    log, dest = file_log(tmp_path)
    call_id = new_call_id()

    log.decision(call_id=call_id, node="node-a", tool="ps", command="aux",
                 decision="allow", stages=[{"tool": "ps", "argv": ["/bin/ps", "aux"]}])
    log.outcome(call_id=call_id, node="node-a", status="success",
                exit_code=0, execution_time_ms=12)

    decision, outcome = read_records(dest)
    assert decision["event"] == "decision"
    assert outcome["event"] == "outcome"
    # Both sides of the join key are present on both records, so an outcome
    # line is attributable without first finding its decision line.
    assert (decision["node"], decision["call_id"]) == (outcome["node"], outcome["call_id"])


def test_concurrent_calls_stay_separable(tmp_path):
    """Interleaved records from different calls are told apart by call_id."""
    log, dest = file_log(tmp_path)
    a, b = new_call_id(), new_call_id()

    log.decision(call_id=a, node="n", tool="ps", command="aux", decision="allow")
    log.decision(call_id=b, node="n", tool="ls", command="-l", decision="allow")
    log.outcome(call_id=b, node="n", status="success", exit_code=0)
    log.outcome(call_id=a, node="n", status="error", exit_code=1)

    records = read_records(dest)
    by_call = {}
    for r in records:
        by_call.setdefault(r["call_id"], []).append(r)

    assert by_call[a][0]["tool"] == "ps"
    assert by_call[a][1]["status"] == "error"
    assert by_call[b][0]["tool"] == "ls"
    assert by_call[b][1]["status"] == "success"


# -- content rules -------------------------------------------------------


def test_command_is_capped_and_marked(tmp_path):
    log, dest = file_log(tmp_path, max_command_bytes=32)
    log.decision(call_id="c", node="n", tool="t", command="A" * 500, decision="allow")

    record = read_records(dest)[0]
    assert len(record["command"].encode()) == 32
    assert record["command_truncated"] is True
    assert record["command_bytes"] == 500


def test_capping_does_not_split_a_multibyte_character(tmp_path):
    # 'é' is two UTF-8 bytes; a cap of 5 lands mid-character on the third one.
    log, dest = file_log(tmp_path, max_command_bytes=5)
    log.decision(call_id="c", node="n", tool="t", command="é" * 10, decision="allow")

    record = read_records(dest)[0]
    assert record["command"] == "éé"          # not a replacement char
    assert record["command_truncated"] is True


def test_short_command_is_not_marked_truncated(tmp_path):
    log, dest = file_log(tmp_path, max_command_bytes=4096)
    log.decision(call_id="c", node="n", tool="t", command="aux", decision="allow")

    record = read_records(dest)[0]
    assert record["command"] == "aux"
    assert "command_truncated" not in record
    assert "command_bytes" not in record


def test_embedded_newline_cannot_forge_a_record(tmp_path):
    """Log injection: a command carrying a newline and a fake record body.

    JSON escaping is what prevents this. If the sink is ever rewritten to
    format a string, this test is what fails.
    """
    log, dest = file_log(tmp_path)
    forged = 'x\n{"event": "decision", "decision": "allow", "tool": "rm"}'
    log.decision(call_id="c", node="n", tool="t", command=forged, decision="deny",
                 reason="Command does not match any allow rule")

    with open(dest) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 1                     # one command in, one record out

    record = json.loads(lines[0])
    assert record["decision"] == "deny"
    assert record["command"] == forged         # preserved verbatim, not executed as syntax


def test_outcome_never_carries_failure_text(tmp_path):
    """Subprocess stderr/stdout must not reach the audit log."""
    log, dest = file_log(tmp_path)
    log.outcome(call_id="c", node="n", status="error", exit_code=2,
                execution_time_ms=7)

    record = read_records(dest)[0]
    assert record["status"] == "error"
    assert record["exit_code"] == 2
    assert "error" not in record
    assert "result" not in record
    assert "stdout" not in record
    assert "stderr" not in record


# -- principal -----------------------------------------------------------


def test_unattributed_principal_says_so(tmp_path):
    log, dest = file_log(tmp_path)
    log.decision(call_id="c", node="n", tool="t", command="x", decision="allow")

    principal = read_records(dest)[0]["principal"]
    # Present and explicitly false — an absent field must never be readable
    # as a trusted caller.
    assert principal["authenticated"] is False


def test_connection_principal_from_scope():
    scope = {
        "client": ("10.0.0.4", 51234),
        "headers": [(b"user-agent", b"mcp-client/1.0"), (b"host", b"node-a")],
    }
    principal = connection_principal(scope)

    assert principal["authenticated"] is False
    assert principal["transport"] == "sse"
    assert principal["peer"] == "10.0.0.4"
    assert principal["peer_port"] == 51234
    assert principal["user_agent"] == "mcp-client/1.0"
    assert principal["connection"]


def test_connection_principal_ignores_forwarded_headers():
    """X-Forwarded-For is caller-supplied; trusting it would let the caller
    choose what the audit log says about them."""
    scope = {
        "client": ("10.0.0.4", 51234),
        "headers": [
            (b"x-forwarded-for", b"1.2.3.4"),
            (b"x-real-ip", b"5.6.7.8"),
        ],
    }
    principal = connection_principal(scope)

    assert principal["peer"] == "10.0.0.4"
    assert "1.2.3.4" not in json.dumps(principal)
    assert "5.6.7.8" not in json.dumps(principal)


def test_connection_principal_survives_a_scope_without_client():
    principal = connection_principal({})
    assert principal["peer"] is None
    assert principal["authenticated"] is False


# -- failure posture -----------------------------------------------------


def unwritable_dest(tmp_path) -> str:
    """A destination whose parent directory does not exist."""
    return str(tmp_path / "no-such-dir" / "audit.jsonl")


def test_fail_open_continues_and_counts(tmp_path):
    log = AuditLog(AuditConfig(destination=unwritable_dest(tmp_path),
                               on_write_failure=CONTINUE))
    log.decision(call_id="c", node="n", tool="t", command="x", decision="allow")
    log.decision(call_id="d", node="n", tool="t", command="y", decision="allow")

    assert log.dropped == 2


def test_fail_closed_raises(tmp_path):
    log = AuditLog(AuditConfig(destination=unwritable_dest(tmp_path),
                               on_write_failure=DENY))
    with pytest.raises(AuditWriteError):
        log.decision(call_id="c", node="n", tool="t", command="x", decision="allow")


def test_drop_count_rides_the_next_successful_record(tmp_path):
    """Loss must be visible in the log itself, not only in a stderr line."""
    dest = str(tmp_path / "sub" / "audit.jsonl")
    log = AuditLog(AuditConfig(destination=dest, on_write_failure=CONTINUE))

    log.decision(call_id="a", node="n", tool="t", command="x", decision="allow")
    log.decision(call_id="b", node="n", tool="t", command="y", decision="allow")
    assert log.dropped == 2

    os.mkdir(os.path.dirname(dest))          # sink recovers
    log.decision(call_id="c", node="n", tool="t", command="z", decision="allow")

    records = read_records(dest)
    assert len(records) == 1
    assert records[0]["call_id"] == "c"
    assert records[0]["audit_dropped"] == 2
    assert log.dropped == 0                  # counter resets after reporting


def test_disabled_log_writes_nothing(tmp_path):
    dest = str(tmp_path / "audit.jsonl")
    log = AuditLog(AuditConfig(enabled=False, destination=dest))
    log.decision(call_id="c", node="n", tool="t", command="x", decision="allow")
    log.startup(node="n", catalog_path="/x", tools=[])

    assert not os.path.exists(dest)


def test_file_is_appended_not_truncated(tmp_path):
    """Records accumulate across writes, and rotation-by-rename is safe
    because the destination is opened per record rather than held open."""
    log, dest = file_log(tmp_path)
    log.decision(call_id="a", node="n", tool="t", command="x", decision="allow")
    log.decision(call_id="b", node="n", tool="t", command="y", decision="allow")

    assert [r["call_id"] for r in read_records(dest)] == ["a", "b"]

    os.rename(dest, dest + ".1")             # logrotate, no SIGHUP handler
    log.decision(call_id="c", node="n", tool="t", command="z", decision="allow")

    assert [r["call_id"] for r in read_records(dest)] == ["c"]
    assert [r["call_id"] for r in read_records(dest + ".1")] == ["a", "b"]


# -- startup record ------------------------------------------------------


def test_startup_record_describes_the_permitted_surface(tmp_path):
    log, dest = file_log(tmp_path, on_write_failure=DENY, max_command_bytes=64)
    log.startup(
        node="node-a",
        catalog_path="/etc/cli-mcp-server/catalog/",
        tools=[{"name": "ps", "binary": "/bin/ps", "healthy": True,
                "allow_rules": 1, "deny_rules": 2}],
    )

    record = read_records(dest)[0]
    assert record["event"] == "startup"
    assert record["node"] == "node-a"
    assert record["catalog_path"] == "/etc/cli-mcp-server/catalog/"
    assert record["tools"][0]["binary"] == "/bin/ps"
    # The failure posture is an invisible property of the deployment unless
    # the log states it.
    assert record["audit"]["on_write_failure"] == DENY
    assert record["audit"]["max_command_bytes"] == 64
