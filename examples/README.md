# Examples

Sample catalog entries you can drop into `cli-mcp-server` to wrap real binaries.

## Layout

```
examples/
└── catalog/         # tool entries — one or more YAML files per tool
```

To use these, copy the files into your configured `catalog.path` (typically `/etc/cli-mcp-server/catalog/` or `~/.config/cli-mcp-server/catalog/`), or point `catalog.path` at this directory directly while you're trying them out:

```yaml
# in your config.yaml
catalog:
  path: "/path/to/cli-mcp-server/examples/catalog"
```

Each file is independent. You can take one, take a few, or use them as templates and write your own. Tool names must be unique across the whole catalog, so if you copy two files that both define a tool called `grep`, the loader will reject the second one — pick one.

## What's here

All current examples are **read-only and have no side effects.** They expose tools that observe state (`ps`, `grep`, file readers, etc.) without changing it. The allow/deny rules in each file are tight enough that an agent can't trick the tool into mutation by clever argument construction — `cat` denies writing, `ps` denies signaling, and so on. They're safe to wire up to an agent without a consent layer.

## Future structure

When examples for tools that *do* mutate state arrive — `git push`, `kubectl apply`, `terraform apply`, anything that creates, deletes, or modifies — they'll need different treatment. Specifically, they belong behind a client-side review-and-approve flow, where the agent proposes a command and a human confirms before the server executes it. That's an MCP client concern, not something cli-mcp-server itself enforces, but the catalog entry is the right place to *signal* that requirement.

The plan for that:

- A subdirectory split. `examples/catalog/read-only/` and `examples/catalog/gated/` (or similar — the exact names will get settled when the first gated example is written and we see what distinction actually matters).
- Convention will be documented here when the split happens.

For now, **flag the category at the top of each file** with a comment:

```yaml
# category: read-only
- name: "ps"
  ...
```

This makes the eventual split a `grep`-and-`mv` operation rather than a re-read of every file.

## Writing your own

The full catalog entry schema is documented in the [main README](../README.md#catalog-entries) and enforced by `cli_mcp/catalog.py`. The short version: each file is a YAML list of tool entries, each entry needs a `name`, a `description` (or `description_file`), a `binary`, and `rules` with at least one `allow` or `deny` pattern.

Some patterns worth borrowing from the examples here:

- **Tight `allow` patterns.** `aux` and `-ef` are safer than `*`. The narrower the allow, the less surface area for an agent to surprise you.
- **Defensive `deny` patterns even on read-only tools.** `ps` is read-only, but a permissive `*` allow could let an agent run `ps --kill ...` on a system where that flag exists. A `* --kill *` deny costs nothing.
- **`pipe_stage: true` only when needed.** Tools that are useful as pipeline filters (`grep`, `head`, `tail`, `sort`) declare this. Tools that should only ever be invoked directly leave it off.
- **Per-tool `timeout_seconds`.** Override the default for tools where the default is wrong in either direction. A tool that grovels through gigabytes of logs needs longer; a tool that should always be instant needs shorter.
