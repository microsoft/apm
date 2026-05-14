---
title: Compile your package
description: Roll your instructions primitives into AGENTS.md / CLAUDE.md / GEMINI.md style root context files for every supported harness, without touching dependencies.
---

`apm compile` reads your **instructions** primitives from `.apm/`
(plus any unpacked under `apm_modules/`) and writes the per-harness
root context files each agent harness reads at startup. It does not
fetch packages, does not resolve dependencies, does not write the
lockfile, and does not deploy other primitive types.

:::note[When you actually need it]
Compile is **optional for the `copilot` target** -- GitHub Copilot
natively reads `.github/instructions/*.instructions.md` (with their
`applyTo:` frontmatter) that `apm install` already deploys, so the
aggregated `AGENTS.md` / `copilot-instructions.md` it produces are a
nice-to-have, not a requirement.

Compile is **recommended for every other target** (`claude`,
`cursor`, `codex`, `gemini`, `opencode`, `windsurf`) -- those
harnesses load instructions through the root context file
(`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) or a harness-specific rules
folder that compile generates. Without it, your instructions are
on disk but the harness will not pick them up.
:::

```bash
apm compile
```

Concretely, that command rolls your `instructions/*.instructions.md`
(see [Instructions](./author-primitives/instructions-and-agents/#1-instructions))
into the native rules surface each target expects:

- `AGENTS.md` -- the cross-harness root context file (Copilot, Codex,
  OpenCode, Windsurf all read this).
- `CLAUDE.md` -- Claude Code's root context file.
- `GEMINI.md` -- Gemini CLI's root context file.
- per-harness rules trees that mirror each instruction's
  `applyTo:` glob: `.github/instructions/`, `.claude/rules/`,
  `.cursor/rules/*.mdc`, `.windsurf/rules/`.

Other primitive types -- prompts, skills, agents, chatmodes, hooks,
commands -- are NOT compiled by this command. They are deployed by
`apm install` directly into the harness directories that consume them
(`.github/prompts/`, `.agents/skills/`, `.claude/commands/`, etc.).
For the full reach map, see
[Primitives and targets](../../concepts/primitives-and-targets/). For
the place compile takes in the broader flow, see
[Lifecycle](../../concepts/lifecycle/).

## The authoring loop

```
edit .apm/instructions/  ->  apm compile  ->  inspect AGENTS.md  ->  repeat
```

You will run this loop while writing or refining instructions. Three
flags speed it up:

```bash
apm compile --watch              # re-run on every change
apm compile --validate           # check frontmatter and structure; emit nothing
apm compile --dry-run            # print placement decisions without writing files
```

`--validate` is the fastest signal that an instruction parses.
`--dry-run` shows you exactly which AGENTS.md tree would be written
where. `--watch` is the tight inner loop while you edit prose.

To preview a script that wraps a `.prompt.md` file, use
[`apm preview`](../preview-and-validate/) instead. `apm compile` builds
the root context files; `apm preview` shows the rewritten command line
your script will execute.

## Pick a target

By default `apm compile` detects targets from your workspace (see
[detection cascade](#detection-cascade) below). Override it with
`--target` (`-t`):

```bash
apm compile --target claude
apm compile --target copilot,cursor          # comma-separated
apm compile --all                            # every canonical target
```

Accepted values: `copilot`, `claude`, `cursor`, `opencode`, `codex`,
`gemini`, `windsurf`, `agent-skills`, `all`. The `agent-skills` slug
is a no-op for compile (skills are deployed by `apm install`); it is
accepted in target lists for symmetry only. Unknown slugs are
rejected before any work runs.

## Detection cascade

When you omit `--target`, APM resolves which targets to build in this
order:

1. Explicit `--target <slug>` flag.
2. The `targets:` field in your `apm.yml`.
3. Auto-detect: any harness root directory (`.github/`, `.claude/`,
   `.cursor/`, `.codex/`, `.gemini/`, `.opencode/`, `.windsurf/`) that
   already exists.
4. Fallback: `minimal` -- writes a single `AGENTS.md` and skips per-
   harness rules folders.

Pin `targets:` in `apm.yml` if you want the same compile output on
every machine. Full rules and the per-target output map live in
[Primitives and targets](../../concepts/primitives-and-targets/#how-a-target-is-selected).

## Where instructions land

Per target, with the rules shape on disk after compile:

| Target | Root context file | Per-rule output | Compile required? |
|---|---|---|---|
| `copilot` | `AGENTS.md` | `.github/instructions/<name>.instructions.md` (preserves `applyTo`) | No -- Copilot reads the per-rule files natively |
| `claude` | `CLAUDE.md` | `.claude/rules/<name>.md` | Yes -- `CLAUDE.md` is the entry point |
| `cursor` | -- | `.cursor/rules/<name>.mdc` | Yes -- `.mdc` is Cursor's rules format |
| `codex` | `AGENTS.md` (folded) | none -- compile-only, no per-file deploy | Yes -- folded into `AGENTS.md` |
| `gemini` | `GEMINI.md` (folded) | none -- compile-only, no per-file deploy | Yes -- folded into `GEMINI.md` |
| `opencode` | `AGENTS.md` (folded) | none -- compile-only, no per-file deploy | Yes -- folded into `AGENTS.md` |
| `windsurf` | -- | `.windsurf/rules/<name>.md` | Yes -- compiled to Windsurf rules |

## compile vs install

| You want to... | Run |
|---|---|
| Iterate on instructions in `.apm/instructions/` | `apm compile` |
| Deploy prompts, skills, agents, hooks, commands, MCP | `apm install` (see [Install packages](../../consumer/install-packages/)) |
| Add a dependency or refresh `apm_modules/` | `apm install` |
| Verify deployed bytes match the lockfile | `apm audit` |

`apm install` runs compile internally as part of its integrate phase,
so a normal `apm install` on a clean checkout already produces
correct AGENTS.md / CLAUDE.md / GEMINI.md output. Reach for
`apm compile` directly when you are iterating on instructions and
do not want install's side effects.

## Pitfalls

- **Confusing compile's scope.** Compile only handles **instructions**
  (and optionally a single chatmode to prepend). If you edit a prompt,
  skill, agent, hook, or command, `apm compile` will not redeploy it
  -- run `apm install` for that.
- **Forgetting `--target` on a clean workspace.** With no harness
  folder present and no `targets:` in `apm.yml`, the cascade falls
  back to `minimal` and writes only `AGENTS.md`. The CLI prints a
  hint, but the easy fix is to either create the harness folder or
  pin `targets:` in your manifest.
- **Stale `AGENTS.md` after deleting an instruction.** Compile leaves
  previous output in place by default. Pass `--clean` to remove
  orphaned files generated by earlier runs.
- **Hand-edited primitives skip the security scan.** `apm compile`
  does not run the install-time hidden-Unicode scan. After hand-edits,
  run `apm audit` before publishing. See
  [drift and secure-by-default](../../consumer/drift-and-secure-by-default/).
- **Zero-output success.** If compile reports success but writes no
  files, your project either has no instructions, or every requested
  target was rejected. The CLI surfaces this as a warning -- check
  `targets:` and the contents of `.apm/instructions/`.

Once your instructions compile cleanly into the harnesses you care
about, package the result with [`apm pack`](../pack-a-bundle/) and
share it via [a marketplace](../publish-to-a-marketplace/).
