---
title: Coming from another tool
description: Move skills, plugins and MCP servers you already install with npx skills, Claude Code, gh skill or by hand into one manifest and lockfile.
---

Use this page if your team already installs agent capabilities another way and wants the same
setup on every machine. You keep your harness, the agent tool that reads the files (GitHub
Copilot, Claude Code, Cursor and others). APM adds a manifest (`apm.yml`) and a lockfile
(`apm.lock.yaml`) underneath, so a teammate or CI gets the same files with
`apm install --frozen`.

| You use today | Run instead |
|---|---|
| `npx skills add owner/repo` | `apm install owner/repo` |
| Claude Code `/plugin marketplace add` and `/plugin install` | `apm marketplace add owner/repo`, then `apm install name@marketplace` |
| `gh skill install owner/repo skill --pin v1.2.0` | `apm install owner/repo/skills/skill#v1.2.0` |
| Skills copied into `.github/` or `.claude/` | `apm init --discover`, then `apm init --apply` |
| A hand-edited `.mcp.json` | `apm install --mcp NAME` |

Every command below assumes an `apm.yml` that names your harnesses. If you have none, start with
`apm init -y --target copilot,claude` (use the harnesses you run).

## From npx skills

Repositories you install with `npx skills add`, including a bare `skills/<name>/SKILL.md`
layout without `apm.yml`, install with `apm install`:

```bash frame="terminal"
apm install vercel-labs/agent-skills#<tag-or-sha>
apm install vercel-labs/agent-skills --skill deploy-to-vercel
```

`--skill` installs one skill from a collection; repeat it for more, or pass `--skill '*'` to
return to all of them. APM writes the selection to `apm.yml`, so a bare `apm install` on
another machine installs the same subset. Without a `#` ref, APM tracks the default branch and
warns `1 dependency unpinned`. The lockfile still pins the resolved commit; to
track releases on `apm update`, add a semver range such as `#^1.0.0` (a tag like `#v1.0.0` stays put). Skill lookup on
[skills.sh](https://skills.sh) keeps working: find there, install with APM.

## From Claude Code plugin marketplaces

Register the same marketplace with APM, then install plugins by the same `name@marketplace`:

```bash frame="terminal"
apm marketplace add anthropics/claude-code
apm install commit-commands@claude-code-plugins
```

```text frame="none"
[+] Marketplace 'claude-code-plugins' registered (12 plugins)
...
  [+] github.com/anthropics/claude-plugins-official/plugins/commit-commands#main
  |-- 3 commands integrated -> .claude/commands/
```

APM uses the name the marketplace declares, so `name@marketplace` matches what you typed in
Claude Code. It records the plugin's git source in `apm.yml` and its commit in the lockfile,
and writes the plugin's commands, skills and agents into `.claude/`. Claude Code then loads
them as project files, so `/plugin` does not list the plugin. Pin the marketplace with
`--ref` or `owner/repo#tag`, as APM's warning suggests. Details:
[Install plugins](../../consumer/install-plugins/) and
[Install from a marketplace](../../consumer/installing-from-marketplaces/).

## From gh skill

`gh skill` pins one skill to a tag or commit. APM pins the same way, with the skill's path
after the repository, and records it in a lockfile that covers every dependency:

```bash frame="terminal"
apm install github/awesome-copilot/skills/documentation-writer#<tag-or-sha>
```

Remove the copy `gh skill` installed before you install with APM; APM does not overwrite a
file it did not write.

## From folders copied by hand

If skills already live in your repository, for example `.claude/skills/review/SKILL.md`, adopt
them in place. Preview what APM finds:

```bash frame="terminal"
apm init --discover
```

```text frame="none"
[i] supported: .claude/skills/review -- Existing local package; only a dependency will be added.
[i] apm.yml dependencies.apm: path: ./.claude/skills/review
[i] Read-only preview. Use --apply to add these dependencies after confirmation.
```

Then add the references to `apm.yml` and install. In a script, add `--yes` to confirm:

```bash frame="terminal"
apm init --apply
apm install
```

The source stays where it is: APM does not copy, translate or run it, and reruns do not
duplicate the entry. Other harnesses get a copy, such as `.agents/skills/review/` for Copilot.
Where the folder already is the harness's own (`.claude/skills/` for Claude Code), APM keeps
your file and reports `1 file skipped`. Loose rules, hooks and MCP config are listed in the
report but not adopted.

To share these skills with other repositories, move them into a package of their own:
[Create a package](../../producer/create-a-package/).

## From hand-edited MCP config

Declare each server in `apm.yml` and let APM write the config each harness reads:

```bash frame="terminal"
apm install --mcp io.github.github/github-mcp-server
apm install --mcp filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace
apm install --mcp linear --transport http --url https://mcp.linear.app/sse
```

The first form resolves a server from the MCP registry; in the other two you define it
yourself. Before you install, delete your hand-written entry with the same name: APM keeps a
differing entry it did not write and reports a conflict. Compare the generated config with your old one for custom `env` values.
See [Add MCP and LSP servers](../../consumer/install-mcp-servers/).

## From an older APM version

Read the [CHANGELOG](https://github.com/microsoft/apm/blob/main/CHANGELOG.md) entries between
your version (`apm --version`) and the new one, upgrade the CLI
([Install APM](../installation/#upgrade-apm)), run `apm install`, review `git status`, and
commit. Changes that need action:

- **0.37 (upcoming):** replace singular `target:` with `targets:`; the warning prints the exact value.
  Replace the `vscode` target with `copilot`. Copilot project MCP servers move from
  `.github/mcp.json` to root `.mcp.json`, shared with Claude Code; `apm install` moves the
  entries APM wrote, then review static `env:` values. With `default_host:` set, write GitHub
  packages as `github.com/owner/repo`.
- **0.36:** `apm install --frozen` also fails when a deployed file differs from its locked hash.
  Save local edits with `apm export-patch`, run `apm install`, and commit.
- **Shared skill folder:** skills for Copilot, Cursor, OpenCode, Codex, Gemini and Windsurf
  deploy to `.agents/skills/`. Your next `apm install` moves the files APM recorded and
  updates the lockfile; `--legacy-skill-paths` keeps the old per-harness folders.
- **From `awd-cli`:** uninstall it (`pip uninstall awd-cli`), install APM, and rename
  `awd.yml` to `apm.yml`. Then run `apm install` to regenerate the lockfile.

## Undo the move

APM writes alongside your existing files and does not move them. To back out, run
`apm uninstall <package>` for each package to remove the files APM wrote, then delete
`apm.yml` and `apm.lock.yaml`.
