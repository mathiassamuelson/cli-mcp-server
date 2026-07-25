# Changelog

Notable changes to cli-mcp-server. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries carry **impact tags** for the benefit of private downstream forks
porting changes by hand. See [docs/DOWNSTREAM.md](docs/DOWNSTREAM.md).

| Tag | Meaning |
| --- | --- |
| `[security-boundary]` | Changes what the allow/deny filter permits. Review before absorbing. |
| `[behavior-change]` | Existing catalogs or callers may observe different results. |
| `[api-change]` | Signature or contract change in `cli_mcp`. |
| `[build-coupled]` | Re-derive against your own build system; do not port verbatim. |

## [Unreleased]

### Added

- **Audit logging of every tool call.** The filter's decisions were previously
  unrecorded: the only evidence a call happened was the response envelope,
  held entirely by the party you would want to audit. Each call now emits a
  `decision` record *before* the subprocess is spawned and an `outcome` record
  after, as JSON Lines, joined on `(node, call_id)`. A refused call emits a
  decision and never an outcome — absence of an outcome record is what proves
  the subprocess did not run, and consumers must treat the join as an outer
  join. A one-time `startup` record lists every tool with its resolved binary
  and verbatim allow/deny patterns, so the log is self-describing. New
  `cli_mcp/audit.py`; new `audit:` config block (defaults to stderr, so an
  existing config keeps working with no edit). Subprocess stdout/stderr is
  never recorded, not even on error. See "Audit logging" in `README.md`.
  `[behavior-change]` `[build-coupled]`
- `cli_executor.build_argv` is now public (was `_build_argv`). The audit log
  records the resolved argv, and sharing one function with the executor is
  what stops what is logged from drifting from what is spawned. `[api-change]`

#### Known limitations, stated rather than discovered

- Caller attribution is **connection-scoped, not request-scoped**. A tool call
  does not run in the task of the POST that carried it, so per-request facts
  are not reachable without patching the transport.
- Connections remain unauthenticated, so `principal.authenticated` is always
  `false`. The field ships now so that adding auth is a value change rather
  than a schema change.
- With the default `on_write_failure: continue`, the log is a strong
  operational record but **not tamper-evident**: filling the destination disk
  makes it lossy. Drops are counted and surface as `audit_dropped` on the next
  record that lands.
- Writes are synchronous, so a hung filesystem stalls the event loop. See
  "Known thin spots" in `docs/TESTING.md`.

- Linting via [ruff](https://docs.astral.sh/ruff/), added to the `dev` extra
  and CI. Configured as a linter only — check-only, no `ruff format`, no
  format-on-save — with a deliberately tight rule set (pyflakes + logical
  pycodestyle) that catches bugs without the diff churn a formatter or import
  reordering would inflict on downstream patch porting. Removed five unused
  imports it found. Replaces the `black`/`flake8` the README mentioned but the
  project never actually configured. `[build-coupled]`
- `docs/TESTING.md` — the test strategy for contributors and coding agents:
  the layered structure and what each layer catches, the no-mocks /
  wall-clock / absence-of-effect conventions, how the pytest suite relates to
  the conformance probes, a decision guide for placing a new test, and an
  honest inventory of thin spots. Referenced from `README.md` and `CLAUDE.md`.
- `docs/proposals/authn-authz.md` — design proposal for authentication,
  authorization, and transport security. Nothing implemented; no behaviour
  change. Compares an NGINX front end against building it into the server,
  recommends splitting by concern (proxy terminates mTLS, the server owns
  principal extraction and policy), and specifies an mTLS backend with
  certificate-derived principals for closed deployments alongside a
  spec-conformant OAuth 2.1 Resource Server path. Records what the MCP
  `2026-07-28` revision changes for it.
- `docs/proposals/privileged-execution.md` — design proposal for running
  root-requiring commands from an unprivileged server. Nothing implemented; no
  behaviour change. Compares sudo-with-wrappers, a setuid helper, a privileged
  helper daemon, and Linux capabilities; recommends eliminating the need
  first, then capabilities, then sudo with per-operation wrappers. Records the
  finding that the executor's timeout and byte-cap kill guarantees do not
  cross the privilege boundary — an unprivileged process cannot signal a root
  child, and `_signal_kill` catches only `ProcessLookupError` today.
- `docs/DOWNSTREAM.md` gains an "Applying upstream patches" recipe for forks
  that are unrelated-history imports with a relocated package root — the shape
  the known forks actually have. Validated against a scratch repo reproducing
  it, from 5/5 conformance probes failing to 5/5 passing.

### Fixed

- Argument-less tool invocation works through `call_tool`. The 0.2.0 fix that
  let `check_command` accept an empty command was unreachable: `parse_pipeline`
  rejected `""` as an empty segment one layer above the filter, so no tool
  could be invoked bare — `uptime`, `free`, `dmesg` — whatever its allow
  rules. An empty segment is now legal when it is the *sole* segment; `|`,
  `ps |`, `| head` and `ps | | head` all still produce two or more segments
  and remain rejected. A fork holding 0.2.0 has this bug and its `empty-args`
  probe passes anyway, which is why the new `empty-args-reach-the-filter`
  probe tests through `parse_pipeline` instead of `check_command`.
  `[security-boundary]` `[behavior-change]`

### Changed

- CI uses `actions/checkout@v5` and `actions/setup-python@v6`. The v4/v5
  versions target Node.js 20, which GitHub has deprecated and is force-running
  on Node 24; runs succeed today but break when that fallback is removed.
  `[build-coupled]`

## [0.2.0] - 2026-07-23

First release with downstream porting support:
[migration note](docs/migrations/v0.2.0.md),
[conformance probes](scripts/verify/).

### Fixed

- `check_paths` no longer bypassable by respelling a denied path. `//etc/shadow`,
  `/etc/./shadow`, `/etc/../etc/shadow`, `--file=/etc/shadow`, and
  `-f/etc/shadow` all defeated the deny list; the shipped catalog exposes
  `cat`, `grep`, `head`, and `tail` with `allow: ["*"]`, so the `--file=` form
  was reachable in a default deployment. Normalization is purely lexical —
  symlinks are deliberately not resolved.
  `[security-boundary]` `[behavior-change]`
- Tools can be invoked with no arguments. `check_command` rejected the empty
  string before matching any pattern, so `uptime`, `free`, and `dmesg` could
  not be called bare despite shipping with `allow: ["*"]`, and no
  argument-less pipe stage (`| wc`, `| sort`, `| uniq`, `| tac`, `| nl`) was
  usable. Empty now matches against the patterns like any other command.
  `[behavior-change]`
- `run_pipeline` no longer hangs indefinitely when output hits the byte cap.
  It reaped the killed stages before draining the last stage's stdout, and
  asyncio holds `Process.wait()` open until every pipe disconnects — so a
  truncated pipeline blocked forever, past its own timeout. A truncated
  pipeline now returns `status: "success"` with `truncated: true`, matching
  `run_tool`, where it previously reported an error or never returned.
  `[behavior-change]`
- Quoted tool names in pipe segments are rejected instead of silently
  corrupting arguments. `"grep" nginx` resolved to args `p" nginx`, which was
  then both filtered and executed — the filter was deciding on text that
  differed from the command line. `[behavior-change]`

### Added

- End-to-end test coverage. An in-memory MCP client session exercises the real
  protocol (transport-agnostic), and a live SSE suite drives the
  `bin/server.sh` path under uvicorn, pinning the three-way coupling between
  `SseServerTransport`, the `/mcp` mount's ASGI `root_path`, and the
  `/mcp/messages` mount ordering. `handle_sse`, `handle_messages`, and
  `/health` previously had no coverage at all.
- `scripts/verify/conformance.py` — dependency-free behavioral probes a fork
  can run without pytest or a build system, to see how far it has drifted.
- `docs/DOWNSTREAM.md` and `docs/migrations/` — the porting contract and
  per-release migration notes.
- `CLAUDE.md` — repo-specific guidance for coding agents.
- Dev dependencies declared as a `dev` extra, pytest configuration, and CI on
  Python 3.12–3.14. The suite previously could not be run from a clean
  checkout without guessing its requirements. `[build-coupled]`

### Changed

- Dropped the `[server]` extra from the `mcp` dependency. That extra does not
  exist; pip warned and silently installed plain `mcp`. `[build-coupled]`

## [0.1.0] - 2026-07-23

Baseline tag for the pre-existing state, so downstream forks have an anchor to
record their sync point against. No code changes.

[Unreleased]: https://github.com/mathiassamuelson/cli-mcp-server/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/mathiassamuelson/cli-mcp-server/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mathiassamuelson/cli-mcp-server/releases/tag/v0.1.0
