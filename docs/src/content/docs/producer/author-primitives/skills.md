---
title: Author a skill
description: Write a SKILL.md primitive APM can install, scan, lock, and deploy across every supported harness.
---

A **skill** is a model-invoked guide. The agent picks it up at runtime
based on its `description`, reads `SKILL.md`, and follows the body to
do a focused task. The format is the cross-tool agent-skills standard
(SKILL.md plus optional bundled resources). APM is the package manager
for skills, not the spec; for the full primitive matrix see
[Primitives and targets](../../../concepts/primitives-and-targets/).

## Folder layout

Author every skill in its own directory under `.apm/skills/`:

```
.apm/skills/
+-- code-review-expert/
    +-- SKILL.md           # required: the skill itself
    +-- scripts/           # optional: executable helpers
    +-- references/        # optional: deep-dive context the skill loads on demand
    +-- assets/            # optional: templates, images, fixtures
    +-- examples/          # optional: sample inputs and outputs
```

The directory name is the skill's identity. `SKILL.md` is the only
required file; the four conventional subdirectories ship as-is when
APM copies the skill to a target. Single-skill repositories may also
place `SKILL.md` at the package root.

## Frontmatter contract

```yaml
---
name: code-review-expert
description: Use when the user asks for a code review, PR feedback, or a diff walkthrough on a Python or TypeScript change. Loads project conventions from references/ before commenting.
---
```

### `name`

- Lowercase alphanumeric and hyphens only (`a-z`, `0-9`, `-`).
- 1 to 64 characters.
- No consecutive hyphens, no leading or trailing hyphen.
- Must equal the parent directory name. APM derives `name` from the
  directory when frontmatter omits it; if both are present and
  disagree, the directory wins on disk and your declared `name` is
  ignored.

Verified in `src/apm_cli/integration/skill_integrator.py`
(`validate_skill_name`) and `src/apm_cli/primitives/parser.py`
(`parse_skill_file` derives `name` from `file_path.parent.name`).

### `description`

APM does not validate the description body, but the agent-skills
standard does, and runtimes use it to decide when to invoke the
skill. Four rules:

1. **Imperative.** Start with a verb that names the user action
   ("Use when", "Apply when"), not the skill ("This skill helps
   you...").
2. **Intent-first.** Lead with the user's intent, then the trigger
   conditions, then any constraints. Runtimes match on the first
   sentence.
3. **Indirect triggers.** Describe situations, not slash-commands.
   "Use when reviewing a Terraform PR" beats "Run /tf-review".
4. **<= 1024 characters.** Hard ceiling in the agent-skills spec.
   Anything longer truncates at runtime on some harnesses.

A bad description ("Helps with code") collides with every other
skill. A good description names the trigger so the runtime can
disambiguate.

## Body budget

Keep `SKILL.md` under **500 lines and 5000 tokens**. This is the
agent-skills convention, not an APM check, but every harness pays a
context-window tax for an oversized body. Overflow goes into
`references/<topic>.md` files that the body loads explicitly:

```markdown
## Project conventions

For the full naming policy, LOAD references/naming.md.
For migration steps, LOAD references/migrations.md.
```

The `LOAD references/<file>` line is a convention the agent recognizes
and follows. Bundle deep context, edge cases, and rare-path runbooks
in `references/`; keep `SKILL.md` to the always-relevant flow.

## Where it lands per target

`apm install` and `apm compile` deploy each skill folder to one
directory per active target. Routing is verified in
`src/apm_cli/integration/targets.py`.

| Target            | Deploy directory                             |
|-------------------|----------------------------------------------|
| `claude`          | `.claude/skills/<name>/SKILL.md`             |
| `windsurf`        | `.windsurf/skills/<name>/SKILL.md`           |
| `copilot`         | `.agents/skills/<name>/SKILL.md`             |
| `cursor`          | `.agents/skills/<name>/SKILL.md`             |
| `codex`           | `.agents/skills/<name>/SKILL.md`             |
| `gemini`          | `.agents/skills/<name>/SKILL.md`             |
| `opencode`        | `.agents/skills/<name>/SKILL.md`             |
| `agent-skills`    | `.agents/skills/<name>/SKILL.md` (explicit)  |

Five harnesses converge on the cross-tool `.agents/skills/`
directory. Claude and Windsurf keep harness-native paths because
their runtimes scan their own roots. The whole skill folder is
copied (`shutil.copytree`), so `scripts/`, `references/`, `assets/`,
and `examples/` ride along. Symlinks and the `.apm-pin` cache marker
are filtered out (`src/apm_cli/security/gate.py:ignore_non_content`).

To restore the pre-convergence layout where every harness gets its
own `.<harness>/skills/` copy, pass `--legacy-skill-paths` or set
`APM_LEGACY_SKILL_PATHS=1`.

## Preview before you commit

Two commands answer "what will this look like once installed?":

```bash
apm compile --validate          # parse frontmatter and structure; no writes
apm compile --dry-run           # print every placement decision; no writes
apm compile --target claude     # write only one target so you can diff
```

`apm compile --validate` is the fast lint loop. `--dry-run` shows the
full routing table for the current set of detected harnesses.
Targeting a single harness lets you inspect the actual file APM
would deploy without touching the others. See
[Compile](../../compile/) and
[Preview and validate](../../preview-and-validate/) for the broader
flow, and [Lifecycle](../../../concepts/lifecycle/) for where
compile sits between install and run.

## Common pitfalls

- **Description collision.** Two skills whose descriptions both start
  with "Helps with code" will fight for the same triggers.
  Lead with the verb plus the situation, not the skill's name.
- **Directory name drift.** If `name:` in frontmatter does not match
  the parent directory, the directory name wins on disk. Rename the
  directory or the frontmatter, not both halfway.
- **Oversized body.** A 2000-line `SKILL.md` blows past every
  runtime's recommended budget and pushes other context out of the
  window. Move the long tail into `references/` and load it on
  demand.
- **Hidden files in the bundle.** `apm install` runs the hidden-Unicode
  scan over every primitive before deploy. A zero-width character in a
  reference file blocks the install for every consumer. Run
  `apm audit --file .apm/skills/<name>/SKILL.md` while authoring.
- **Setting `target: vscode` in `apm.yml`.** There is no `vscode`
  target. Use `targets:` (plural) with slugs from the table above, or
  let auto-detection pick.
