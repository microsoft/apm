---
title: Hooks and commands
description: Two target-specific primitives -- lifecycle hooks and slash commands -- with strict per-harness reach.
---

Hooks and slash commands are the two APM primitives that do not pretend
to be portable. Unlike skills or instructions, they ship to a strict
subset of harnesses, never get folded into `AGENTS.md`, and rely on
each target's own format. Author them when the value to a specific
harness justifies the per-target maintenance.

This page covers both. For the cross-harness reach map, see
[Primitives and targets](../../concepts/primitives-and-targets/).
For dev-only versus prod separation in the manifest, see
[Dev-only primitives](../../concepts/primitives-and-targets/#dev-only-primitives).

## Why they are target-specific

A skill is a markdown file APM can route to every canonical skill target. A hook
is a runtime callback fired by one harness inside its own tool loop.
A slash command is a command-palette entry surfaced by an IDE.
Neither generalizes: nothing reaches `AGENTS.md`, nothing routes to
harnesses that lack the concept. Unsupported targets are silently
skipped, not errors. Treat both as opt-in surface, not as your
primary distribution path.

## Hooks

Source layout. APM discovers hook JSON files in either of two
directories at the package root:

```
your-package/
+-- .apm/
|   +-- hooks/
|       +-- pretool-validate.json
+-- hooks/                      # also discovered (Claude-native layout)
    +-- post-edit-format.json
```

Each file is a JSON document keyed by lifecycle event. APM accepts the
Claude (`PreToolUse`, `PostToolUse`) and Copilot (`preToolUse`,
`postToolUse`) shapes; events are renamed per target during merge.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {"type": "command", "command": "${PLUGIN_ROOT}/scripts/validate.sh", "timeout": 10}
        ]
      }
    ]
  }
}
```

APM also accepts the "naked" Claude settings-slice shape -- event names at
the top level with no outer `"hooks":` wrap. This is the literal shape
Claude Code accepts inside its own `settings.json`, so a hooks slice copied
straight from there works as a standalone APM hook file:

```json
{
  "PreToolUse": [
    {
      "hooks": [
        {"type": "command", "command": "${PLUGIN_ROOT}/scripts/validate.sh", "timeout": 10}
      ]
    }
  ]
}
```

Both shapes are normalized internally before merge. A file whose `"hooks"`
key is present but not a JSON object fails closed with a warning; a file
that parses cleanly but contributes zero entries also logs a warning so
authors notice empty merges during development.

The `${PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`, `${CURSOR_PLUGIN_ROOT}`, and `${KIRO_PLUGIN_ROOT}`
tokens resolve to the installed package root and are rewritten per
target. Plain `./script.sh` resolves relative to the hook file. If the hook
file lives in `hooks/` or `.apm/hooks/`, a path like
`./hooks/run-hook.sh` resolves from the package root so the deployed
path is not doubled.

When a hook command points at a script inside a package hook directory,
APM deploys the hook source bundle so sibling helper modules stay
available at runtime:

- Claude-family merged targets (Claude, Cursor, Codex, Gemini,
  Antigravity, and Windsurf), Copilot, and Kiro receive the same bundle.
- Root hook JSON descriptors, symlinks, and `.apm-pin` markers are not
  deployed.
- JavaScript and TypeScript hook bundles get a minimal `package.json`
  sidecar with the nearest source package's Node `type`; packages
  without an explicit `type` deploy as `commonjs`, and shell-only
  bundles do not get a sidecar.

For multi-target packages, prefer simple hook filenames plus consumer
per-dependency `targets:` in `dependencies.apm` to limit reach. If the
same manifest stem is mirrored in both `hooks/` and `.apm/hooks/`, APM
integrates the `.apm/hooks/` copy once per target.

:::note
See the object-form dependency field in
[Manifest Schema](../../reference/manifest-schema/#412-object-form) and
the target vocabulary in
[Primitives and targets](../../concepts/primitives-and-targets/).
:::

:::caution[Deprecated]
Hook filename routing (`*-<harness>-hooks.json`) is deprecated. Ship one
hook manifest; consumers scope harness reach with the per-dependency
`targets:` field. The filename router still works during the deprecation
window and warns at install time.

Before: name the manifest `my-pkg-codex-hooks.json`. After: keep
`hooks.json` generic and let the consumer set `targets: [codex]`.
Combined deprecated stems such as `claude-codex-hooks.json` route to every
named target token during the migration window.
Stems with target tokens outside the trailing target suffix (for example
`codex-launch-hooks.json`) fall back to universal or suffix routing and print a
warning naming the ignored token.
:::

Supported targets and where the integrator writes:

| Target   | Output                                | Mode                 |
|----------|---------------------------------------|----------------------|
| copilot  | `.github/hooks/<pkg>-<name>.json`     | one file per hook    |
| claude   | `.claude/settings.json`               | merged into settings |
| cursor   | `.cursor/hooks.json`                  | merged               |
| gemini   | `.gemini/settings.json`               | merged               |
| codex    | `.codex/hooks.json`                   | merged               |
| windsurf | `.windsurf/hooks.json`                | merged               |
| kiro     | `.kiro/hooks/<package-slug>-<hook-file-stem-slug>-<event-slug>-<n>.json` | one file per hook action |
| opencode | -- not supported --                   | silently skipped     |

Copilot hook files are namespaced with the source package name to avoid
collisions across installed deps; bundled scripts land alongside under
`.github/hooks/scripts/<pkg>/`.

Merged hook files contain only each target's native upstream fields. APM writes
ownership metadata to a sibling `apm-hooks.json` sidecar for Claude, Cursor,
Gemini, Codex, Windsurf, and Antigravity. The sidecar is created and cleaned up
automatically alongside the native config; it is an APM implementation detail
and should not be edited by hand.

Verified against `src/apm_cli/integration/targets.py` and
`src/apm_cli/integration/hook_integrator.py`.

## Commands

Slash commands share their source with prompts. There is no
`.apm/commands/` directory:

```
your-package/
+-- .apm/
    +-- prompts/
        +-- review-pr.prompt.md   # also routes as a slash command
```

Frontmatter the command integrator preserves: `description`,
`allowed-tools`, `model`, `argument-hint`, `input`. Other keys (for
example `author`, `mcp`, `parameters`) are dropped during the
transform and surfaced as install-time diagnostics.

```markdown
---
description: Review the current PR for security regressions.
allowed-tools: [Read, Grep]
argument-hint: "[pr-number]"
input:
  - name: pr_number
    description: The PR number to review.
---

Review pull request #$pr_number. Focus on auth and input handling.
```

Supported targets and output paths:

| Target   | Output                           | Format                |
|----------|----------------------------------|-----------------------|
| claude   | `.claude/commands/<name>.md`     | native markdown       |
| cursor   | `.cursor/commands/<name>.md`     | claude-format subset  |
| opencode | `.opencode/commands/<name>.md`   | opencode markdown     |
| gemini   | `.gemini/commands/<name>.toml`   | TOML                  |
| windsurf | `.windsurf/workflows/<name>.md`  | called "workflows"    |
| copilot  | -- not a command --              | ships as a prompt     |
| codex    | -- not supported --              | silently skipped      |

Verified against `src/apm_cli/integration/targets.py` and
`src/apm_cli/integration/command_integrator.py`.

## When NOT to use these

Reach for a skill, instruction, or prompt first. Use hooks or commands
only when the behavior is a runtime callback (hook) or a
command-palette entry (command), you accept that consumers on Copilot,
Codex, or OpenCode will not get them, and you will own per-target
formats. "Run a script before every tool call" fits a hook. "Give the
agent a procedure" fits a skill -- and reaches every harness.

## Pitfalls

- **Hook event names.** Author in Claude or Copilot conventions only.
  The integrator renames; arbitrary event names will not be mapped.
- **Cursor command frontmatter loss.** Cursor reuses the Claude
  command transformer today, so any prompt-only metadata is dropped
  with a diagnostic. Keep Cursor commands to the preserved key set.
- **Script paths.** Use `${PLUGIN_ROOT}` (or the harness-specific
  alias) for scripts that ship inside the package. Plain absolute
  paths break on consumers' machines.
- **Hook script path resolution.** `apm install -g` (user-scope)
  rewrites `${PLUGIN_ROOT}` and relative `./` references to absolute
  paths so Claude Code can execute scripts regardless of the working
  directory. Project-scope `apm install` (no `-g`) keeps `command`
  paths repo-relative so checked-in configs stay portable across
  clones, contributors, and CI. Either way, if a referenced script
  is missing at install time the installer emits a warning -- in
  user-scope the unexpanded variable is rewritten to the absolute
  source path so the hook fails loudly at runtime; in project-scope
  the variable is left in place so the deployed config never embeds
  the installer's machine-local prefix.
- **Same `.prompt.md` is two primitives.** A single
  `.apm/prompts/foo.prompt.md` becomes Copilot's prompt and Claude's
  `/foo` command in the same install. Name files with both surfaces
  in mind.
- **OpenCode and hooks.** OpenCode has no hooks concept. Do not author
  a Claude+OpenCode package and assume hooks reach both -- they do
  not. The install log notes the skip.

Once your hooks and commands are in place, run `apm install --dry-run`
to preview what each target will receive, then `apm pack` to bundle.
See [Compile](../compile/) and [Pack a bundle](../pack-a-bundle/).
