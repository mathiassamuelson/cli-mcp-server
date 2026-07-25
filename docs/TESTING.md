# Test strategy

For contributors and coding agents working on cli-mcp-server. It explains not
just *what* is tested but *why the suite is shaped this way*, so a change lands
in the right layer with the right kind of assertion.

If you are here to port this to a downstream fork, read
[DOWNSTREAM.md](DOWNSTREAM.md) first — it covers applying these tests to a
repo with a different layout.

## What this suite optimizes for

This is a **security-boundary server**: its whole reason to exist is that the
allow/deny filter runs in the server, not in the model's context. So the tests
optimize for one thing above coverage percentage — *proving the boundary holds
under adversarial input*. A test that pushes the line coverage up but never
feeds the filter a bypass attempt is close to worthless here.

Three principles follow from that, and they are non-negotiable:

1. **No mocks.** Every test drives real code paths. Subprocess tests spawn real
   processes from stub scripts; catalog tests load real YAML from real
   directories; protocol tests speak real JSON-RPC. A mock of
   `create_subprocess_exec` would test our belief about the subprocess API, not
   the API — and the subtle bugs this suite has caught all lived in that gap.

2. **Resource guarantees are asserted on wall-clock time, not status.** When a
   test claims "the timeout kills the subprocess," it asserts
   `elapsed < 3.0` — not just that the result says `"timed out"`. A killed
   process and an abandoned one return the same status; only the clock tells
   them apart. Same for the output-cap kill.

3. **Security tests assert absence of effect.** A denial test should prove the
   subprocess never ran, not merely that the envelope says `"denied"`. The
   strongest form is a stub that writes a sentinel file; the test asserts the
   sentinel does not exist.

## The layers

The suite is deliberately stratified. Each layer catches a class of bug the
others cannot, and each is progressively more expensive, so most assertions
live in the cheap layers and only genuinely end-to-end behavior climbs to the
top.

| Layer | Files | What only this layer catches |
| --- | --- | --- |
| **Unit** | `test_filter.py`, `test_pipeline.py`, `test_binary_resolution.py` | Filter and parser logic: glob semantics, deny-beats-allow, path normalization, quote scanning, `$PATH` omission. Fast, table-driven, no I/O. |
| **Subprocess integration** | `test_executor_hardening.py`, `test_pipeline_executor.py`, `test_cli_executor.py` | The hard runtime guarantees: kill-on-timeout, output-cap kill, sanitized env, pipe wiring. Spawns real processes from `tmp_path` stubs. |
| **In-process wiring** | `test_server_catalog.py`, `test_examples_catalog.py` | Catalog → registry → dispatch, with real YAML on disk. Calls `call_tool()` as a function. |
| **E2E protocol** | `test_e2e_protocol.py` | The MCP decorator layer, input-schema advertisement, JSON-RPC serialization, and result coercion — driven through a real `ClientSession`. Transport-agnostic. |
| **E2E transport** | `test_e2e_transport.py` | ASGI routing, the SSE handshake, `/health` — the `bin/server.sh` path under a live uvicorn server on a real socket. |

Current inventory (120 tests):

| File | Tests | Focus |
| --- | --- | --- |
| `test_filter.py` | 37 | glob match, allow/deny precedence, `check_paths` incl. traversal + flag-attached bypass regressions |
| `test_pipeline.py` | 25 | segment scanning, forbidden metacharacters, resolution, quoted-name rejection |
| `test_examples_catalog.py` | 13 | YAML loader, validation errors, defaults inheritance, unhealthy entries |
| `test_cli_executor.py` | 9 | single-tool execution via the `run_cli` back-compat shim |
| `test_e2e_protocol.py` | 8 | in-memory MCP round trips, denial-is-in-band contract |
| `test_binary_resolution.py` | 7 | `_resolve_binary` search-path walk, `$PATH` never consulted |
| `test_executor_hardening.py` | 7 | timeout kill, output cap, sanitized env, prepend_args |
| `test_pipeline_executor.py` | 6 | multi-stage chains, timeout, truncation-without-hang, warnings |
| `test_server_catalog.py` | 5 | catalog-driven list/dispatch/denied/unhealthy |
| `test_e2e_transport.py` | 3 | live SSE handshake, tool call, `/health` |

## Why E2E earns its cost

The E2E layer is the newest and the most expensive, and it justified itself
immediately: `test_pipeline_through_protocol` failed on its first run and
surfaced a bug — argument-less invocations were universally denied — that
neither the unit tests nor a careful read of the code had found. It only
appeared when the full chain ran: `resolve_pipeline` stripped a tool name to
`""`, and `check_command` rejected the empty string, and nothing below the
protocol layer exercised both together.

The two E2E files are split on purpose:

- `test_e2e_protocol.py` is **transport-agnostic**. It uses
  `mcp.shared.memory.create_connected_server_and_client_session`, so it keeps
  working if the server ever moves off SSE.
- `test_e2e_transport.py` is **SSE-specific**. It pins a three-way coupling —
  `SseServerTransport("/messages/")`, the `/mcp` mount's ASGI `root_path`, and
  the `/mcp/messages` mount ordering — that would break at runtime with nothing
  failing in a lower layer. A fork on a different transport rewrites this file
  and keeps the other.

## Two test harnesses, not one: pytest and conformance probes

There is a second, deliberately separate test surface:
[`scripts/verify/conformance.py`](../scripts/verify/README.md).

|  | pytest suite (`tests/`) | Conformance probes (`scripts/verify/`) |
| --- | --- | --- |
| Audience | this repo's CI and contributors | downstream forks on any build system |
| Dependencies | pytest, pytest-asyncio, httpx | standard library + importable `cli_mcp` only |
| Question answered | "is every behavior correct and covered?" | "does my copy still behave like upstream?" |
| Granularity | many small assertions | one probe per shipped behavioral guarantee |

They are not redundant. The pytest suite is exhaustive but unportable — a fork
with different dependency pinning and layout cannot run it as-is. The probes
are coarse but run anywhere, which is what lets a fork check drift without
adopting our test stack. **Every behavioral change adds both**: a pytest test
for the detail, and a probe for the guarantee.

## Adding a test — a decision guide

- **Filter or parser logic** (a new deny form, a grammar rule) → unit test in
  `test_filter.py` or `test_pipeline.py`. Feed it the *bypass* attempts, not
  just the happy path.
- **A runtime guarantee** (timeout, cap, env, fd handling) → subprocess test in
  `test_executor_hardening.py` or `test_pipeline_executor.py`. **Assert on
  elapsed time** if it involves killing anything.
- **Dispatch, schema, or protocol behavior** → `test_e2e_protocol.py`.
- **Routing, transport, `/health`** → `test_e2e_transport.py` (mark `e2e`).
- **A shipped behavioral guarantee** that a fork must not silently lose → also
  add a probe to `scripts/verify/conformance.py`, tagged with the release.

When fixing a bug, the regression test goes in the **lowest layer that would
have caught it**. If none would have, that itself is the finding — it means a
layer is missing an assertion, not just a case.

## Notes for coding agents

You likely arrive with no memory of how this suite was built. Three things that
will otherwise cost you time — the full list and rationale is in
[CLAUDE.md](../CLAUDE.md) and the [v0.2.0 migration note](migrations/v0.2.0.md):

- **The MCP in-memory session cannot be a pytest yield fixture.** anyio raises
  `RuntimeError: Attempted to exit cancel scope in a different task` on
  teardown. Enter it with an `@asynccontextmanager` inside each test — see
  `mcp_session()` in `test_e2e_protocol.py`.
- **`filter.py` and `pipeline.py` disagree slightly about quoting**, because
  `pipeline.py` hand-rolls a scanner and `cli_executor.py` re-tokenizes with
  `shlex`. A test that assumes they agree is testing a fiction; a *bypass* that
  exploits the disagreement is a real finding.
- **A failing test after a change is ambiguous** across fork boundaries — real
  defect, or porting error? The [migration note](migrations/v0.2.0.md) maps
  each finding to the test that pins it; use it before "fixing" anything.

Do not chase the coverage number for its own sake. 88% of lines is covered
today, but the value is in *which* lines: the untested remainder is mostly
error and resource-exhaustion branches, and a new test there is worth more than
one that nudges an already-covered happy path.

## Running

```bash
pip install -e ".[dev]"
pytest -q                          # full suite (120 tests)
pytest -q -m "not e2e"             # skip socket-binding tests
pytest -q --cov=cli_mcp --cov-report=term-missing
python scripts/verify/conformance.py   # behavioral probes (also in CI)
```

`asyncio_mode = "auto"` (in `pyproject.toml`) means `async def test_*` needs no
decorator. CI runs the full suite plus the probes on Python 3.12–3.14.

## Known thin spots

Honest inventory, so nobody mistakes green for complete:

- **`test_cli_executor.py` exercises the `run_cli` back-compat shim**, not the
  primary `run_tool`/`run_pipeline` entry points (those are covered elsewhere).
  It inflates the count against a deprecated wrapper; a future cleanup should
  port it onto `run_tool` and retire the shim.
- **Denial tests mostly assert the envelope, not absence of effect.** Principle
  3 above is the target state, not yet the universal practice — the
  sentinel-file pattern is the upgrade path.
- **No property-based / differential testing** of the two quoting
  implementations. A Hypothesis test asserting `pipeline._scan_segments` and
  `shlex` agree on arbitrary input would be the highest-leverage addition,
  given that their disagreement is a bypass primitive.
- **Config precedence** (`CLI_MCP_CONFIG` → `~/.config` → `/etc`) in
  `load_config` is documented but untested.
- **Audit writes are synchronous, and nothing tests what happens when the
  sink blocks.** A hung filesystem (NFS, a full disk mid-write) stalls the
  event loop for every in-flight call, not just the one being logged. The
  trade was taken deliberately — ordered records, no loss window, no drain
  task, at a call volume that is human- or agent-paced rather than high-QPS —
  but the failure mode is untested because provoking it portably is hard. If
  this moves to a bounded queue, the test to write first is the one asserting
  what happens when the queue fills.
- **Audit records are asserted per call, not under concurrency.** The suite
  checks that interleaved records stay separable by `call_id`
  (`test_audit.py`), but nothing drives simultaneous calls through a live
  server and checks the log afterwards.
