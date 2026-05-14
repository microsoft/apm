---
title: Instructions and agents
description: Author scope-attached rules and persona scoping modules that compile to every supported harness.
---

These two primitives pair naturally. An **instruction** is a
scope-attached rule: a coding standard, naming convention, or review
checklist that fires when the agent touches files matching a glob. An
**agent** is a persona scoping module: a named specialist (security
reviewer, migration assistant, on-call SRE) the user invokes
explicitly. Instructions shape *how* the model behaves on any given
file. Agents shape *who* the model becomes when summoned.

Reach across harnesses differs and matters when you choose. See
[Primitives and targets](../../../concepts/primitives-and-targets/)
for the full matrix; the gist is below.

## Instructions

A unit of policy that travels with a glob. Authors write one rule;
APM compiles it to whatever frontmatter and directory the harness
expects.

### Layout

```
my-package/
  apm.yml
  .apm/
    instructions/
      python-style.instructions.md
      pr-review-checklist.instructions.md
```

File names end in `.instructions.md`. The basename (minus the double
extension) becomes the deployed filename stem.

### Frontmatter

```markdown
---
description: Python style rules enforced on src/ and tests/
applyTo: "**/*.py"
---

- Use `pathlib.Path`, never `os.path`.
- Tests live next to the module under `tests/<module>/`.
- ...
```

| Key | Required | Purpose |
|---|---|---|
| `description` | yes | One-line summary; used in compiled context indexes |
| `applyTo` | yes for instructions | Glob (or comma-separated globs) the rule binds to |

`applyTo` is the load-bearing field. Without it the rule is treated as
unconditional and gets folded into compiled context files
(`AGENTS.md`, `GEMINI.md`) instead of a per-file rule directory. With
it, each harness wraps the body in its own scoping syntax.

### Body conventions

- Lead with bullets, not prose. Instructions are read by an agent
  mid-task.
- One topic per file. Split `python-style` and `python-testing`; do
  not co-mingle.
- Cite paths inside the repo with backticks; do not assume any other
  context is loaded.
- Skip greetings and meta ("In this file we will..."). State the rule.

### What compiles where

| Target | Output path | Transform |
|---|---|---|
| copilot | `.github/instructions/<name>.instructions.md` | verbatim; `applyTo` preserved |
| claude | `.claude/rules/<name>.md` | `applyTo` -> `paths:` list |
| cursor | `.cursor/rules/<name>.mdc` | `applyTo` -> `globs:`; description auto-derived if missing |
| windsurf | `.windsurf/rules/<name>.md` | `applyTo` -> `trigger: glob` + `globs:`; missing `applyTo` -> `trigger: always_on` |
| codex | folded into `AGENTS.md` | compile-only, no per-file deploy |
| gemini | folded into `GEMINI.md` | compile-only, no per-file deploy |
| opencode | folded into `AGENTS.md` | compile-only, no per-file deploy |

Source: `src/apm_cli/integration/instruction_integrator.py`,
`src/apm_cli/integration/targets.py`.

## Agents

A specialist persona invoked by name, with optional model and tool
constraints. Think of it as a callable role -- the user types
`@security-review` and the harness loads the body as system context.

### Layout

```
my-package/
  apm.yml
  .apm/
    agents/
      security-review.agent.md
      migration-assistant.agent.md
```

File names end in `.agent.md`. APM also accepts `.chatmode.md` and the
legacy `.apm/chatmodes/` directory for backward compatibility; new
work should use `.agent.md` under `.apm/agents/`.

### Frontmatter

```markdown
---
name: security-review
description: Reviews diffs for OWASP top-10 issues and missing input validation.
model: gpt-5
tools:
  - read
  - grep
---

You are a security reviewer. Your job is to inspect the working diff
for...
```

| Key | Required | Purpose |
|---|---|---|
| `name` | recommended | Display name; defaults to filename stem |
| `description` | yes | Used by Cascade and Copilot to decide when to surface the agent |
| `model` | optional | Pinned model the harness should switch to when invoked |
| `tools` | optional | Whitelist of tools the persona may call |

`model` and `tools` reach Copilot, Claude, Cursor, and OpenCode
verbatim. Codex receives a TOML translation. Windsurf drops both
fields and emits a diagnostic warning at install time -- its skill
format does not support per-persona model or tool scoping.

### Body conventions

- Open with role and scope in two sentences. The harness uses this as
  the system prompt.
- Define what the persona will and will not do. Boundaries make
  agents useful.
- List the artifacts it expects ("the open PR diff", "the failing
  test name") and what it returns ("a markdown review with file:line
  citations").
- Keep the body under 300 lines. Long agents drown the harness's
  context window before the user's task even loads.

### What compiles where

| Target | Output path | Transform |
|---|---|---|
| copilot | `.github/agents/<name>.agent.md` | verbatim |
| claude | `.claude/agents/<name>.md` | verbatim |
| cursor | `.cursor/agents/<name>.md` | verbatim |
| opencode | `.opencode/agents/<name>.md` | verbatim |
| codex | `.codex/agents/<name>.toml` | YAML frontmatter -> TOML; body becomes `developer_instructions` |
| windsurf | `.windsurf/skills/<name>/SKILL.md` | reformatted as a Cascade skill; `model`/`tools` dropped with a warning |
| gemini | not deployed | Gemini CLI has no agents primitive |

Source: `src/apm_cli/integration/agent_integrator.py`,
`src/apm_cli/integration/targets.py`.

## Choosing one or the other

Pick **instructions** when:

- The rule applies to a *file pattern*, not a workflow.
- You want the agent to follow it implicitly, without being summoned.
- The content is short, declarative, and reviewable as policy.

Pick **agents** when:

- A user needs to *invoke* a specialist on demand.
- The behaviour involves a sequence of steps, decisions, or model
  switches, not a static rule.
- You want to scope tool access (e.g. read-only review persona).

Many packages ship both: instructions for the always-on guardrails,
plus one or two agents for the deeper workflows that warrant a
dedicated persona.

## Common pitfalls

- **Missing `applyTo`.** An instruction without `applyTo` stops being
  scope-attached and gets folded into the compiled context file. If
  you wanted Cursor or Copilot to scope it to `**/*.ts`, the rule
  will not bind.
- **Agent named `default` or `start`.** These collide with script
  resolution in `apm run`. Pick a descriptive name.
- **`model:` and `tools:` on a Windsurf-targeted agent.** Cascade has
  no equivalent; APM warns and drops them. If those constraints are
  load-bearing, do not target windsurf for that agent -- ship it as
  an instruction instead, or restrict the package's `targets:`.
- **Agent body that re-states global instructions.** Agents inherit
  the workspace's compiled context. Restate only what the persona
  needs to *override* or *add*; do not duplicate `python-style`
  inside `code-reviewer.agent.md`.
- **Co-mingling rules and persona.** A 600-line `.agent.md` that
  contains style rules, review checklist, and persona prompt is two
  primitives in a trench coat. Split it.

## Verify before shipping

```bash
apm compile --validate           # frontmatter + structure check, no writes
apm compile --target cursor      # see exactly what lands in .cursor/
apm preview <script>             # if the agent is wired to a script
```

For the full primitive catalogue and target matrix, see
[Primitives and targets](../../../concepts/primitives-and-targets/).
For prompts and slash-commands, see
[Prompts](../prompts/). For packing and publishing, see
[Pack a bundle](../../pack-a-bundle/).
