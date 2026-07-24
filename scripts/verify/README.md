# Conformance probes

```bash
python scripts/verify/conformance.py            # all checks
python scripts/verify/conformance.py --since 0.2.0
python scripts/verify/conformance.py -v         # detail on passing checks too
```

Exit status is 0 only if every check passes.

If your fork keeps `cli_mcp` somewhere other than the repo root, point
`PYTHONPATH` at it:

```bash
PYTHONPATH=bin/cli-mcp-server/lib python scripts/verify/conformance.py
```

## What these are for

These probe **behavior**, not implementation, and they depend on nothing but
the standard library and an importable `cli_mcp` — no pytest, no dev extras,
no build system, no CI.

That constraint is the whole point. A private fork adapted to another build
system can run this unchanged and get a direct answer to *does my copy still
behave like upstream?* — a question the test suite cannot answer for them,
because their test framework, dependency pinning, and layout have all drifted.

For a fork absorbing a release, run this **before** porting anything. It tells
you which upstream findings actually apply to your copy. Some may already be
fixed differently downstream, in which case the fix should be adapted rather
than copied.

## Adding a check

Every behavioral change ships one. Register it with the release that
introduced it:

```python
@check("short-name", "0.3.0", "one line: the property being asserted")
def _short_name() -> Result:
    ...
    return Result(True, "what was observed")
```

Checks accumulate rather than being replaced. A fork that is behind upstream
will fail the checks for releases it has not absorbed — that is the intended
signal, not a defect.

Keep them dependency-free. A check that needs pytest belongs in `tests/`.
