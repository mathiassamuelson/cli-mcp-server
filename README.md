# cli-mcp-server

A configurable [MCP](https://modelcontextprotocol.io/) server that exposes any CLI binary as an MCP tool, with a deny-first / allow-list / default-deny command filter.

Point it at a binary on disk, declare which subcommands are safe, and any MCP-aware client (Claude Desktop, an Anthropic-API agent, etc.) can run those subcommands and read their output — without the agent ever getting near `rm`, `shutdown`, or anything else you didn't explicitly bless.

---

## Why this exists

LLM agents are useful exactly when they can interact with real systems, but giving an agent unrestricted shell access is a non-starter for anything touching production. The usual answers are bad in different ways:

- **Hand-write an MCP tool per CLI.** Works, but you write the same subprocess wrapper, timeout handling, and JSON envelope code for every binary you wrap.
- **Give the agent shell access and hope.** The agent invents commands. Some are destructive. You find out later.
- **Tell the agent what the rules are in the system prompt.** The agent forgets, or ignores them under pressure, or hits a token limit and the rules slide out of context.

This server collapses the wrapper-writing into one daemon and moves the safety rules out of the prompt and into a config file the agent can't influence. The filter runs in the server, not the model. If the agent tries `cache.flush` and your deny list says `*.flush`, the model never gets to see the result of that call — it gets a denial.

---

## How it works

```
┌──────────────┐         MCP / SSE         ┌────────────────────────┐
│  MCP client  │◄────────────────────────► │   cli-mcp-server       │
│  (agent)     │                           │                        │
└──────────────┘                           │  ┌──────────────────┐  │
                                           │  │  Tool catalog    │  │
                                           │  │  (YAML entries)  │  │
                                           │  └────────┬─────────┘  │
                                           │           │            │
                                           │  ┌────────▼─────────┐  │
                                           │  │  Allow/deny      │  │
                                           │  │  filter          │  │
                                           │  └────────┬─────────┘  │
                                           │           │            │
                                           │  ┌────────▼─────────┐  │
                                           │  │  subprocess exec │  │
                                           │  └────────┬─────────┘  │
                                           └───────────┼────────────┘
                                                       ▼
                                                 ┌──────────┐
                                                 │  binary  │
                                                 └──────────┘
```

Each tool you declare in the catalog becomes one MCP tool. The tool takes a single `command` string argument — the agent assembles a command, the server passes it through the allow/deny filter, executes via `asyncio.create_subprocess_exec` (with `shlex.split` so quoted arguments survive), and returns a JSON envelope.

Binary paths are resolved against a configured `search_paths` list, **not** the process `PATH`. This means an agent (or anything else) can't trick the server into running a different binary by manipulating environment variables.

---

## Quick start

### 1. Install

```bash
git clone https://github.com/mathiassamuelson/cli-mcp-server.git
cd cli-mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Requires Python 3.12+.

### 2. Minimal config

Create `~/.config/cli-mcp-server/config.yaml`:

```yaml
server:
  host: "127.0.0.1"
  port: 8100
  node_name: "local"

catalog:
  path: "~/.config/cli-mcp-server/catalog/"
  search_paths:
    - /usr/bin
    - /bin
```

### 3. Declare a tool

Create `~/.config/cli-mcp-server/catalog/ps.yaml`. Each file is a YAML **list** of tool entries — one file can declare several related tools:

```yaml
- name: "ps"
  description: "Read-only process listing. Use for diagnosing what's running."
  binary: "ps"
  timeout_seconds: 10
  rules:
    deny:
      - "* --kill *"
    allow:
      - "aux"
      - "-ef"
      - "-eo *"
```

> The full schema lives in `cli_mcp/catalog.py` (see `_validate_entry` and `ToolEntry`) with a worked example in `configs/example.yaml`. The fields above are the minimum; the next section covers the rest.

### 4. Run

```bash
bin/server.sh
```

Defaults to `0.0.0.0:8100`. Override with `HOST` and `PORT`.

### 5. Verify

```bash
curl http://127.0.0.1:8100/health
# {"status":"ok","node":"local","tools":["ps"]}
```

---

## Configuration

### Server config

YAML file with two sections:

| Section | Purpose |
|---|---|
| `server.host`, `server.port` | Bind address. |
| `server.node_name` | Identifier returned in every tool response. Useful when one client connects to multiple cli-mcp-server instances; the agent can route by node. |
| `catalog.path` | Directory holding tool entry YAML files. |
| `catalog.search_paths` | Where to resolve bare binary names. Never falls back to `$PATH`. |
| `catalog.defaults` | Optional defaults applied to every tool entry (e.g. a default `timeout_seconds`). |
| `audit.*` | Audit log settings. See [Audit logging](#audit-logging). Omitting the section enables auditing to stderr with defaults. |

### Config file lookup

In order:

1. `CLI_MCP_CONFIG` environment variable (full path to YAML)
2. `~/.config/cli-mcp-server/config.yaml`
3. `/etc/cli-mcp-server/config.yaml`

### Catalog entries

Each YAML file in `catalog.path` is a **list** of tool entries. Files are loaded in lexicographic order; subdirectories are ignored. Tool names must be unique across the whole catalog.

Required fields per entry:

- `name` — the MCP tool name the agent sees.
- `description` *or* `description_file` — exactly one. `description` is inline; `description_file` is a path (relative to the catalog directory) to a separate file. The latter is useful when a tool's description is the size of a man page — for example, the system prompt's worth of CLI reference for an LLM-driven tool.
- `binary` — absolute path, or a bare name resolved via `search_paths`.
- `rules` — must define at least one of `deny` or `allow`. An empty `rules` block is rejected at load time (it would default-deny everything, which is almost certainly not what you meant).

Optional fields:

- `timeout_seconds` — per-tool timeout. Defaults from `catalog.defaults.timeout_seconds`, then to `30`.
- `prepend_args` — list of strings prepended to every argv. Useful for binaries that take a fixed leading argument like a subsystem or subcommand. For example, a tool that always invokes `nom-tell statmon ...` declares `binary: nom-tell` and `prepend_args: ["statmon"]`.
- `output.max_bytes` — stdout cap. Defaults from `catalog.defaults.output.max_bytes`, then to `65536`. Output beyond this is truncated and the producer is killed.
- `path_rules.deny` — separate filesystem-path denials applied to the command string. For when you want to allow `cat *` in `rules.allow` but still block `cat /etc/shadow`.
- `pipe_stage` — boolean. Set to `true` if the tool may appear as a non-lead stage in a pipeline (see Pipelines below). Defaults to `false`.

If the configured `binary` doesn't resolve at startup, the entry loads as **unhealthy** rather than failing the server. The tool still appears in the registry but returns an error envelope when called, with the list of paths that were tried. This means a single missing binary can't take down a multi-tool server.

### Allow/deny rules

The filter is **deny-first, then allow-list, then default-deny**:

```
1. If the command matches any pattern in `deny` → reject.
2. Else if the command matches any pattern in `allow` → accept.
3. Else → reject.
```

Patterns are glob-style (Python `fnmatch`):

- `*` matches any sequence of characters, including spaces. `ps *` matches `ps aux`, `ps -eo pid,comm`, etc.
- Matching is **case-insensitive**.
- Patterns are matched against the *full command string* the agent sends — not against argv tokens.

Deny rules take precedence over allow rules, so it's safe to write a broad allow like `kubectl get *` next to a narrow deny like `kubectl get secrets *`. The deny wins.

#### Worked example: locking down `kubectl`

```yaml
- name: "kubectl"
  description: "Read-only Kubernetes inspection."
  binary: "/usr/local/bin/kubectl"
  timeout_seconds: 30
  rules:
    deny:
      - "* delete *"
      - "* apply *"
      - "* patch *"
      - "* edit *"
      - "* exec *"
      - "* port-forward *"
      - "* cp *"
      - "* drain *"
      - "* cordon *"
      - "* uncordon *"
      - "* scale *"
      - "* rollout *"
      - "* get secrets*"
    allow:
      - "get *"
      - "describe *"
      - "logs *"
      - "top *"
      - "version*"
      - "config view*"
```

The agent can list pods, read logs, describe resources — but cannot mutate state and cannot read secrets, even though `get *` would otherwise allow it.

---

## Pipelines

A command string can chain multiple catalog tools with `|`, just like a shell pipeline:

```
aux | grep nginx | head -5
```

This is **not** a shell — there's no `/bin/sh` involved at any point. The server parses the pipeline itself, looks up each stage in the catalog, runs each stage's allow/deny filter independently, then chains the subprocesses together with `create_subprocess_exec` writing each stdout into the next stdin.

Rules:

- The lead stage is the tool the agent invoked. Its args are the text before the first `|`.
- Every subsequent stage starts with a catalog tool name, and that tool **must declare `pipe_stage: true`**. If `grep` isn't a registered pipe stage, the pipeline is denied.
- Each stage's args are still subject to its own `rules.deny` / `rules.allow`. The deny rule on `* /etc/shadow` in the `cat` entry still fires when `cat` is on the right side of a pipe.
- Forbidden metacharacters reject the whole pipeline at parse time: `;`, `&`, `>`, `<`, `` ` ``, `$(`, `&&`, `||`, newlines. Quoted strings (single or double) preserve their content verbatim — `grep "hello | world"` is one stage, not two.

To make a tool usable downstream, declare it as a pipe stage in its catalog entry:

```yaml
- name: "grep"
  description: "Filter lines matching a pattern."
  binary: "grep"
  pipe_stage: true
  rules:
    allow:
      - "*"
```

Without `pipe_stage: true` the agent can still invoke `grep` as a standalone tool but cannot use it as the right-hand side of a pipe.

---

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/mcp` | GET (SSE) | MCP client connection. |
| `/mcp/messages` | POST | MCP message handling (used by the SSE transport, not called directly). |
| `/health` | GET | Liveness check. Returns `node_name` and the list of registered tool names. |

## Tool response format

Every tool returns a JSON envelope:

```json
{
  "node": "local",
  "tool": "ps",
  "command": "aux",
  "status": "success",
  "exit_code": 0,
  "execution_time_ms": 42,
  "result": "USER PID %CPU %MEM ...\n..."
}
```

`status` is one of:

- `success` — binary ran and exited. `exit_code`, `execution_time_ms`, and `result` are populated.
- `denied` — the filter rejected the command. `error` describes which rule fired.
- `error` — execution failed (binary not found, timeout, subprocess crash). `error` has details.

If stdout parses as JSON, the server returns it as a parsed object in `result`; otherwise `result` is the raw text.

Output is capped at the tool's `output.max_bytes` (default 65536). When the cap is hit, the producer is killed, the response includes `"truncated": true`, and a marker line is appended to the captured output. This bounds the worst case where a tool decides to print a million lines.

---

## Running

```bash
export CLI_MCP_CONFIG=/path/to/config.yaml
bin/server.sh
```

`bin/server.sh` activates `.venv` and runs `uvicorn cli_mcp.server:app`. `HOST` and `PORT` env vars override the bind defaults.

The server is designed to run on the host alongside the binaries it wraps — most useful CLI tools have host coupling (config files, Unix sockets, log directories, dynamically linked dependencies) that makes containerizing them more trouble than it's worth. Run it under systemd, supervisord, or whatever your platform's process manager of choice is.

---

## Connecting an MCP client

Any MCP client supporting the SSE transport can connect to `http://<host>:8100/mcp`. The server registers one MCP tool per catalog entry. Tool names are the entry's `name` field; the client sees them prefixed with `node_name` if you've set that up on the client side (most multi-server clients do this so tool names stay unique across hosts).

A minimal Python client using the `mcp` SDK:

```python
import asyncio
from mcp.client.sse import sse_client
from mcp import ClientSession

async def main():
    async with sse_client("http://127.0.0.1:8100/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([t.name for t in tools.tools])
            result = await session.call_tool("ps", {"command": "aux"})
            print(result.content[0].text)

asyncio.run(main())
```

---

## Security notes

- **Network exposure.** Default bind is `0.0.0.0:8100`. There's no auth on the server itself — wrap it behind a VPN, a reverse proxy with mTLS, or bind to localhost. The threat model is "an agent in a controlled network calling tools," not "anyone on the internet calling tools."
- **No shell, ever.** Commands are tokenized with `shlex.split` and executed via `create_subprocess_exec`. Pipelines chain subprocess stdouts into stdins directly — there is no `/bin/sh -c`. `$(rm -rf /)` in a command string is a literal argument, not a substitution.
- **Sanitized subprocess environment.** Every tool runs with a minimal env: `PATH`, `LANG`, `LC_ALL` only. The server's own environment doesn't leak through.
- **`search_paths`, not `$PATH`.** Bare binary names in catalog entries resolve only against the explicit list. An agent cannot influence which binary runs by manipulating environment variables.
- **Default-deny.** The third rule of the filter is "if nothing matched, reject." Forgetting to add an allow rule fails closed. An entry with empty rules is rejected at load time.
- **Pipeline stages need explicit opt-in.** A tool can be invoked directly without `pipe_stage: true`, but it can't appear as a downstream pipeline stage. This stops an agent from chaining a read-only tool into a downstream tool that wasn't designed for untrusted input.
- **Be thoughtful with allow patterns.** This server protects the binary boundary — it can't protect against a permissive `*` in an allow list. `aux` is safer than `*`; `get pods*` is safer than `get *`.

---

## Audit logging

Every tool call is recorded. Records are JSON Lines, written to `stderr` by default (journald picks them up under a systemd unit) or to an absolute path — see the `audit:` block in [`configs/example.yaml`](configs/example.yaml).

### Two records per call

A call emits a **`decision`** record *before* the subprocess is spawned, and an **`outcome`** record after it finishes:

```json
{"ts":"2026-07-24T10:15:02.113Z","event":"decision","call_id":"9f2c…","node":"dns-node-a",
 "tool":"ps","command":"aux | grep -e nginx","decision":"deny",
 "reason":"segment 1 (grep): Command matches deny rule: * -e *",
 "principal":{"authenticated":false,"transport":"sse","connection":"41ab…","peer":"10.0.0.4"}}
```

```json
{"ts":"2026-07-24T10:15:02.119Z","event":"outcome","call_id":"9f2c…","node":"dns-node-a",
 "status":"success","exit_code":0,"execution_time_ms":42}
```

Writing the decision first is deliberate: a call that wedges or takes the process down still leaves proof that it was authorized and started.

A one-time **`startup`** record is written when the catalog loads, listing every tool with its resolved binary and its verbatim allow/deny patterns. It makes the log self-describing — you can answer "what was this node permitting when that call came in" without recovering the config file as it existed then.

### Joining the two — read this before writing queries

The join key is **`(node, call_id)`**, present on both records.

**It is an outer join by design.** A refused call writes a `decision` and *never* an `outcome`. That asymmetry is the point: the absence of an outcome record bearing a given `call_id` is what proves the subprocess never ran. A query that inner-joins, or that filters out unmatched decisions as a data-quality problem, discards precisely the security events you built this for.

### What is deliberately not recorded

- **Subprocess stdout and stderr, ever — including on failure.** Tool output is arbitrary system data and belongs to a different retention and access class than an audit trail. The outcome record says *that* a call failed and its exit code; the failure text goes to the caller only.
- **Proxy headers.** `X-Forwarded-For` and friends are caller-supplied. Trusting them would let a caller choose what the audit log says about them. `peer` is the transport-level address.
- **Commands beyond `max_command_bytes`**, which are truncated and marked with `command_truncated` plus the original `command_bytes`.

### Caller attribution, and its limits

Connections are unauthenticated, so `principal.authenticated` is always `false` today. The field is present from day one so that adding auth is a value change rather than a schema change, and so an absent field can never be read as a trusted caller.

Two limits worth stating plainly rather than discovering later:

- **Attribution is connection-scoped, not request-scoped.** A tool call does not execute in the task of the POST that carried it — the message is handed to the SSE session's stream and the handler runs inside `mcp_server.run()`. Per-connection facts (peer address, the connection's `User-Agent`) are available; per-request ones are not.
- **This log establishes what the server did, not who asked it to.** With unauthenticated connections and the default fail-open posture, it is a strong operational record and *not* a tamper-evident audit trail: anyone who can fill the destination disk can make it lossy. Both limits close the same way — when authentication lands.

### When the sink fails

`on_write_failure` controls it:

| Value | Behaviour |
|---|---|
| `continue` (default) | The call runs. The drop is counted and rides along on the next record that lands, as `audit_dropped`, so loss stays visible in the log itself. One complaint goes to stderr on first failure, then silence — a per-call complaint would fill the remaining disk. |
| `deny` | The call is refused before anything is spawned; the caller gets `status: error`. |

`deny` gates **execution**. Once a subprocess has run, a failed *outcome* write is always counted rather than raised — converting a real result into a refusal would report a falsehood to the caller.

The active posture is included in the `startup` record, so it is not an invisible property of the deployment.

---

## Development

```bash
pip install -e ".[dev]"
pytest                 # run tests (169)
pytest -m "not e2e"    # skip socket-binding end-to-end tests
ruff check .           # lint
```

Linting is [ruff](https://docs.astral.sh/ruff/) in **check-only** mode — it
reports, it never rewrites files, and there is no autoformatter or
format-on-save in this project. The enabled rules are the bug-catching subset
(pyflakes plus logical-error pycodestyle); the reasoning is in the
`[tool.ruff.lint]` block of `pyproject.toml`.

The suite lives in `tests/` and is stratified from unit tests through live
end-to-end tests over the real MCP transport. Because this is a
security-boundary server, the tests optimize for proving the allow/deny filter
holds under adversarial input rather than for coverage percentage.

See **[docs/TESTING.md](docs/TESTING.md)** for the full test strategy: the
layers and what each catches, the no-mocks / wall-clock / absence-of-effect
conventions, how the pytest suite relates to the dependency-free conformance
probes, and a decision guide for where a new test belongs. Contributors and
coding agents adding tests should start there.

---

## Background

This server was extracted from a project that wraps DNS query-log analysis CLIs for an LLM-driven aggregator. The wrapping was generic enough that it didn't deserve to live inside that project — every team building agents that need to drive real CLIs has the same problem. The DNS-flavored ancestor lives at [statmon-ai](https://github.com/mathiassamuelson/statmon-ai) if you want to see a fuller end-to-end deployment using this server.

---

## License

Licensed under Apache 2.0. See [LICENSE](LICENSE) for details.
