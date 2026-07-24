# Downstream forks

This project expects to be cloned into private repositories and adapted to
other build systems. Upstream cannot see those forks, cannot test against
them, and cannot coordinate their release timing. This document is the
contract that makes porting changes cheap anyway.

Two things upstream controls, and this is how each is handled:

1. **How legible a change is** — can you tell what it means for *your* copy
   without asking? → impact tags, migration notes, conformance probes.
2. **How small the adaptation surface is** — how much must be re-derived
   rather than copied? → the coupled-file list below, kept short on purpose.

## What ports, and what does not

| Copy across unchanged | Re-derive for your build system |
| --- | --- |
| `cli_mcp/` — all runtime code | `pyproject.toml` |
| `tests/` — all test bodies and fixtures | `bin/server.sh` |
| `scripts/verify/` — conformance probes | `configs/` |
| `docs/`, `CHANGELOG.md` | `CLAUDE.md` (its "Running tests" section) |

Runtime code never reads build-system state — `load_config` takes an env var
and two fixed paths, nothing setuptools-aware — so `cli_mcp/` has no reason to
diverge. Tests build everything under `tmp_path` rather than repo-relative
paths, so they do not care about your layout.

If a change would add a fifth file to the right-hand column, upstream flags it
with `[build-coupled]` in the changelog and says so in the migration note.

One exception worth knowing: `tests/test_e2e_transport.py` is SSE-specific. A
fork that has moved to streamable HTTP rewrites it against
`mcp.client.streamable_http`. `tests/test_e2e_protocol.py` is
transport-agnostic and carries across unchanged.

## Absorbing a release

1. **Run the conformance probes against your fork, before porting anything.**

   ```bash
   python scripts/verify/conformance.py --since <your-last-synced-version>
   ```

   This needs no pytest, no dev extras, and no build system — just an
   importable `cli_mcp`. It tells you which upstream findings actually apply
   to your copy. A finding you already fixed differently should be adapted,
   not overwritten.

2. **Read the migration note** for the release in `docs/migrations/`. It
   records the order to apply changes, the traps encountered while making
   them, and what to check in your own catalog.

3. **Scan the changelog for impact tags.** `[security-boundary]` and
   `[behavior-change]` entries are the ones that can break a working catalog;
   `[build-coupled]` entries you rewrite rather than port.

4. **Port.** Commits are kept small, single-purpose, and independently green,
   so individual changes can be applied in isolation. See
   [Applying upstream patches](#applying-upstream-patches) — cherry-picking by
   SHA does not work for the forks that exist today, but applying upstream's
   diffs does.

5. **Re-run the conformance probes.** All checks should pass.

6. **Record your sync point** — see below.

## Applying upstream patches

The known forks are **unrelated-history imports**: the code was copied into a
new repo with its own root commit, often with the package relocated (e.g.
`cli_mcp/` living under `bin/<something>/lib/`). Two consequences.

**There is no common ancestor**, so `git cherry-pick <upstream-sha>` has
nothing to rebase against. Confirm your own situation with:

```bash
git remote add upstream https://github.com/mathiassamuelson/cli-mcp-server.git
git fetch upstream --tags
git merge-base HEAD upstream/main     # no output + non-zero: unrelated
```

**Add the remote anyway.** Even with no shared history, having upstream's
objects locally lets you read the real diffs instead of reconstructing changes
from prose:

```bash
git diff upstream/v0.1.0..upstream/v0.2.0 -- cli_mcp/
git show upstream/main:cli_mcp/filter.py
```

### Recipe for a relocated package root

Split each commit by pathspec, because `--directory` prepends to *every* path
in the patch — applying a whole commit that touches both `cli_mcp/` and
`tests/` would bury the tests inside the package directory:

```bash
# source changes -> relocated package root
git format-patch -1 <sha> --stdout -- cli_mcp \
  | git apply --directory=bin/cli-mcp-server/lib -p1

# test changes -> wherever your tests actually live
git format-patch -1 <sha> --stdout -- tests | git apply -p1
```

**`git apply` is atomic.** If any file in the patch is missing downstream, the
*entire* patch is silently discarded — including the hunks that would have
applied. This bites when a commit also touches a test file your fork does not
have yet. Exclude it explicitly:

```bash
git format-patch -1 <sha> --stdout -- tests \
  | git apply -p1 --exclude=tests/test_e2e_protocol.py
```

Use `--3way` for fallback merging when context has drifted, and `--reject` if
you would rather get `.rej` files than an all-or-nothing failure.

### Running the probes with a relocated package

`scripts/verify/conformance.py` expects `cli_mcp` to be importable from the
repo root. If yours lives elsewhere, point `PYTHONPATH` at it:

```bash
PYTHONPATH=bin/cli-mcp-server/lib python scripts/verify/conformance.py
```

This whole recipe was validated against a scratch repo reproducing exactly
that shape — unrelated history, relocated package root — taking it from 5/5
probes failing to 5/5 passing.

## Recording your sync point

Upstream tags every release. Without a recorded sync point there is no way to
say which changes a fork has absorbed, and the changelog cannot be diffed
against your state.

The convention is a `.upstream-sync` file at your fork's root:

```
upstream: https://github.com/mathiassamuelson/cli-mcp-server
version: v0.2.0
date: 2026-07-23
notes: test_e2e_transport.py adapted for internal transport
```

Upstream cannot verify this. It is worth doing anyway — it is the first thing
anyone porting the next release will look for, including an agent doing the
port on your behalf.

## For agents doing the port

If you are an LLM agent reading this in a downstream fork, you have no context
from the work that produced the release. Two things follow:

- **Run the conformance probes first.** Do not assume upstream's findings
  apply to this fork — verify. The probes exist precisely because that
  question cannot be answered by reading a diff.
- **A failing test after porting is ambiguous.** It means either a real defect
  in this fork or a porting error. The migration note says which test pins
  which finding; use it to tell the two apart before changing anything.

## Conventions upstream follows

So you can rely on them:

- Every behavioral change ships a conformance probe in `scripts/verify/`.
- Every release gets a changelog entry; entries that affect porting carry
  impact tags.
- Releases with anything non-obvious to port get a migration note, written
  *during* the work rather than reconstructed from the diff afterwards.
- Commits are single-purpose and independently green.
