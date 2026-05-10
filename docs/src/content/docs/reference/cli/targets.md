---
title: apm targets
description: Show and inspect resolved deployment targets for the current project
sidebar:
  order: 15
---

Show which agent harnesses APM will deploy to from the current project,
and why.

## Synopsis

```bash
apm targets [OPTIONS]
```

## Description

`apm targets` prints the canonical target table for the current
directory: every supported harness, whether APM considers it active,
the filesystem signal that activated it, and where compiled output
will land.

Resolution order matches `apm compile`:

1. `--target` / `--all` on the command line (not applicable to
   `apm targets` itself, but reflected by `compile` and `install`).
2. `targets:` field in `apm.yml`.
3. Auto-detection via filesystem signals (see [Detection signals](#detection-signals)).

Use this command before `apm compile` or `apm install` to confirm
what auto-detection resolves to. If APM lists a target you do not
intend (for example, `CLAUDE.md` is project documentation, not a
Claude Code config), pin `targets:` explicitly in `apm.yml`.

## Subcommands

`apm targets` is a Click command group, but no subcommands ship today.
Invoking `apm targets` without arguments prints the resolved-target
table.

:::note[Planned]
Subcommands such as `apm targets add` are reserved on the group for
future use. They are not implemented yet.
:::

## Options

| Flag | Description |
|------|-------------|
| `--json` | Emit machine-readable JSON instead of the table. One object per canonical target with `target`, `status`, `source`, `deploy_dir`, `needs`. |
| `--all` | In `--json` mode, include the `agent-skills` meta-target row (excluded by default). No effect on table output. |

The `agent-skills` meta-target is a multi-harness fan-out for shared
`.agents/skills/` output. It is not a harness and is excluded from the
default table.

## Examples

Show the resolved target table:

```bash
apm targets
```

Sample output in a project with `CLAUDE.md` and `.cursor/`:

```
  TARGET       STATUS     SOURCE                                   DEPLOY DIR
  ------------ ---------- ---------------------------------------- ----------
  claude       active     CLAUDE.md                                .claude/
  copilot      inactive   needs .github/copilot-instructions.md    .github/
  cursor       active     .cursor/                                 .cursor/
  codex        inactive   needs .codex/                            .codex/
  gemini       inactive   needs GEMINI.md                          .gemini/
  opencode     inactive   needs .opencode/                         .opencode/
  windsurf     inactive   needs .windsurf/                         .windsurf/
```

Machine-readable form:

```bash
apm targets --json
apm targets --json --all
```

## Detection signals

Auto-detection walks the project root for these markers. The first
match per target is enough to activate it.

| Target | Signal(s) APM looks for | Deploy directory |
|--------|-------------------------|------------------|
| `claude` | `.claude/` directory, or `CLAUDE.md` file | `.claude/` |
| `copilot` | `.github/copilot-instructions.md` file | `.github/` |
| `cursor` | `.cursor/` directory, or `.cursorrules` file (legacy) | `.cursor/` |
| `codex` | `.codex/` directory | `.codex/` |
| `gemini` | `.gemini/` directory, or `GEMINI.md` file | `.gemini/` |
| `opencode` | `.opencode/` directory | `.opencode/` |
| `windsurf` | `.windsurf/` directory | `.windsurf/` |
| `agent-skills` | Meta-target; never auto-detected. Opt in via `targets:` in `apm.yml` or `--target agent-skills` on `apm compile`. | `.agents/skills/` |

Notes:

- Detection is filesystem-only. APM does not inspect file contents to
  decide whether a marker is "real".
- A `CLAUDE.md` written as documentation will still activate the
  `claude` target. Pin `targets:` in `apm.yml` to override.
- If no signals are found and `apm.yml` declares no `targets:`,
  `apm targets` prints the full table with every row inactive and an
  info hint to create a harness config or declare `targets:`
  explicitly.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Table or JSON printed (including the all-inactive case). |

## Related

- [`apm install`](../install/) -- uses the same target resolution to decide which harness configs to wire.
- [`apm compile`](../compile/) -- compiles primitives for the targets shown here.
- [Targets matrix](../../targets-matrix/) -- per-target output layout and feature support.
- [Concepts: primitives and targets](../../../concepts/primitives-and-targets/)
