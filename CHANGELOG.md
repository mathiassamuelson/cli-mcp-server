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

### Fixed

- `mcp` is pinned `>=1,<2`. It was unpinned, and mcp 2.0.0 removed the
  low-level `Server.list_tools()` / `call_tool()` decorators and the SSE
  transport `cli_mcp/server.py` is built on, so any fresh install — including
  every CI run, which installs unpinned into a clean environment — resolved to
  2.x and died at import with `AttributeError: 'Server' object has no
  attribute 'list_tools'`, before reading any config or catalog. Existing
  deployments with a populated venv were unaffected until reinstalled, which
  is why this surfaced at install time rather than as a failing test. Support
  for 2.x is a port of the server's transport layer, not a version bump.
  `[build-coupled]`

### Added

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
- `docs/DOWNSTREAM.md` gains an "Applying upstream patches" recipe for forks
  that are unrelated-history imports with a relocated package root — the shape
  the known forks actually have. Validated against a scratch repo reproducing
  it, from 5/5 conformance probes failing to 5/5 passing.

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
