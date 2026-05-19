---
title: "GitHub Copilot App workflows (Experimental)"
description: "Deploy APM prompts with schedule frontmatter as Copilot App workflows backed by the desktop SQLite store."
sidebar:
  order: 5
---

:::caution[Frontier preview]
This integration is experimental and off by default. You must enable the `copilot_app` flag before using it.

```bash
apm experimental enable copilot-app
```

Until the flag is enabled, the `copilot-app` target stays inert: it is hidden from auto-detection, and explicit `--target copilot-app` installs fail cleanly with the enable hint instead of touching the App's database.
:::

## What it does

When `copilot_app` is enabled and a package ships a prompt with a `schedule:` frontmatter block, `apm install --target copilot-app --global` inserts the prompt as a row in the GitHub Copilot desktop App's SQLite store at `~/.copilot/data.db`. The App reads new rows on next launch (or refresh) and lists them under Workflows.

Prompts that do not carry `schedule:` are skipped silently at this target — they continue to deploy to file-based targets (`copilot`, `vscode`, `claude`, ...) without changes.

## Why a new target

The `copilot` target writes prompts as files (`.github/prompts/<name>.prompt.md`) for Copilot in IDEs. The desktop App stores workflows in a SQLite database, not on disk. They are different surfaces; `copilot-app` exists so that one APM install can serve both without leakage.

## Authoring a scheduled prompt

Add a `schedule:` block to any `.prompt.md` file in your package's `.apm/prompts/` folder:

```markdown
---
name: Daily Digest
schedule:
  interval: daily         # one of: manual, hourly, daily, weekly
  schedule_hour: 9        # 0-23, UTC; ignored for manual / hourly
  schedule_day: 1         # 0-6 (weekly only)
  mode: interactive       # one of: interactive, plan, autopilot
  model: claude-opus-4.7  # optional
  reasoning_effort: high  # optional
---

Summarise yesterday's commits across all open PRs ...
```

`autopilot` mode is policy-blocked for third-party packages by default — third-party packages cannot auto-run anything on your machine without you flipping the App-side toggle.

## Lifecycle

| `apm` action | Effect on `~/.copilot/data.db` |
|---|---|
| `apm install` | INSERT row with `enabled = 0` (always disabled on install — you opt in). |
| `apm install` (already installed) | UPDATE prompt text / schedule / mode. `enabled`, `last_run_at`, `next_run_at` are preserved. |
| `apm uninstall` | DELETE only APM-namespaced rows (`apm--<owner>--<pkg>--<prompt>`). User-authored rows are never touched. |
| `apm list` | Reports APM-managed workflows alongside other primitives. |

## Enable and check

```bash
apm experimental enable copilot-app
apm experimental list
apm experimental disable copilot-app
```

## Database resolution

| Order | Source |
|---|---|
| 1 | `APM_COPILOT_APP_DB` environment variable (absolute path; used as-is). |
| 2 | `~/.copilot/data.db` if it exists. |

If neither resolves, the install fails with `[x] GitHub Copilot desktop App not detected. Expected ~/.copilot/data.db ...` and the command exits 1.

## "Auth" model

There is none. The DB file is local; access is governed by your filesystem permissions. APM never sends credentials or syncs the DB anywhere. Treat the DB as user-scope state.

## Schema compatibility

APM guards writes with `PRAGMA user_version`. The current tested version is `13`. If the App ships a newer schema, APM refuses to write and asks you to update APM rather than risk corruption.

## Concurrency

APM opens the DB in WAL mode and retries briefly when the App holds a write lock. If a lock cannot be acquired after the retry window, the install fails with `[!] Copilot App database is locked. Try again with the App closed.`

## Lockfile entries

Deployed rows are tracked in the project / user lockfile under the `copilot-app-db://workflows/<namespaced-id>` URI scheme. Standard sync semantics apply: lockfile drift triggers redeploy; removal from lockfile triggers row delete on next install.

## Out of scope (today)

- Package signing for `mode: autopilot` (planned).
- Scheduled-execution-on-install (deliberately not implemented — first-run is always manual).
- `gh-aw` outer-loop target (separate roadmap).
