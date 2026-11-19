---
title: Write skills, instructions and hooks
description: Author each kind of agent file once under .apm/, with the minimum frontmatter, and know where APM writes it for each harness.
---

Use this page when you add files to a package. Each section names one primitive
(APM's word for one kind of file an agent loads), where it lives in your package,
the frontmatter it needs, where `apm install` writes it, and the mistake authors
make most often. A harness is the agent tool that reads the deployed files, such
as GitHub Copilot, Claude Code, Cursor or Codex.

File-based primitives live under `.apm/` in your package; MCP servers are
declared in `apm.yml`. Run `apm install` in the
package repository to deploy your own files and check the result
([Create a package](../create-a-package/)). The full map of what lands where, per
harness and scope, is the
[Targets matrix](../../reference/targets-matrix/).

## Skills

A skill is a folder of guidance the agent loads when a request matches its
`description`.

```markdown title=".apm/skills/review/SKILL.md"
---
name: review
description: Review a diff against the team's checklist. Use when the user asks for a code review.
---

Steps the agent follows...
```

Put extra material in `references/`, `scripts/` or `assets/` next to `SKILL.md`;
APM copies the whole folder. It lands in `.agents/skills/review/` for Copilot,
Cursor, Codex, Gemini CLI, OpenCode and Windsurf, and in `.claude/skills/review/`
for Claude Code.

**Gotcha:** `name` must be lowercase letters, digits and hyphens, and equal the
folder name. If they differ, the folder name wins. Write the description as a
trigger ("Use when...") so it does not collide with other skills.

## Instructions

An instruction is a rule the agent applies to files that match a glob, such as
a style guide for Python.

```markdown title=".apm/instructions/python.instructions.md"
---
description: Python style rules
applyTo: "**/*.py"
---

- Use pathlib, not os.path.
```

Copilot gets `.github/instructions/python.instructions.md`; Claude Code gets
`.claude/rules/python.md` with `applyTo` turned into `paths:`. Codex, Gemini CLI
and OpenCode read one combined file instead: `apm compile` builds `AGENTS.md` or
`GEMINI.md`, and consumers see a reminder after install.

**Gotcha:** without `applyTo`, some harnesses do not know when to load the rule;
Cursor may skip it. Use `applyTo: "**"` for a rule that always applies.

## Agents

An agent is a named persona the user calls on demand, such as a security
reviewer.

```markdown title=".apm/agents/security-review.agent.md"
---
name: security-review
description: Reviews diffs for missing input validation.
---

You are a security reviewer...
```

It lands in `.github/agents/security-review.agent.md` for Copilot,
`.claude/agents/security-review.md` for Claude Code and
`.codex/agents/security-review.toml` for Codex.

**Gotcha:** optional fields such as `model` and `tools` do not translate to every
harness. Codex, for example, drops a field it cannot represent, and install
prints a warning naming the agent. Read the warnings after `apm install`.

## Prompts

A prompt is a task the user starts by name, with optional inputs.

```markdown title=".apm/prompts/release-notes.prompt.md"
---
description: Draft release notes for a tag.
input:
  - tag: "The tag to describe"
---

Summarize the changes in ${input:tag}...
```

The file name is the command name. It lands in `.github/prompts/` for Copilot,
as the `/release-notes` command in `.claude/commands/` for Claude Code, and as
the native command format for Cursor, Gemini CLI, OpenCode, Windsurf and Kiro.
Codex has no prompts.

**Gotcha:** Copilot CLI and the Copilot harness in VS Code do not load prompt
files. To reach them, ship the task as a skill with
`disable-model-invocation: true` so it runs when called, not on its own. APM keeps
`description`, `input`, `allowed-tools`, `model` and `argument-hint`, and drops
other keys with a warning.

## Hooks and commands

A hook is a command the harness runs on an event, such as after every edit.
Hook files are JSON under `.apm/hooks/`; scripts sit next to them and use paths
relative to the hook file.

```json title=".apm/hooks/format.json"
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit",
        "hooks": [{ "type": "command", "command": "./scripts/format.sh", "timeout": 10 }]
      }
    ]
  }
}
```

APM merges it into `.claude/settings.json` for Claude Code, writes
`.github/hooks/<package>-format.json` for Copilot, and copies
`scripts/format.sh` where each harness can run it. Event names are renamed per
harness (`PostToolUse` becomes `postToolUse` for Copilot). OpenCode gets no hooks.

Commands (slash commands) have no folder of their own: APM builds them from your
prompts, as described above.

**Gotcha:** a hook runs code on the consumer's machine. When a consumer's
project or organization turns on the executables gate, your hooks wait until
they [approve your package](../../consumer/approve-executables/). Every
hook file reaches every harness the consumer targets; to limit a hook to Claude
Code, publish it in a package whose `apm.yml` sets `targets: [claude]`.


### Migrating from filename routing

APM no longer routes hook files named `*-<target>-hooks.json` or `hooks-<target>.json` to
one target only. If dropping that routing would give a target a hook file the old name kept
from it, `apm install` fails for that package, names the target and files, and writes
nothing from it; until then `apm uninstall` of another package stops with the same message.

- **Authors:** split the package so each target set has its own package whose `apm.yml`
  declares `targets:`, with an ordinary `hooks/hooks.json`. Keep shared hooks in a common
  package.
- **Consumers:** if the package ships hooks for one target only, narrow the dependency with
  object-form `targets: [codex]`. If it also ships a generic `hooks.json`, ask its author
  to split it.

Then reinstall. A file whose old name already matched every target keeps installing.

## MCP servers

An MCP server gives the agent tools, such as access to GitHub or a database. A
package declares servers in `apm.yml`, not as files:

```yaml title="apm.yml"
dependencies:
  mcp:
    - io.github.github/github-mcp-server
    - name: internal-search
      registry: false
      transport: stdio
      command: search-server
      env:
        API_TOKEN: "${SEARCH_TOKEN}"
```

`io.github.github/github-mcp-server` comes from the MCP registry;
`internal-search` is self-defined. On
install, APM writes each consumer harness's own MCP config file
([Add MCP and LSP servers](../../consumer/install-mcp-servers/)).

**Gotcha:** never commit a secret; reference an environment variable as above. A
self-defined server in a package that a consumer pulls in transitively (through
another package) is held back until they approve it, so document it in your
README.

## Links between files

Link from one primitive to another file in your package with a normal relative
Markdown link:

```markdown title=".apm/agents/architect.agent.md"
See [design patterns](../../docs/patterns.md).
```

When APM copies an agent, instruction, prompt or command into a consumer's
harness folder, it rewrites the link so it still points at the file, now inside
`apm_modules/`. Links inside one skill folder are not rewritten, because the
folder is copied whole.

**Gotcha:** APM rewrites a link when the target file exists and stays inside
your package. A link to another package, an absolute path or a missing
file is left as written, and breaks for consumers. Install your package into a
test consumer and open a deployed file to check
([Test a package locally](../test-packages/)).
