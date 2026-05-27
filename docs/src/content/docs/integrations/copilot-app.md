---
title: "GitHub Copilot App workflows (Experimental)"
description: "Deploy APM prompts with schedule frontmatter as Copilot App workflows backed by the desktop SQLite store."
sidebar:
  order: 6
---

See the [Targets matrix](../../reference/targets-matrix/) for where `copilot-app` fits alongside the other deploy targets.

:::caution[Frontier preview]
This integration is experimental and off by default. You must enable the `copilot-app` flag before using it.

```bash
apm experimental enable copilot-app
```

See the [Experimental flags reference](../../reference/experimental/) for the full `apm experimental` subcommand surface (enable / disable / list).

Until the flag is enabled, the `copilot-app` target stays inert: it is hidden from auto-detection, and explicit `--target copilot-app` installs fail cleanly with the enable hint instead of touching the App's database.
:::

## What you get

Your APM workflows appear in the Copilot App's correct project tab automatically -- no manual workspace tagging. Ship a `.prompt.md` with workflow frontmatter (any of `interval`, `schedule_hour`, `schedule_day` at the top level), run `apm install --target copilot-app` inside the repo, and the App lists the workflow under that repo's Workflows tab on next launch. Add `--global` to install from a user-scope `~/.apm/apm.yml`; omit it for project-scoped, team-shared scheduled prompts.

Prompts that do not carry workflow frontmatter are plain slash commands: they deploy to file-based targets (`copilot`, `vscode`, `claude`, ...) and APM hard-errors with an actionable diagnostic if you point them at `copilot-app` directly. A single `.prompt.md` belongs to exactly ONE surface -- whichever its frontmatter shape selects.

:::note[How it works]
The desktop App stores workflows in a SQLite database at `~/.copilot/data.db`, not as files on disk -- so `copilot-app` is a separate target from the file-based `copilot` integration. `apm install --target copilot-app` inserts one row per workflow prompt; the App picks them up on next launch or refresh. If a freshly installed workflow does not appear, restart the Copilot App once.
:::

## Authoring a workflow prompt

:::note[Shape predicate]
Only `interval`, `schedule_hour`, and `schedule_day` at the top level mark a `.prompt.md` as a workflow. Setting `mode:`, `model:`, or `reasoning_effort:` alone keeps it a plain VSCode-style prompt (deploys to `copilot`, `claude`, etc.) -- those keys are accepted on workflows but never trigger workflow routing on their own.
:::

Add workflow frontmatter (flat top-level keys) to any `.prompt.md` file in your package's `.apm/prompts/` folder:

```markdown
---
name: Daily Digest
interval: daily            # one of: manual, hourly, daily, weekly
schedule_hour: 9           # 0-23, UTC; ignored for manual / hourly
schedule_day: 1            # 0-6 (weekly only)
mode: interactive          # one of: interactive, plan
model: claude-opus-4.7     # optional
reasoning_effort: high     # optional
---

Summarise yesterday's commits across all open PRs ...
```

Manual-only workflows omit `schedule_hour` / `schedule_day` and set
`interval: manual` (the default when any other execution-shape key is
present). The Copilot App provides a "run now" affordance for every
workflow, so manual-only is a useful shape — no schedule, just a
named, parameterised prompt the user can fire from the App UI.

The Copilot App also defines an `autopilot` mode, but APM intentionally
does NOT accept it via this target. Until package signing ships, a
third-party package could declare `mode: autopilot` and have the App
auto-run the prompt the moment you flip the in-App enable toggle.
Refusing autopilot at the writer is the secure-by-default behaviour;
you can still set autopilot yourself on a per-row basis from the App
UI after install.

## Lifecycle

| `apm` action | Effect on `~/.copilot/data.db` |
|---|---|
| `apm install` | INSERT row with `enabled = 0` (always disabled on install — you opt in). |
| `apm install` (already installed, content unchanged) | UPDATE display fields only. `enabled`, `last_run_at`, `next_run_at` are preserved. |
| `apm install` (already installed, any execution-affecting field changed) | UPDATE row; reset `enabled = 0`; clear `next_run_at`. |
| `apm uninstall` | DELETE only APM-namespaced rows (`apm--<owner>--<pkg>--<prompt>`). User-authored rows are never touched. |

Execution-affecting fields are the prompt body, schedule (`interval` / `schedule_hour` / `schedule_day`), `mode`, `model`, and `reasoning_effort`. The reset is by design: you opted in to a specific prompt, so any change to what runs or when is a new consent surface.

Removing the source `.prompt.md` from a package and re-syncing drops the lockfile entry but does NOT delete the corresponding row from `~/.copilot/data.db` -- it merely orphans it. Run `apm uninstall <pkg>` to remove the row.

## Project scoping

Workflows are scoped to a real Copilot App project so "Run now" in the Workflows tab CWDs into the right repository and the row groups under the correct sidebar entry. APM resolves the project once per install, then stamps every workflow row's `project_id`.

Two resolution paths run in this order:

| Order | When it fires | What it does |
|---|---|---|
| 1 | The Copilot App is running and is reachable on its loopback WebSocket (`~/.copilot/run/ws.{port,token}`, 0o600). | APM calls the App's own `create_project_from_path` over IPC. The App runs full discovery (GitHub owner/repo detection, default branch, account binding) and the resulting project is immediately known to the webview, so opening the Workflows tab cannot hit the white-screen-on-unknown-project failure mode. |
| 2 | The Copilot App is closed, OR the WebSocket surface is unreachable for any reason (stale token after restart, etc). | APM falls back to a direct-SQLite `BEGIN IMMEDIATE` resolver: SELECT by `main_repo_path` (UNIQUE), INSERT if missing. |

In both paths the workflow rows are written via direct SQLite -- the WS surface is currently project-registration only -- so lockfile ids stay namespaced and stable (`apm--<owner>--<pkg>--<prompt>`) across runs and across the WS-vs-SQLite branch.

If APM cannot derive a repo context at all (no `.git/`, no `origin`, etc) the workflows install with `project_id = NULL`. You can attach them to any project after the fact from the App's Workflows tab.

### One-time restart hint

The first time APM registers a brand-new project for a given repository, install prints:

```
[i] Registered a new Copilot App project for this repo. Restart the Copilot App once so the new project appears in the UI (see github/github-app#5483).
```

Subsequent installs into the same repo are silent. The hint is upstream-bug guidance: the App's webview does not currently refresh its projects sidebar when a new `projects` row appears mid-session, so one restart wires the new project into the UI. Once the App learns about the project, neither the install nor the App needs to be restarted again.

## `--global` and workflow-shape prompts

Workflows installed with `apm install --global` run with `CWD=~/.copilot`, not a repository -- which is almost never what the user wants. APM still deploys global workflows (so global skills and commands keep working), but emits a one-time warn-and-proceed diagnostic whenever a `--global` install carries any workflow-shape prompt:

```
[!] Copilot App workflows installed with --global run with CWD=~/.copilot, not a project. Attach the workflow to a project from the App's Workflows tab to fix this, or re-run `apm install` from a repo without --global.
```

The remediation is per-row: attach the workflow to any project from the Workflows tab in the App. Or re-run `apm install` from inside a repo without `--global`.

## Enable and check

Use `apm experimental enable copilot-app` to turn the target on, `apm experimental list` to see all flags, and `apm experimental disable copilot-app` to turn it off again. See the [Experimental flags reference](../../reference/experimental/) for the complete subcommand surface.

## Database resolution

| Order | Source |
|---|---|
| 1 | `APM_COPILOT_APP_DB` environment variable (absolute path; used as-is). |
| 2 | `~/.copilot/data.db` if it exists. |

If neither resolves, the install fails with `[x] GitHub Copilot desktop App not detected. Expected ~/.copilot/data.db ...` and the command exits 1.

## "Auth" model

There is none. The DB file is local; access is governed by your filesystem permissions. APM never sends credentials or syncs the DB anywhere. Treat the DB as user-scope state.

## Schema compatibility

APM guards writes with `PRAGMA user_version`. Below the supported minimum (`13`) APM refuses to write, because the `workflows` table may not exist. Within the tested range (`13`–`15` today) writes proceed normally. Above the tested maximum APM warns once and continues -- additive schema bumps are forward-compatible against APM's narrow write surface (the `workflows` table), so a hard fail would block users unnecessarily. If a future schema is found to break reads, that specific version will be added to a known-breaking list and hard-fail again.

## Concurrency

The Copilot App owns the DB and keeps it in WAL mode while running. APM coexists with the App's writer connection by issuing `BEGIN IMMEDIATE` with a bounded retry; if a lock cannot be acquired after the retry window, the install fails with a `[!]` warning noting that the Copilot App DB stayed locked and asking you to close the GitHub Copilot app momentarily and retry.

## Lockfile entries

Deployed rows are tracked in the project / user lockfile under the `copilot-app-db://workflows/<namespaced-id>` URI scheme. Standard sync semantics apply: lockfile drift triggers redeploy; removal from lockfile triggers row delete on next install.

## Out of scope (today)

- Package signing (would unlock additional trust-gated capabilities such as `mode: autopilot`).
- Scheduled-execution-on-install (deliberately not implemented — first-run is always manual).
- `gh-aw` outer-loop target (separate roadmap).
