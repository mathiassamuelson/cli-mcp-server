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
