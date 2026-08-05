---
title: "Microsoft 365 Copilot Cowork"
description: "Deploy APM skills to Microsoft 365 Copilot Cowork through a OneDrive-synchronised skills folder."
sidebar:
  order: 4
---

## What it does

APM deploys package skills to Microsoft 365 Copilot Cowork at user scope. APM writes each deployed skill to Cowork's fixed OneDrive convention:

```text
<onedrive-root>/Documents/Cowork/skills/<package-name>/SKILL.md
```

`copilot-cowork` is an **explicit-only** target. It is never auto-detected, and it is never included in `--target all`. You always ask for it by name, and always at user scope:

```bash
apm install --target copilot-cowork --global
```

No experimental flag is required.

## First-run setup on macOS

Corporate macOS machines very often mount more than one OneDrive root --
for example a personal tenant mount plus a shared-libraries mount:

```text
~/Library/CloudStorage/OneDrive-Contoso
~/Library/CloudStorage/OneDrive-SharedLibraries-Contoso
```

APM cannot guess which one holds your Cowork folder, so it refuses to
choose and lists the candidates. Check which root actually contains
`Documents/Cowork`, then persist it once:

```bash
ls -d ~/Library/CloudStorage/OneDrive*/Documents/Cowork
apm config set copilot-cowork-skills-dir \
  "$HOME/Library/CloudStorage/OneDrive-Contoso/Documents/Cowork/skills"
```

Treat this as normal setup rather than troubleshooting: if you have two
mounts, you will need it before your first install.

## OneDrive auto-detection

Resolution is first match wins:

1. If `APM_COPILOT_COWORK_SKILLS_DIR` is set, APM uses that path as-is.
2. Otherwise if `apm config set copilot-cowork-skills-dir` has stored a path, APM uses that persisted value.
3. Otherwise APM falls back to platform-specific detection.

| Platform | Resolution |
|----------|------------|
| macOS | Search `~/Library/CloudStorage/OneDrive*`. One match is used. No matches means Cowork is unavailable. Two or more matches fail with an actionable error that lists the candidates and recommends `APM_COPILOT_COWORK_SKILLS_DIR` -- see [First-run setup on macOS](#first-run-setup-on-macos). |
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
apm config set copilot-cowork-skills-dir "$HOME/OneDrive/Documents/Cowork/skills"
```

APM expands `~`, rejects empty or whitespace-only values, and rejects relative paths. The path does not need to exist yet, which is useful while OneDrive is still synchronising.

Inspect the stored value:

```bash
apm config get copilot-cowork-skills-dir
```

`apm config get copilot-cowork-skills-dir` prints the stored path or `Not set`.

Clear the persisted path:

```bash
apm config unset copilot-cowork-skills-dir
```

`apm config unset copilot-cowork-skills-dir` clears the value and restores auto-detection.

## Install

Cowork is user-scope only and explicit-only. You must name it and pass `--global`:

```bash
apm install --target copilot-cowork --global
```

Because it is explicit-only, `apm install --global` and `apm install --target all --global` do **not** deploy to Cowork.

Cowork deployments are skills only:

```text
.apm/skills/<name>/SKILL.md
-> <onedrive-root>/Documents/Cowork/skills/<name>/SKILL.md
```

Project-scope behaviour depends on how Cowork was selected:

- **Explicit `--target copilot-cowork` without `--global`** -- APM stops with a clean error telling you to rerun with `--global`.
- **Selected implicitly** (via `targets:` in `apm.yml`, or an `apm config target` default) -- APM emits one `[!]` warning, skips Cowork, and continues with the remaining targets.

This means you can list `copilot-cowork` in `apm.yml` alongside project targets: project-scope installs quietly skip it, and `apm install --global` picks it up.

## Verifying the delivery path

Getting a skill into `Documents/Cowork/skills/` does not by itself prove
that Cowork loaded it. The repository ships a package that closes that
gap: `packages/cowork-smoke-test/` contains a single skill whose only job
is to return a fixed sentinel token.

Install it at user scope, wait for OneDrive to finish syncing, then ask
Cowork to "run the APM Cowork smoke test". A reply containing
`APM-SENTINEL-7Q4X-COWORK-2F91` proves the skill was read, because that
token appears nowhere except in that file -- a model that never loaded it
cannot invent it.

For the negative control, remove the dependency, re-run the install (APM
cleans the orphaned file), and ask again **in a fresh conversation**. The
sentinel must now be unavailable. Re-using the old conversation is not a
valid check: the token is already in that history.

## Skills-only behaviour

Cowork deploys only `SKILL.md` content. Instructions, agents, prompts, hooks, commands, and MCP material are skipped for this target.

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
- macOS multi-tenant error: expected on machines with more than one OneDrive mount. See [First-run setup on macOS](#first-run-setup-on-macos).
- Linux: set `APM_COPILOT_COWORK_SKILLS_DIR` or persist the path with `apm config set copilot-cowork-skills-dir ...`.
- Path no longer wanted: run `apm config unset copilot-cowork-skills-dir` to remove the stored value.
- Project-scope error: rerun with `--global`, or let APM skip Cowork by selecting it through `apm.yml` instead of the CLI.
- Nothing deployed on `apm install --global`: Cowork is explicit-only. Pass `--target copilot-cowork`.
- Non-skill primitives skipped: expected behaviour. Cowork only deploys skills.

See also [IDE and Tool Integration](../ide-tool-integration/) and [Targets matrix](../../reference/targets-matrix/).
