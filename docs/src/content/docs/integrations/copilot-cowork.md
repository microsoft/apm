---
title: "Microsoft 365 Copilot Cowork (Experimental)"
description: "Deploy APM skills to Microsoft 365 Copilot Cowork through a OneDrive-synchronised skills folder."
sidebar:
  order: 4
---

:::caution[Frontier preview]
This integration is experimental and off by default. You must enable the `copilot-cowork` flag before using it.

```bash
apm experimental enable copilot-cowork
```

Until the flag is enabled, the `copilot-cowork` target stays inert: it is hidden from active target detection, and explicit `--target copilot-cowork` installs fail cleanly instead of deploying anything.
:::

## What it does

When the `copilot-cowork` flag is enabled, APM can deploy package skills to Microsoft 365 Copilot Cowork at user scope. APM writes each deployed skill to Cowork's fixed OneDrive convention:

```text
<onedrive-root>/Documents/Cowork/skills/<package-name>/SKILL.md
```

## Enable the flag

```bash
apm experimental enable copilot-cowork
apm experimental list
apm experimental disable copilot-cowork
```

Use `apm experimental list` to confirm whether `copilot-cowork` is enabled on the current machine.

## OneDrive auto-detection

Resolution is first match wins:

1. If `APM_COPILOT_COWORK_SKILLS_DIR` is set, APM uses that path as-is.
2. Otherwise if `apm config set copilot-cowork-skills-dir` has stored a path, APM uses that persisted value.
3. Otherwise APM falls back to platform-specific detection.

| Platform | Resolution |
|----------|------------|
| macOS | Search `~/Library/CloudStorage/OneDrive*`. One match is used. No matches means Cowork is unavailable. Two or more matches fail with an actionable error that lists the candidates and recommends `APM_COPILOT_COWORK_SKILLS_DIR`. |
| Windows | Use `%ONEDRIVECOMMERCIAL%`, then `%ONEDRIVE%`. |
| Linux | No default lookup. Set `APM_COPILOT_COWORK_SKILLS_DIR` or persist the path with `apm config set copilot-cowork-skills-dir ...`. |

When APM finds a OneDrive root, it always deploys to `Documents/Cowork/skills/` under that root.

## APM_COPILOT_COWORK_SKILLS_DIR override

Set `APM_COPILOT_COWORK_SKILLS_DIR` when you need to bypass auto-detection, such as:

- a non-standard OneDrive install
- a multi-tenant macOS machine
- Linux, where there is no platform default

Example:

```bash
export APM_COPILOT_COWORK_SKILLS_DIR="$HOME/Library/CloudStorage/OneDrive - Contoso/Documents/Cowork/skills"
```

## Persisting the skills directory

Use `apm config` when you want the Cowork skills path to persist across shells. This is especially useful on Linux, where there is no auto-detection and you would otherwise need to export `APM_COPILOT_COWORK_SKILLS_DIR` in every shell.

Set a persisted path:

```bash
apm experimental enable copilot-cowork
apm config set copilot-cowork-skills-dir "$HOME/OneDrive/Documents/Cowork/skills"
```

`apm config set copilot-cowork-skills-dir` requires the `copilot-cowork` experimental flag. APM expands `~`, rejects empty or whitespace-only values, and rejects relative paths. The path does not need to exist yet, which is useful while OneDrive is still synchronising.

Inspect the stored value:

```bash
apm config get copilot-cowork-skills-dir
```

`apm config get copilot-cowork-skills-dir` works whether or not the `copilot-cowork` flag is enabled, and prints the stored path or `Not set`.

Clear the persisted path:

```bash
apm config unset copilot-cowork-skills-dir
```

`apm config unset copilot-cowork-skills-dir` also works whether or not the `copilot-cowork` flag is enabled.

## Install

Cowork is user-scope only. Use `--global`, and add `--target copilot-cowork` when you want to target Cowork explicitly.

```bash
apm install --global
apm install --target copilot-cowork --global
```

Cowork deployments are skills only:

```text
.apm/skills/<name>/SKILL.md
-> <onedrive-root>/Documents/Cowork/skills/<name>/SKILL.md
```

If you try project scope, APM stops with a clean error that tells you to rerun with `--global`.

## Skills-only behaviour

Cowork deploys only `SKILL.md` content. Instructions, agents, prompts, hooks, commands, chatmodes, and MCP material are skipped for this target.

If any selected package contains non-skill primitives, APM emits one `[!]` summary warning for the whole install run. The install still succeeds, and the skill content still deploys.

## Caps

Cowork limits are warn-only. They never block install:

- More than 50 skills in the Cowork directory after install -> one `[!]` warning recommending review.
- Any individual `SKILL.md` larger than 1 MiB -> one `[!]` warning for that file.

## Lockfile representation

In `apm.lock.yaml`, Cowork-deployed paths are recorded as synthetic URIs such as:

```text
cowork://skills/my-skill/SKILL.md
```

This keeps the lockfile portable across machines, users, and OneDrive tenants. APM translates between `cowork://skills/...` and absolute filesystem paths only at I/O boundaries; internal install logic still works with absolute `Path` objects.

## Troubleshooting

- Cowork unavailable or no OneDrive detected: confirm OneDrive is installed and synchronising, then set `APM_COPILOT_COWORK_SKILLS_DIR`.
- macOS multi-tenant error: set `APM_COPILOT_COWORK_SKILLS_DIR` to the account you want to use.
- Linux: set `APM_COPILOT_COWORK_SKILLS_DIR` or persist the path with `apm config set copilot-cowork-skills-dir ...`.
- Path still persists after disabling `copilot-cowork`: run `apm config unset copilot-cowork-skills-dir` to remove the stored value.
- Project-scope error: rerun with `--global`.
- Non-skill primitives skipped: expected behaviour. Cowork only deploys skills.

See also [IDE and Tool Integration](../ide-tool-integration/) and [apm experimental](../../reference/experimental/).
