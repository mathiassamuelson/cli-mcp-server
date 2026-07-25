# Proposal: running privileged commands from an unprivileged server

**Status: proposal. Nothing here is implemented.** No behaviour changes ship
with this document.

The requirement: some catalog tools need root, and the MCP server must not run
as root. Companion to [authn-authz.md](authn-authz.md) — that one is about
*who is calling*, this one is about *what the server itself is allowed to
become*. They compose: §9 covers why privileged tools are exactly the ones
that most need per-principal policy.

---

## 1. The requirement, and the thing that is not negotiable

If the server runs as root, every one of its defences is a speed bump rather
than a boundary. A filter bypass, a quoting disagreement between
`pipeline.py` and `shlex.split`, a path-normalization miss — each becomes root
code execution instead of an unprivileged annoyance. The whole value of the
project is that the blast radius of a filter bug is small.

So: **the server process runs unprivileged, and a narrow, enumerable set of
operations crosses into root.** Everything below is about how narrow, how
enumerable, and where the boundary is drawn.

One property is worth stating up front because it constrains every option
(§7): **an unprivileged process cannot signal a root process.** `kill(2)`
requires the sender's real or effective UID to match the target's real or
saved set-UID. The server runs as `cli-mcp`; a sudo'd child runs as root. The
executor's timeout and byte-cap kills — which this repo treats as hard
guarantees and asserts on wall-clock elapsed time — **do not work across the
privilege boundary.** That is not a detail to discover in production.

---

## 2. First question: does it need root at all?

Worth asking per operation, because the cheapest privileged operation is the
one that was eliminated.

- **Read-only state.** A root-owned systemd timer writes a snapshot to a
  world-readable file; the MCP tool reads the file. No escalation, no
  boundary, and the tool becomes trivially safe. This covers a surprising
  share of "show me X" diagnostics — routing tables, interface state, service
  status dumps.
- **Group membership.** Many "root" needs are really "read this
  root-owned file/socket." Adding `cli-mcp` to a group, or an ACL on one path,
  removes the whole problem. Far narrower than root.
- **A single Linux capability.** `ping` needs `CAP_NET_RAW`, not root. Reading
  arbitrary files needs `CAP_DAC_READ_SEARCH`. See option E.

Recommendation: the design should require each privileged catalog entry to
**record why it cannot be done unprivileged**, as a required field. It costs
one line per entry and it is the only mechanism that reliably stops the
privileged set from growing by default.

---

## 3. Options

### A. `sudo` with per-operation wrapper scripts

Server runs as `cli-mcp`. A sudoers rule grants NOPASSWD execution of specific
**wrapper** programs — not of general-purpose binaries with argument patterns.

```
Cmnd_Alias CLI_MCP_OPS = /usr/local/libexec/cli-mcp/restart-resolver, \
                         /usr/local/libexec/cli-mcp/dump-routes

cli-mcp ALL=(root) NOPASSWD: CLI_MCP_OPS
Defaults!CLI_MCP_OPS env_reset, secure_path="/usr/sbin:/usr/bin:/sbin:/bin"
```

The executor prepends `sudo -n --` for entries marked `privileged: true`.

**Why wrappers and not argument patterns.** sudoers wildcard matching is the
classic footgun in this space: `*` matches across whitespace, so
`/usr/bin/systemctl restart *` grants far more than "restart one unit," and
reasoning about what a pattern admits is genuinely hard. A wrapper collapses
that: it accepts a small fixed vocabulary, validates it in one reviewable
place, and the sudoers entry contains no wildcards at all. This gets most of
option D's "closed vocabulary" benefit without a daemon.

**`NOEXEC` caveat.** `NOEXEC:` prevents the sudo'd program from exec'ing
anything else, which is attractive — but a shell-script wrapper *must* exec
the real binary, so `NOEXEC` breaks it. Either omit `NOEXEC` (and accept that
the wrapper's own correctness is load-bearing) or make wrappers compiled
binaries. I would omit it and keep wrappers short enough to read.

### B. `sudo` with argument patterns, no wrappers

Listed only to reject it. It moves the security-critical vocabulary into
sudoers glob syntax, which is the weakest matcher in the stack and the one
furthest from the tests. If the filter and sudoers disagree about what an
argument pattern admits, that disagreement is a privilege escalation — the
same class of bug as the `pipeline.py` / `shlex.split` divergence that
`CLAUDE.md` already flags as the thing most likely to bite.

### C. setuid-root helper binary

A small purpose-built setuid binary implementing a fixed operation set.

Removes the sudo dependency, but setuid programs are hard to write correctly:
environment, file descriptors, signal dispositions, `argv[0]`, resource
limits, umask, and `LD_*` handling all have to be right. It also cannot be
Python — the kernel ignores the setuid bit on interpreted scripts, so this
means shipping C into a Python project that downstream forks port by hand.
**Not recommended** unless nothing else fits.

### D. Privileged helper daemon over a Unix socket

A small root daemon exposes a **closed, named** set of operations over a Unix
domain socket. The unprivileged server connects and requests an operation by
name with typed arguments; the daemon validates, runs it, and streams results
back. `SO_PEERCRED` verifies the caller's uid/gid.

This is textbook privilege separation and it is architecturally the strongest
option:

- The root side **never receives an argv**. It receives an operation name and
  typed parameters, which is a categorically smaller attack surface than "a
  command string we validated."
- **It solves §1's kill problem properly.** The daemon is root, owns the
  child, and can therefore actually kill it. Timeout enforcement moves to the
  side that has the authority to enforce it.
- No sudo and no setuid in the trusted computing base.
- The root side is small enough to audit and test independently, and can keep
  its own log at the privilege boundary.

Costs: a second daemon to package, supervise, and version across 12–20 nodes;
an IPC protocol to design and keep compatible; and some duplication of catalog
concepts on the root side.

### E. Linux capabilities

File capabilities on the target binary (`setcap cap_net_raw+ep /usr/bin/ping`)
give the narrowest privilege of any option.

Two sharp edges. Setting **ambient** capabilities on the server process
(systemd `AmbientCapabilities=`) is worse than it looks: ambient capabilities
are inherited by *every* child, so the privilege applies to all tools rather
than the privileged ones — the opposite of what we want. And file
capabilities on a shared binary grant it to **everyone** who runs that binary,
not just us; the right form is a capability on a private wrapper.

Genuinely the best answer where the need maps to one capability. Linux-only,
which is fine for the target deployment but not for the project generally.

### F. systemd transient units (`systemd-run`) or D-Bus

Delegates the privilege drop, logging, and resource limits to systemd. But it
moves the security policy into polkit rules — another policy language, another
place to get it wrong — and couples the server to systemd. Reasonable in a
systemd-only shop; more moving parts than A for the same result.

---

## 4. Comparison

| | Privileged surface | Kill guarantee (§1) | New deps | Ops familiarity | Effort |
|---|---|---|---|---|---|
| **A** sudo + wrappers | Fixed vocabulary, in wrappers | **Broken** — needs in-wrapper timeout | sudo in TCB | High | Low |
| **B** sudo + arg patterns | sudoers globs | Broken | sudo in TCB | High | Low |
| **C** setuid helper | Fixed, in C | Broken | C toolchain | Low | High |
| **D** helper daemon | Named operations, no argv | **Works** — root side kills | none | Medium | Medium-high |
| **E** capabilities | One capability | N/A — no root child | none | Medium | Low per op |
| **F** systemd-run | polkit policy | Partial | polkit | Medium | Medium |

---

## 5. Recommendation

**Eliminate first (§2), then capabilities (E) where the need is one
capability, then `sudo` + wrappers (A) as the general mechanism** — with the
in-wrapper timeout of §7.1 as a hard requirement rather than a
recommendation.

A is the right default because it is incremental, ops-standard, keeps
`cli_mcp/` almost unchanged, and puts the privileged vocabulary in small
reviewable wrapper scripts. Its one serious weakness — the broken kill
guarantee — has a solid fix, and that fix is testable.

**Choose D instead if the privileged operation set is small and stable.** It
is the better boundary, and it is the only option that restores the kill
guarantee without relying on every wrapper author to remember a `timeout`
call. The tipping point is roughly: if there are fewer than about a dozen
privileged operations and they change rarely, the daemon pays for itself; if
the set is broad or grows with each catalog addition, wrappers scale better.

I would not build C. I would not use B at all.

---

## 6. What changes in this codebase

Deliberately small. The catalog gains a field; the executor gains an argv
prefix; the audit log gains two facts.

```yaml
- name: restart-resolver
  description: Restart the resolver on this node.
  binary: /usr/local/libexec/cli-mcp/restart-resolver   # absolute, a wrapper
  privileged: true
  privilege_reason: "systemctl requires root; wrapper restarts one fixed unit"
  timeout_seconds: 30          # MUST exceed the wrapper's own timeout
  rules:
    allow: ["--zone *"]        # bare "*" is rejected at load time when privileged
```

### Load-time validation for `privileged: true`

The catalog already rejects empty `rules` at load time on the grounds that it
would default-deny everything. The same instinct applies harder here — these
are cheap, testable, and each closes a real hole:

1. **`binary` must be an absolute path.** Which program receives root must not
   depend on `search_paths` ordering.
2. **The wrapper must not be writable by the server's own user.** If `cli-mcp`
   can write the file it invokes via sudo, the entire scheme is defeated —
   the server can rewrite what it runs as root. `stat` at load time, refuse
   to register otherwise. This one is worth its weight alone.
3. **`allow` must not contain a bare `*`.** A privileged tool with a
   catch-all allow list is a root shell with extra steps.
4. **`pipe_stage` must be false.** See §7.2.
5. **`privilege_reason` is required** (§2), and appears in the startup record.

### Executor

`build_argv` — already public since the audit work — becomes:

```python
[sudo_path, "-n", "--", entry.binary, *entry.prepend_args, *shlex.split(args)]
```

A useful consequence: because the audit log records the argv that
`build_argv` produces, **the escalation itself shows up in the audit log**
rather than being invisible. That is the correct thing for the record to say.

### Audit

`decision` records gain `privileged: true` and the target uid; the startup
record lists which entries are privileged along with their reasons and the
resolved wrapper paths. A privileged denial is the single most valuable line
in the log and should be trivially greppable.

Note also that sudo writes its own syslog trail. That is a **second,
independent record of every escalation**, written by a process the MCP server
cannot influence — genuinely useful for detecting tampering with our own log,
and worth documenting as a correlation source rather than treating our audit
log as the only evidence.

---

## 7. Traps specific to this codebase

### 7.1 The kill guarantee does not cross the privilege boundary

The one that matters most, restating §1 concretely.

`cli_executor._signal_kill` calls `proc.kill()`. For a privileged tool that
process is root, and the server is not. The call raises `PermissionError`
(currently caught only for `ProcessLookupError`, so **this would propagate
today**), and the subprocess keeps running. Every guarantee in the executor's
own docstring — "timeout actually kills the subprocess", "the producer is
killed on cap" — silently stops holding.

Consequences beyond the timeout itself:

- `run_pipeline`'s cap path kills all stages then drains; an unkillable root
  producer blocks instead, and `_reap`'s bound expires leaving an **orphaned
  root process** on a long-running server. That is a resource leak with
  privilege attached.
- `tests/test_executor_hardening.py` asserts `elapsed < 3.0` to prove a kill
  happened. That assertion cannot be written the same way for privileged
  tools, so the existing test strategy needs a deliberate answer here rather
  than a gap.

**Required mitigation, whichever option is chosen:** the privileged side
enforces its own timeout. With option A that means every wrapper is, at
minimum:

```sh
#!/bin/sh
exec timeout --kill-after=5s 25 /usr/bin/systemctl restart resolver.service
```

`timeout` runs as root, so it *can* signal its own root child. The catalog's
`timeout_seconds` then becomes a backstop that should only ever fire if the
wrapper's own timeout failed — hence the "must exceed" rule in §6. Validating
that relationship is not possible from the config alone (the wrapper's value
is inside the script), which is an argument for generating wrappers from a
template rather than hand-writing them.

With option D this is free: the daemon owns the child and can kill it.

Separately, `_signal_kill` should catch `PermissionError` regardless, so a
failed kill degrades to a logged abandonment instead of an unhandled
exception.

### 7.2 Privileged tools must not be pipeline stages

The pipeline feature chains one stage's stdout into the next stage's stdin. If
a privileged tool can appear in **non-lead** position, then unprivileged
output is being fed to a root process's stdin — a direct injection channel
into the privileged side, and one our filter never inspects because it filters
arguments, not stream contents.

`privileged: true` and `pipe_stage: true` must be mutually exclusive, rejected
at load time. A privileged tool in *lead* position piping into unprivileged
stages (`priv-tool | grep foo`) is fine and useful — root output flowing
downward is not the dangerous direction.

### 7.3 `NoNewPrivileges` silently breaks sudo

The obvious systemd hardening for this unit is exactly what stops it working:

- `NoNewPrivileges=yes` — **disables setuid entirely**, so sudo fails. Same
  for options C and E.
- `RestrictSUIDSGID=yes` — same effect.
- `PrivateUsers=yes`, `ProtectSystem=strict`, `PrivateDevices=yes` — each can
  break individual privileged operations depending on what they touch.

This is worth an explicit note in the deployment docs, because the failure is
confusing: the unit looks hardened, and the privileged tools just stop.
Option D sidesteps it neatly — the *server* unit can be maximally hardened
including `NoNewPrivileges=yes`, because it never escalates; only the small
helper unit runs privileged.

That is a genuine point in D's favour that is easy to miss.

### 7.4 Environment sanitization overlaps, and that is fine

`SAFE_ENV` already restricts children to `PATH`, `LANG`, `LC_ALL`. sudo's
`env_reset` plus `secure_path` will override `PATH` again. Belt and braces —
no conflict, but the wrapper should not assume the executor's `PATH` survived.

### 7.5 `path_rules` matter far more here

A privileged tool that takes a path argument is the highest-value target in
the catalog. The `check_paths` normalization work from 0.2.0 is what stands
between `--file=/etc/shadow` and a root read. Privileged entries should be
reviewed on the assumption that any path they accept will be attacked, and
`path_rules.deny` should be treated as mandatory rather than optional for
them.

---

## 8. Spikes

1. **Confirm the kill failure on the target platform.** Run a sudo'd sleep
   under the real executor, trigger the timeout, and observe whether the
   process survives and what `proc.kill()` raises. Decides the exact shape of
   the `_signal_kill` fix and gives the regression test something to pin.
   This is the highest-value spike; do it first.
2. **Confirm the in-wrapper `timeout` mitigation works end to end**, including
   that the executor's read loop terminates promptly when the wrapper's
   timeout fires rather than waiting on the backstop.
3. **Check whether sudo's `use_pty` default** (on by default in recent sudo)
   changes process-tree shape enough to affect reaping or stdout EOF. It
   should not change the kill conclusion — the child is root either way — but
   it may affect when pipes close.
4. **Enumerate the actual privileged operations.** The choice between A and D
   (§5) turns on this count and its rate of change, and nobody should pick
   the architecture before the list exists.

---

## 9. Interaction with authn/authz

Privileged tools are the strongest argument for the policy layer proposed in
[authn-authz.md §10](authn-authz.md). "Any authenticated caller may invoke any
tool" is tolerable for read-only diagnostics and is not tolerable when some of
those tools restart services as root.

Concretely, the two proposals should land in this order: **policy scoping
before the first privileged tool ships.** Otherwise there is a window where
every caller that can reach the socket can invoke root operations, which is a
strictly worse position than today.

A privileged tool should also be denied by default in any policy that does not
name it explicitly — i.e. `allow: ["*"]` in a *policy* entry should not confer
privileged tools. That mirrors the load-time rule in §6 and keeps "grant
everything" from quietly meaning "grant root."

---

## 10. Open questions

1. **What are the actual operations?** (Spike 4.) Everything downstream of
   this — A versus D, how many wrappers, whether capabilities suffice —
   depends on the list.
2. **How many are genuinely irreducible** after applying §2? My expectation is
   that a meaningful share become file reads or group memberships.
3. **Who owns the wrappers?** They are security-critical code that lives
   outside this repo (in `/usr/local/libexec`), so they fall outside the
   conformance-probe and fork-porting story entirely. That gap is worth an
   explicit decision: ship them in `configs/` as templates, ship them as a
   separate package, or accept that they are deployment-local.
4. **Does any privileged operation need to be interactive or long-running?**
   Both interact badly with the timeout design and would push toward D.
5. **Is `sudo` acceptable in the TCB at all** for Akamai, given its CVE
   history? If not, that decides for D or E without further discussion.

---

## 11. Suggested phasing

| Phase | Contents |
|---|---|
| 1 | `_signal_kill` catches `PermissionError`; regression test pinning the abandonment path. Independent of everything else and worth doing regardless. |
| 2 | Catalog `privileged` + `privilege_reason` fields with all five load-time validations (§6). No execution change yet — entries validate and are refused, which is safe to ship. |
| 3 | Executor sudo prefix, audit fields, wrapper template + reference sudoers in `configs/`, deployment note covering §7.3. |
| 4 | Policy scoping from the authn proposal — **must precede any privileged tool reaching a live catalog** (§9). |
| 5 | Re-evaluate D against the real operation list; migrate if the count stayed small. |

Phase 1 is a bug fix that stands alone. Phase 2 ships pure validation with no
new capability, which is the safest possible way to introduce a
security-relevant field.

Usual obligations apply: a conformance probe per behavioural change, impact
tags in `CHANGELOG.md`, and a migration note. Phases 2–4 are all
`[security-boundary]`.
