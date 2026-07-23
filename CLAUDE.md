# cli-mcp-server

An MCP server that exposes configured CLI binaries as MCP tools, gated by a
deny-first / allow-list / default-deny filter. The filter is the product — it
runs in the server, not in the model's context.

## Running tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                    # full suite
pytest -q -m "not e2e"       # skip tests that bind a socket
```

`asyncio_mode = "auto"` is set in `pyproject.toml`, so `async def test_*`
functions need no `@pytest.mark.asyncio` decorator.

## This repo has private downstream forks

Changes here get ported by hand into private clones that have been adapted to
other build systems. Two consequences for how you work:

- **Keep tests independent of repo layout.** Build fixtures under `tmp_path`.
  A test that hardcodes a repo-relative path breaks in every fork.
- **Build-system coupling is confined to `pyproject.toml`, `bin/server.sh`,
  `configs/`, and this file.** Those four are re-derived downstream; everything
  else should copy across unchanged. Don't add a fifth without flagging it.

## Test conventions

- **No mocks.** Tests spawn real subprocesses from stub scripts written to
  `tmp_path`, and load real YAML from real directories.
- **Kill semantics are asserted on wall-clock elapsed time**, not just on the
  returned status. `assert elapsed < 3.0` is what proves the subprocess was
  actually killed rather than merely abandoned.
- **Security-boundary tests assert absence of effect.** A denial test should
  prove the subprocess never ran, not just that the envelope says `denied`.

## Release conventions

- Every behavioral change ships a probe in `scripts/verify/conformance.py`,
  registered against the release that introduced it. Keep it dependency-free —
  a check that needs pytest belongs in `tests/`.
- Every change gets a `CHANGELOG.md` entry under `Unreleased`. Tag entries that
  affect downstream porting: `[security-boundary]`, `[behavior-change]`,
  `[api-change]`, `[build-coupled]`.
- Releases with anything non-obvious to port get `docs/migrations/vX.Y.Z.md`,
  written **during** the work. Reconstructing it from the diff afterwards
  loses the traps, which are the part worth recording.
- Keep commits single-purpose and independently green so they can be
  cherry-picked and bisected.

## Things that are easy to get wrong

- `filter.py` and `pipeline.py` are the security boundary. `pipeline.py`
  hand-rolls a quote scanner and `cli_executor.py` re-tokenizes with
  `shlex.split` — if those two disagree about what is quoted, that is a filter
  bypass, not a cosmetic bug.
- Binary resolution deliberately ignores `$PATH` (`catalog.py:_resolve_binary`).
  Never "fix" it to consult the environment.
- Wrapping the MCP in-memory client session in a pytest yield fixture trips
  anyio's cancel-scope check on teardown. Enter it with an
  `@asynccontextmanager` inside each test instead.
