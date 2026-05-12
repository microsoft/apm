---
title: Author a prompt
description: Ship a parameterized, single-purpose AI workflow as a .prompt.md primitive that compiles to every harness's slash-command format.
---

A prompt is a single-purpose, parameterized AI workflow. Write one
Markdown file with frontmatter; APM deploys it as a Copilot prompt,
a Claude `/command`, a Cursor command, an OpenCode command, a Gemini
TOML command, and a Windsurf workflow.

Use a prompt when the consumer invokes the workflow on demand
("review this PR", "draft a release note"). Use a
[skill](../skills/) when the harness should auto-discover a
meta-guide mid-conversation. Prompts are called; skills are reached
for.

## File layout

Place prompt files under `.apm/prompts/`. The base filename becomes
the command name on every target.

```
my-package/
  apm.yml
  .apm/
    prompts/
      review-pr.prompt.md       # -> /review-pr
      release-notes.prompt.md   # -> /release-notes
```

Files at the package root (`<pkg>/*.prompt.md`) are also discovered
for backward compatibility, but `.apm/prompts/` is canonical and
what `apm init` scaffolds.

## Frontmatter

Five keys are preserved by the cross-tool transform. Anything else
is dropped at compile time and surfaced via diagnostics so consumers
on lossy targets see a warning, not silent data loss.

| Key | Type | Purpose |
|---|---|---|
| `description` | string | One-line summary; shown in command pickers. Required for discoverability. |
| `input` | list | Argument names the prompt reads. Simple list (`[name, scope]`) or object list (`- name: "what it is"`). |
| `allowed-tools` | list | Tool allow-list (Claude/Cursor honor this; Copilot ignores it). |
| `model` | string | Preferred model slug (Claude/Cursor only). |
| `argument-hint` | string | Free-form hint shown in the picker; auto-derived from `input` when omitted. |

Reference inputs in the body with `${input:name}`. The compiler
rewrites these per target (for example, `$name` for Claude
commands).

```markdown
---
description: Review a pull request against our coding standards.
input:
  - pr_url: "URL of the PR to review"
  - focus: "Optional focus area (e.g. security, perf)"
allowed-tools: [Bash, Read, Grep]
---

# Review PR ${input:pr_url}

You are reviewing the changes in ${input:pr_url}. Focus on
${input:focus} when set; otherwise apply the full checklist.

1. Fetch the diff.
2. Flag any deviation from `.github/CONTRIBUTING.md`.
3. Summarize blockers, suggestions, and nits in three sections.
```

## Body conventions

One prompt, one focused intent. If the body branches into "if asked
X do Y; if asked Z do W", split it. The picker on every target is
flat -- many small prompts beat one big one.

Write to the model in the second person and lead with the verb. The
body is delivered verbatim into a chat session.

## Where it lands per target

`apm install` (or `apm compile`) routes one source file to every
detected harness. Verified against
[`src/apm_cli/integration/targets.py`](https://github.com/microsoft/apm/blob/main/src/apm_cli/integration/targets.py)
and `command_integrator.py`.

| Target | Output path | Format |
|---|---|---|
| copilot | `.github/prompts/<name>.prompt.md` | verbatim copy |
| claude | `.claude/commands/<name>.md` | `/command`, inputs become `$arg` |
| cursor | `.cursor/commands/<name>.md` | shared command transform |
| opencode | `.opencode/commands/<name>.md` | shared command transform |
| gemini | `.gemini/commands/<name>.toml` | TOML command |
| windsurf | `.windsurf/workflows/<name>.md` | workflow |
| codex | (none) | Codex has no prompts or commands primitive |

For the broader primitive-by-target reach map, see
[Primitives and targets](../../../concepts/primitives-and-targets/).

## How a consumer invokes it

After `apm install`, the prompt is reachable from each harness's
native command surface. No separate registration step.

```
Copilot   open the prompts picker; select "review-pr"
Claude    /review-pr <pr_url> <focus>
Cursor    /review-pr ...
OpenCode  /review-pr ...
Gemini    /review-pr ...
Windsurf  /review-pr (from the workflows menu)
```

A producer who also ships a runnable script can wire the prompt into
`apm.yml`'s `scripts:` block so consumers can bypass the picker:

```yaml
scripts:
  review: copilot --prompt .apm/prompts/review-pr.prompt.md
```

`apm run review --param pr_url=https://...` compiles the prompt with
the parameter bound and invokes the harness. See
[Lifecycle: RUN](../../../concepts/lifecycle/) for the rewrite rules.

## Pitfalls

- **Non-preserved frontmatter is dropped.** Keys like `author`,
  `mcp`, or `parameters` survive on Copilot (verbatim copy) but are
  stripped on Claude/Cursor/OpenCode/Gemini/Windsurf. APM logs a
  diagnostic at install time. Keep authoritative metadata to the
  five preserved keys.
- **Input names must match `[A-Za-z][\w-]{0,63}`.** Other entries
  are rejected with a warning at integrate time and never reach the
  command frontmatter.
- **Codex receives nothing.** Do not assume a prompt is universal.
  If Codex coverage matters, ship the same workflow as a
  [skill](../skills/) -- skills route to all seven harnesses.
- **One file, one command name.** Two prompts with the same base
  filename in `.apm/prompts/` and at the package root collide; the
  later writer wins on copilot and the transform fails on
  Claude/Cursor. Pick a location and stick to it.
- **`apm compile` does not re-scan for hidden Unicode.** Edits
  between installs are not gated. Run `apm audit` before publishing.
