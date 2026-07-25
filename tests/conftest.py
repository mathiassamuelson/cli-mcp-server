"""Shared fixtures for the end-to-end MCP suites.

Everything is built under `tmp_path` so these fixtures carry across forks
with different repo layouts — see CLAUDE.md.
"""

import json
import socket
import stat
import textwrap
import threading
import time

import httpx
import pytest
import uvicorn

from cli_mcp import server as srv


@pytest.fixture(autouse=True)
def _isolate_audit_sink(monkeypatch):
    """Keep the audit sink from leaking between tests.

    `AUDIT` is a lazily-built global keyed off `CONFIG`. Without this, a test
    that configures a file destination leaves the next test writing into a
    torn-down tmp_path, which is order-dependent and silent.
    """
    monkeypatch.setattr(srv, "AUDIT", None)
    yield
    monkeypatch.setattr(srv, "AUDIT", None)


@pytest.fixture
def e2e_config(tmp_path, monkeypatch):
    """Stub binaries + catalog, with the server module pointed at them."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    fake = bin_dir / "fakebin"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:]}))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    upper = bin_dir / "upperbin"
    upper.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write(sys.stdin.read().upper())\n"
    )
    upper.chmod(upper.stat().st_mode | stat.S_IEXEC)

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "e2e.yaml").write_text(textwrap.dedent("""\
        - name: fake
          description: Fake tool for e2e tests.
          binary: fakebin
          prepend_args: ["sub"]
          timeout_seconds: 5
          rules:
            deny: ["destroy*"]
            allow: ["ok*"]
        - name: upper
          description: Uppercase stdin.
          binary: upperbin
          pipe_stage: true
          timeout_seconds: 5
          rules:
            allow: ["*"]
        - name: missing
          description: Tool whose binary is absent on this node.
          binary: not-installed
          rules:
            allow: ["*"]
    """))

    # Audit goes to a real file under tmp_path rather than the default stderr,
    # so tests read back what a deployment would actually have written.
    audit_path = tmp_path / "audit.jsonl"

    cfg = {
        "server": {"host": "127.0.0.1", "port": 0, "node_name": "e2e-node"},
        "catalog": {"path": str(catalog), "search_paths": [str(bin_dir)]},
        "audit": {"destination": str(audit_path)},
    }
    monkeypatch.setattr(srv, "CONFIG", cfg)
    monkeypatch.setattr(srv, "REGISTRY", None)
    monkeypatch.setattr(srv, "AUDIT", None)
    cfg["_audit_path"] = audit_path
    yield cfg
    monkeypatch.setattr(srv, "REGISTRY", None)
    monkeypatch.setattr(srv, "AUDIT", None)


@pytest.fixture
def audit_records(e2e_config):
    """Read back the audit log written by the server under test."""
    path = e2e_config["_audit_path"]

    def read(event: str | None = None) -> list[dict]:
        if not path.exists():
            return []
        records = [
            json.loads(line)
            for line in path.read_text().splitlines()
            if line.strip()
        ]
        return [r for r in records if event is None or r["event"] == event]

    return read


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_server(e2e_config):
    """Run the real Starlette app under uvicorn — the bin/server.sh path.

    Yields the base URL. Binds an ephemeral port so parallel runs don't
    collide.
    """
    port = _free_port()
    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    ready = False
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base}/health", timeout=0.5)
            ready = True
            break
        except httpx.HTTPError:
            time.sleep(0.05)

    if not ready:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("live server did not become ready within 10s")

    yield base

    server.should_exit = True
    thread.join(timeout=5)
