---
title: "Lifecycle Scripts"
description: "Run custom actions (shell commands, HTTP webhooks) at install, update, and uninstall time."
sidebar:
  order: 12
---

APM supports **lifecycle scripts** -- custom actions that fire automatically
at key moments during install, update, and uninstall operations. Scripts live
inside `apm.yml` under a `lifecycle:` key -- no separate config files to manage.

```yaml
# apm.yml
lifecycle:
  post-install:
    - type: command
      run: echo "Install complete"
    - type: http
      url: https://analytics.corp.net/events
```

Run `apm lifecycle init` to scaffold this block in the current project.
Project scripts require explicit trust before they run: `apm lifecycle trust`.
See the [CLI reference](../reference/cli/lifecycle/) for all subcommands.

A failing script never aborts the CLI operation. HTTP scripts dispatch in a
background thread (fire-and-forget), while command scripts run synchronously
and can delay the operation until they finish or their timeout elapses.

Scripts are defined in three tiers. The **project tier** uses the repository `apm.yml`
manifest under a top-level `lifecycle:` key. The **user tier** uses
`~/.apm/apm.yml` (or `$APM_HOME/apm.yml`) under the same `lifecycle:` key.
The **admin** tier remains `/etc/apm/policy.d/*.json`, suited for machine-
and fleet-managed deployment.

## Supported events

| Event            | Fires when                           |
|------------------|--------------------------------------|
| `pre-install`    | Before the install pipeline runs     |
| `post-install`   | After a successful install completes |
| `pre-update`     | Before the update pipeline runs      |
| `post-update`    | After a successful update completes  |
| `pre-uninstall`  | Before uninstall begins              |
| `post-uninstall` | After a successful uninstall         |

## Script file format

Project and user manifests embed lifecycle scripts under a top-level
`lifecycle:` key in `apm.yml`. The admin tier keeps the versioned JSON
`{version: 1, scripts: {...}}` wrapper in `/etc/apm/policy.d/*.json`. All
entries share the same field names and `type` discriminator.

Each entry declares its kind via `type: command` (shell subprocess) or
`type: http` (HTTPS webhook). An optional `description` field documents
the entry for reviewers and `apm lifecycle` output.

**Project/user tier -- `apm.yml` `lifecycle:` block:**

```yaml
lifecycle:
  post-install:
    - type: command
      description: "Set up local build deps"
      command: "make setup"
      timeoutSec: 30
    - type: http
      description: "Notify internal dashboard"
      url: "https://hooks.example.com/installed"
      headers:
        X-Token: "$APM_HOOK_TOKEN"
```

**Admin tier -- JSON (`policy.d/*.json`):**

```json
{
  "version": 1,
  "scripts": {
    "post-install": [
      {
        "type": "command",
        "bash": "./scripts/notify.sh",
        "timeoutSec": 10
      },
      {
        "type": "http",
        "url": "https://analytics.corp.net/apm/events",
        "headers": { "Authorization": "Bearer $APM_ANALYTICS_TOKEN" },
        "timeoutSec": 5
      }
    ]
  }
}
```

## Script types

### Command

Run a shell command. The event payload is piped via **stdin** as JSON:

```yaml
type: command
description: "Run post-install setup"
bash: "./scripts/notify.sh"
cwd: "./scripts"
env:
  LOG_LEVEL: "INFO"
timeoutSec: 10
```

Fields:
- `type` -- must be `command`
- `description` -- (optional) human annotation shown in `apm lifecycle`
- `bash` -- command string for bash (use this on Linux/macOS)
- `command` -- fallback command string (cross-platform)
- `cwd` -- working directory (relative paths resolve against project root)
- `env` -- extra environment variables merged into the process env
- `timeoutSec` -- execution timeout (default: 30s)

If both `bash` and `command` are present, `bash` takes priority.

### HTTP

Send a JSON POST to an HTTPS endpoint:

```yaml
type: http
description: "Ping analytics dashboard"
url: "https://analytics.corp.net/apm/events"
headers:
  Authorization: "Bearer $APM_ANALYTICS_TOKEN"
timeoutSec: 5
```

Fields:
- `type` -- must be `http`
- `description` -- (optional) human annotation shown in `apm lifecycle`
- `url` -- HTTPS endpoint (**http:// is rejected**)
- `headers` -- request headers; values support `$ENV_VAR` expansion
- `timeoutSec` -- request timeout (default: 10s)

Security:
- **HTTPS only** -- `http://` URLs are rejected
- **No redirects** -- redirect following is disabled
- Headers support env-var expansion (`$VAR` or `${VAR}`)
- **Credential denylist** -- variable names matching `TOKEN`, `SECRET`, `PAT`,
  `KEY`, `PASSWORD`, `CREDENTIAL`, or `AUTHTOKEN` patterns are blocked from
  expansion by default. To opt in, add the variable name to `allowedEnvVars`:

  ```yaml
  type: http
  url: "https://analytics.corp.net/apm/events"
  headers:
    Authorization: "Bearer $APM_ANALYTICS_TOKEN"
  allowedEnvVars:
    - APM_ANALYTICS_TOKEN
  ```

## Discovery locations

Script files are loaded from three directories. All files are **additive** --
every script from every file runs. Policy scripts cannot be disabled.

| Priority     | Path                        | Who controls     | Format |
|--------------|-----------------------------|------------------|--------|
| 1 (highest)  | `/etc/apm/policy.d/*.json`  | Platform/IT team | JSON   |
| 2            | `~/.apm/apm.yml`            | Individual user  | YAML   |
| 3            | `apm.yml` `lifecycle:`      | Project          | YAML   |

Policy is a directory of JSON files. User and project sources are single
`apm.yml` files that embed the `lifecycle:` subtree.

## Event payload

Command scripts receive JSON on **stdin**. HTTP scripts receive it as the
POST body.

```json
{
  "schema_version": 1,
  "event": "post-install",
  "packages": [
    { "name": "org/repo", "reference": "v1.0.0" }
  ],
  "scope": "project",
  "timestamp": "2026-06-13T14:50:15Z",
  "cli_version": "0.14.1",
  "working_directory": "/path/to/project"
}
```

## Trust model

Lifecycle scripts from different sources are subject to different trust rules:

- **Policy scripts** (`/etc/apm/policy.d/*.json`) -- controlled by your
  platform/IT team. Run without any consent gate; they cannot be disabled
  by the developer.
- **User scripts** (`~/.apm/apm.yml`) -- controlled by the developer.
  Run without a gate.
- **Project scripts** (`apm.yml` `lifecycle:`) -- committed into the repo and
  **skipped by default.** Cloning an untrusted repo and running
  `apm install` would otherwise execute attacker-controlled shell commands.
  Trust is explicit, lifecycle-subtree-bound, and revocable:
  - Run `apm lifecycle trust` to record trust for the current `lifecycle:` block.
  - Any edit to `lifecycle:` revokes trust and requires re-approval.
  - Editing other `apm.yml` keys does not revoke trust.
  - Run `apm lifecycle untrust` to revoke without editing the file.
  - Trust records are stored in `~/.apm/scripts-trust.json` (or
    `$APM_HOME/scripts-trust.json`).

Two environment-level kill-switches are also available:

- `APM_NO_SCRIPTS=1` -- disables all lifecycle scripts for one run. Useful
  in CI and untrusted clone environments.
- Org policy `executables.deny_all: true` -- when set in `apm-policy.yml`,
  suppresses all lifecycle scripts as a one-directional safety ceiling. An
  org that has locked down all executable primitives will also have lifecycle
  scripts suppressed automatically.

## Analytics use case

The canonical use case for lifecycle scripts is installation analytics.
An enterprise platform team can deploy an org-wide webhook via the
policy directory to track which packages are actively used:

Create `/etc/apm/policy.d/analytics.json`:

```json
{
  "version": 1,
  "scripts": {
    "post-install": [
      {
        "type": "http",
        "url": "https://analytics.internal.company.com/apm/events",
        "headers": { "Authorization": "Bearer $APM_ANALYTICS_TOKEN" }
      }
    ],
    "post-update": [
      {
        "type": "http",
        "url": "https://analytics.internal.company.com/apm/events",
        "headers": { "Authorization": "Bearer $APM_ANALYTICS_TOKEN" }
      }
    ],
    "post-uninstall": [
      {
        "type": "http",
        "url": "https://analytics.internal.company.com/apm/events",
        "headers": { "Authorization": "Bearer $APM_ANALYTICS_TOKEN" }
      }
    ]
  }
}
```

Set the token in CI:

```bash
export APM_ANALYTICS_TOKEN="your-bearer-token"
apm install
```

The webhook receives a JSON payload for every install and uninstall,
enabling dashboards that show adoption, version drift, and removal
trends -- without any changes to individual project configurations.

## Security considerations

- HTTP script URLs must use `https://`.
- Tokens are never stored in script files -- use env-var expansion in headers.
- Scripts have configurable timeouts (10s for HTTP, 30s for commands by
  default). HTTP scripts dispatch in the background; command scripts run
  synchronously and can delay the operation up to their timeout.
- A script failure never aborts the CLI operation; failures are logged in
  verbose mode (`--verbose`).

## Script output log

Script stdout, stderr, and execution status are appended to a log file at
`~/.apm/logs/scripts.log` (or `$APM_HOME/logs/scripts.log`). This lets
administrators audit script behaviour without enabling verbose CLI output.

Each entry includes a UTC timestamp, event name, script type, target
command or URL, status, exit code (for commands), and any captured output:

```
[2026-06-16T08:25:43Z] event=pre-install type=command target=echo 'Check passed' status=ok exit_code=0
  stdout: Check passed

[2026-06-16T08:25:44Z] event=post-install type=http target=https://analytics.corp.net/events status=ok
  stdout: HTTP 200
```

The log file is created automatically on first script execution.

## CLI commands

For the full command reference, see [apm lifecycle](../reference/cli/lifecycle/).

Key workflows:

```bash
apm lifecycle init              # scaffold lifecycle: block in apm.yml
apm lifecycle trust             # trust current lifecycle: subtree
apm lifecycle validate          # check all scripts for schema/config errors
apm lifecycle test post-install # dry-run: preview what would fire (no execution)
apm lifecycle test post-install --execute  # fire the event for real
apm lifecycle untrust           # revoke trust; scripts stop running
```

The `test` command defaults to a **dry-run** (no commands or HTTP requests run)
and shows the trust status of the project lifecycle block. Add `--execute` to
actually fire the event -- useful for verifying wiring before the first real install.
