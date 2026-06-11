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
[Primitives and targets](../concepts/primitives-and-targets/). For
the place compile takes in the broader flow, see
[Lifecycle](../concepts/lifecycle/).

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
`--dry-run` shows you exactly which root-context tree (`AGENTS.md`,
`CLAUDE.md`, ...) would be written where. `--watch` is the tight inner
loop while you edit prose.

To preview a script that wraps a `.prompt.md` file, use
[`apm preview`](./preview-and-validate/) instead. `apm compile` builds
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

Experimental targets (`hermes`, `openclaw`, `copilot-cowork`,
`copilot-app`) are also accepted once their flag is enabled via
`apm experimental enable <flag>`, but are excluded from `--all`.
`apm compile -t hermes` emits `AGENTS.md` (the `hermes` target shares
the `agents` compile family). See
[Hermes Agent](../integrations/hermes/).

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
[Primitives and targets](../concepts/primitives-and-targets/#how-a-target-is-selected).

## Where instructions land

Per target, with the rules shape on disk after compile:

| Target | Root context file | Per-rule output | Compile required? |
|---|---|---|---|
| `copilot` | `AGENTS.md` | `.github/instructions/<name>.instructions.md` (preserves `applyTo`) | No -- Copilot reads the per-rule files natively; deduplicates with `.github/instructions/` (see [below](#copilot-deduplication)) |
| `claude` | `CLAUDE.md` | `.claude/rules/<name>.md` | Yes -- deduplicates with `.claude/rules/` (see [below](#claude-code-deduplication)) |
| `cursor` | -- | `.cursor/rules/<name>.mdc` | Yes -- `.mdc` is Cursor's rules format |
| `codex` | `AGENTS.md` (folded) | none -- compile-only, no per-file deploy | Yes -- folded into `AGENTS.md` |
| `gemini` | `GEMINI.md` (folded) | none -- compile-only, no per-file deploy | Yes -- folded into `GEMINI.md` |
| `opencode` | `AGENTS.md` (folded) | none -- compile-only, no per-file deploy | Yes -- folded into `AGENTS.md` |
| `windsurf` | -- | `.windsurf/rules/<name>.md` | Yes -- compiled to Windsurf rules |

## compile vs install

| You want to... | Run |
|---|---|
| Iterate on instructions in `.apm/instructions/` | `apm compile` |
| Deploy prompts, skills, agents, hooks, commands, MCP | `apm install` (see [Install packages](../consumer/install-packages/)) |
| Add a dependency or refresh `apm_modules/` | `apm install` |
| Verify deployed bytes match the lockfile | `apm audit` |

`apm install` runs compile internally as part of its integrate phase,
so a normal `apm install` on a clean checkout already produces
correct AGENTS.md / CLAUDE.md / GEMINI.md output. Reach for
`apm compile` directly when you are iterating on instructions and
do not want install's side effects.

:::note[Copilot deduplication]
<a id="copilot-deduplication"></a>
When `.github/instructions/` is already populated with `.instructions.md` files
(deployed by `apm install --target copilot`), `apm compile --target copilot`
automatically omits the instructions section from `AGENTS.md` to avoid
duplicate context in Copilot's context window. `AGENTS.md` is still generated
when it carries a constitution or dependency `@import` paths. If
`.github/instructions/` is later cleared, re-running `apm compile` restores
the instructions section to `AGENTS.md`.

This deduplication is **target-aware**: it only activates when the sole
AGENTS.md consumer is Copilot. When compiling for targets that do not read
`.github/instructions/` (Codex, OpenCode, Windsurf, etc.), instructions
are always included in `AGENTS.md` regardless of whether
`.github/instructions/` exists. To opt out of deduplication even for
Copilot-only compiles, pass `--no-dedup` (alias: `--force-instructions`):

```bash
apm compile --target copilot --no-dedup
```
:::

:::note[Claude Code deduplication]
<a id="claude-code-deduplication"></a>
When `.claude/rules/` is already populated with instructions,
`apm compile --target claude` automatically omits the instructions
section from `CLAUDE.md` to avoid duplicate content in Claude Code's
context window. The directory can be populated by either
`apm install --target claude` or by an earlier `apm compile --target claude`
run -- both write per-file instruction rules into `.claude/rules/`.
`CLAUDE.md` is still generated when it carries a constitution or
dependency `@import` paths. If `.claude/rules/` is later removed,
re-running `apm compile` restores the instructions section to
`CLAUDE.md`.

To opt out of the deduplication and always include the instructions
section in `CLAUDE.md` (for debugging or when you intentionally want
both copies), pass `--no-dedup` (alias: `--force-instructions`):

```bash
apm compile --target claude --no-dedup
```

This flag affects both the Claude and Copilot deduplication paths (see
[Copilot deduplication](#copilot-deduplication)).
:::

## Managed-section mode

By default `apm compile` overwrites `AGENTS.md` entirely. If your team
keeps hand-written content in `AGENTS.md` alongside APM-managed rules,
use **managed-section mode** to update only the APM-owned block while
leaving everything else untouched.

For the full `apm.yml` key reference for `compilation.agents_md`, see
[the `compilation.agents_md` section in the manifest schema](../reference/manifest-schema/#62-compilationagents_md).

**1. Add markers to `AGENTS.md`:**

```md
<!-- apm:start -->
<!-- apm will insert content here -->
<!-- apm:end -->
```

**2. Enable the mode in `apm.yml`:**

```yaml
compilation:
  agents_md:
    mode: managed_section
    start_marker: "<!-- apm:start -->"
    end_marker: "<!-- apm:end -->"
```

The default markers are `<!-- apm:start -->` and `<!-- apm:end -->`, so
you can omit `start_marker` and `end_marker` if you use those verbatim.

**Constraints:**
- The target file must already exist: if it does not, APM raises a clear
  error ("does not exist yet") instead of a confusing "markers not found".
  Use `mode: full` for the first run to create the file, then switch to
  `managed_section`.
- Both markers must be present in the file exactly once (missing or
  duplicate markers raise a loud error so no content is silently lost).
- The start marker must appear before the end marker; reversed order raises a loud error.
- `start_marker` and `end_marker` must be distinct non-empty strings.
- Content outside the markers is preserved verbatim across every compile
  run; only the block between the markers is replaced.

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
  orphaned files generated by earlier runs. When compiling for the
  `claude` target and `.claude/rules/` is already populated, `--clean`
  also removes a stale APM-generated `CLAUDE.md`. Hand-authored
  `CLAUDE.md` files (those without the `<!-- Generated by APM CLI -->`
  marker) are never deleted.
- **Hand-edited primitives skip the security scan.** `apm compile`
  does not run the install-time hidden-Unicode scan. After hand-edits,
  run `apm audit` before publishing. See
  [drift and secure-by-default](../consumer/drift-and-secure-by-default/).
- **Zero-output success.** If compile reports success but writes no
  files, your project either has no instructions, or every requested
  target was rejected. The CLI surfaces this as a warning -- check
  `targets:` and the contents of `.apm/instructions/`.

Once your instructions compile cleanly into the harnesses you care
about, package the result with [`apm pack`](./pack-a-bundle/) and
share it via [a marketplace](./publish-to-a-marketplace/).
