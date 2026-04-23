---
title: "Your First Package"
description: "Build a real APM package with a skill and an agent, install it, and ship it as a plugin."
sidebar:
  order: 3
---

In about ten minutes you will scaffold an APM package, add a skill that
auto-activates inside Copilot or Claude, add a custom agent that pairs with
it, install both into a project, and ship the result as a plugin. No prompts,
no `cat <<EOF`, no compile step you do not need.

If you want the conceptual map first, read [Anatomy of an APM Package](../../introduction/anatomy-of-an-apm-package/).
Otherwise, start here.

## Prerequisites

- APM installed -- see [Installation](/apm/getting-started/installation/).
- A GitHub account and an empty repo for publishing (step 5).
- A runtime where you can try the result: GitHub Copilot, Claude Code, or
  Cursor.

## 1. Scaffold

```bash
apm init -y team-skills
cd team-skills
```

`apm init` creates exactly one file -- the manifest. The `.apm/` source tree
is yours to author.

```
team-skills/
+-- apm.yml
```

Open `apm.yml` and give it a real description. The rest of the manifest is
already correct:

**`apm.yml`**

```yaml
name: team-skills
version: 1.0.0
description: Skills and agents for our team's review workflow
author: your-handle
dependencies:
  apm: []
  mcp: []
scripts: {}
```

## 2. Add a skill

A **skill** is a chunk of expertise that the runtime activates automatically
based on its `description`. No slash command, no manual selection: the agent
sees the description, decides the skill is relevant, and pulls it in. That
auto-activation is what separates skills from prompts.

Create one for drafting pull-request descriptions:

**`.apm/skills/pr-description/SKILL.md`**

```markdown
---
name: pr-description
description: >-
  Activate when the user asks for a pull-request description, a summary of
  uncommitted changes, or release notes. Use when preparing to open a PR or
  when the user says "draft a PR description for me".
---
# PR Description Skill

Produce a PR description with these sections, in order:

## Summary

One sentence. What changes and why. No file lists, no implementation detail.

## Motivation

Two to four sentences. The problem this solves or the capability it adds.
Link to the issue or design doc if one exists.

## Changes

Bullet list grouped by area (e.g. "API", "Tests", "Docs"). One bullet per
logical change, not per file.

## Risk and rollback

Note any breaking changes, migrations required, or feature flags.
Mention how to revert if something breaks.

## Testing

How you verified the change. Commands run, environments tested.
```

The frontmatter `description` is a contract with the runtime: write it as
"activate when ...". The body is the operating manual the agent reads when
the skill fires.

> Want to inspect a real one? The skill that governs this CLI's own
> architecture decisions lives at
> [`.apm/skills/python-architecture/SKILL.md`](https://github.com/microsoft/apm/blob/main/.apm/skills/python-architecture/SKILL.md)
> in this repo. Same shape, different concern.

See the [Skills guide](/apm/guides/skills/) for the full schema.

## 3. Add a custom agent

A **custom agent** (`.agent.md`) is a named expert your runtime can invoke
directly. While skills auto-activate based on context, agents are summoned
on demand -- typically with `@agent-name`.

Pair the skill with a reviewer agent that critiques the diff before the PR
goes out:

**`.apm/agents/team-reviewer.agent.md`**

```markdown
---
name: team-reviewer
description: Senior reviewer that critiques diffs against team standards before PR submission.
---
# Team Reviewer

You are a senior engineer reviewing a teammate's diff before it becomes
a pull request. Your job is to catch the things that waste reviewer
time downstream.

## What to check, in order

1. **Correctness.** Does the code do what its commit message claims?
   Spot logic errors, off-by-ones, unhandled error paths.
2. **Tests.** Are the changed code paths covered? Are new public APIs
   exercised by at least one test? Flag missing coverage explicitly.
3. **Naming and clarity.** Are names accurate? Would a new contributor
   understand this in six months?
4. **Surface area.** Does this change export anything new? If yes, is
   that intentional and documented?

## Output format

Group findings by severity: **Blocking**, **Should fix**, **Nit**.
For each finding, cite the file and line. End with a one-line verdict:
"Ready to ship", "Address blockers then ship", or "Needs another pass".

Do not rewrite the code yourself. Point and explain.
```

> A real example: this repo's documentation agent lives at
> [`.apm/agents/doc-writer.agent.md`](https://github.com/microsoft/apm/blob/main/.apm/agents/doc-writer.agent.md).

See the [Agent Workflows guide](/apm/guides/agent-workflows/) for more.

## 4. Deploy and use

Run install with no arguments. APM treats your repo as the package and
deploys its `.apm/` content into the runtime directories your tools read:

```bash
apm install
```

Output:

```
[+] <project root> (local)
|-- 1 agent integrated -> .github/agents/
|-- 1 skill(s) integrated -> .github/skills/
[i] Added apm_modules/ to .gitignore
```

Your tree now has source on the left and runtime-ready output on the right:

```
team-skills/
+-- .apm/                              # source you edit
|   +-- skills/
|   |   +-- pr-description/SKILL.md
|   +-- agents/
|       +-- team-reviewer.agent.md
+-- .github/                           # generated by apm install
|   +-- skills/
|   |   +-- pr-description/SKILL.md
|   +-- agents/
|       +-- team-reviewer.agent.md
+-- apm.yml
+-- apm.lock.yaml
```

`apm install` auto-detects which runtimes you have. The example above shows
`.github/` because Copilot is the default fallback. If `.claude/`, `.cursor/`,
or `.opencode/` exists in the project, they get populated too. To target
explicitly, see the [Compilation guide](/apm/guides/compilation/).

> **What about `apm compile`?** Compile is a different concern: it
> generates merged `AGENTS.md` / `CLAUDE.md` files for tools that read a
> single top-level context document (Codex, Gemini, plain `agents`-protocol
> hosts). Copilot, Claude Code, and Cursor read the per-skill directories
> directly -- no compile step needed.

Now open Copilot or Claude in this project. Ask "draft a PR description for
my last commit". The `pr-description` skill activates on its own. To get the
review pass, type `@team-reviewer review my staged changes`.

## 5. Publish as a package

Push to GitHub:

```bash
git init
git add apm.yml .apm/
git commit -m "Initial team-skills package"
git remote add origin https://github.com/your-handle/team-skills.git
git push -u origin main
```

In any other project's `apm.yml`:

```yaml
dependencies:
  apm:
    - your-handle/team-skills
```

Then `apm install` -- consumers get the same skill and agent in their
runtime dirs, with version pinning recorded in `apm.lock.yaml`.

For a real published package to read, see
[`microsoft/apm-sample-package`](https://github.com/microsoft/apm-sample-package)
(install with `apm install microsoft/apm-sample-package#v1.0.0`).

## 6. Ship as a plugin (optional)

The same package can ship as a standalone plugin -- no APM required for
consumers. This lets you target plugin-aware hosts (Copilot CLI plugins,
the broader plugin ecosystem) with the primitives you already authored.

```bash
apm pack --format plugin
```

Output:

```
build/team-skills-1.0.0/
+-- plugin.json        # synthesized from apm.yml
+-- agents/
|   +-- team-reviewer.agent.md
+-- skills/
    +-- pr-description/SKILL.md
```

No `apm.yml`, no `apm_modules/`, no `.apm/`. Just primitives in
plugin-native layout.

If you know up front that you want to ship a plugin, you can scaffold with
`apm init --plugin team-skills`, which adds `plugin.json` next to `apm.yml`
from day one. APM still gives you dependency management, the lockfile, and
audit while you author; pack produces the plugin bundle when you ship.

For the full reference, see the [Pack & Distribute guide](/apm/guides/pack-distribute/)
and the [Plugin authoring guide](/apm/guides/plugins/).

## Next steps

- [Anatomy of an APM Package](/apm/introduction/anatomy-of-an-apm-package/)
  -- the full mental model: `.apm/` vs `apm_modules/` vs `.github/`.
- [Skills guide](/apm/guides/skills/) -- bundled resources, sub-skills,
  activation tuning.
- [Agent Workflows guide](/apm/guides/agent-workflows/) -- chaining agents,
  GitHub Agentic Workflows integration.
- [Dependencies guide](/apm/guides/dependencies/) -- depend on other APM
  packages, file-level imports, version pinning.
- [`apm audit`](/apm/reference/cli-commands/) -- scan dependencies for
  policy violations before they ship.
