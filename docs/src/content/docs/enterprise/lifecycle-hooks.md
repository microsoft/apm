---
title: "Lifecycle Hooks"
description: "Run custom actions (shell commands, HTTP webhooks) at install, update, and uninstall time."
sidebar:
  order: 12
---

APM supports **lifecycle hooks** -- custom actions that fire automatically
at key moments during install, update, and uninstall operations. Hooks are
fire-and-forget: a failing hook never blocks the CLI.

Hooks are defined in standalone JSON files and discovered from well-known
directories, following the same pattern as GitHub Copilot CLI hooks.

## Supported events

| Event            | Fires when                           |
|------------------|--------------------------------------|
| `pre-install`    | Before the install pipeline runs     |
| `post-install`   | After a successful install completes |
| `pre-update`     | Before the update pipeline runs      |
| `post-update`    | After a successful update completes  |
| `pre-uninstall`  | Before uninstall begins              |
| `post-uninstall` | After a successful uninstall         |

## Hook file format

Hook files are JSON with a versioned schema:

```json
{
  "version": 1,
  "hooks": {
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

## Hook types

### Command

Run a shell command. The event payload is piped via **stdin** as JSON:

```json
{
  "type": "command",
  "bash": "./scripts/notify.sh",
  "cwd": "./scripts",
  "env": { "LOG_LEVEL": "INFO" },
  "timeoutSec": 10
}
```

Fields:
- `bash` -- command string for bash (use this on Linux/macOS)
- `command` -- fallback command string (cross-platform)
- `cwd` -- working directory (relative paths resolve against project root)
- `env` -- extra environment variables merged into the process env
- `timeoutSec` -- execution timeout (default: 30s)

If both `bash` and `command` are present, `bash` takes priority.

### HTTP

Send a JSON POST to an HTTPS endpoint:

```json
{
  "type": "http",
  "url": "https://analytics.corp.net/apm/events",
  "headers": { "Authorization": "Bearer $APM_ANALYTICS_TOKEN" },
  "timeoutSec": 5
}
```

Fields:
- `url` -- HTTPS endpoint (**http:// is rejected**)
- `headers` -- request headers; values support `$ENV_VAR` expansion
- `timeoutSec` -- request timeout (default: 10s)

Security:
- **HTTPS only** -- `http://` URLs are rejected
- **No redirects** -- redirect following is disabled
- Headers support env-var expansion (`$VAR` or `${VAR}`)

## Discovery locations

Hook files are loaded from three directories. All files are **additive** --
every hook from every file runs. Policy hooks cannot be disabled.

| Priority     | Path                        | Who controls     |
|--------------|-----------------------------|------------------|
| 1 (highest)  | `/etc/apm/policy.d/*.json`  | Platform/IT team |
| 2            | `~/.apm/hooks/*.json`       | Individual user  |
| 3            | `.apm/hooks.json`           | Project          |

Policy and user sources are directories (all `*.json` files are loaded).
The project source is a single file.

## Event payload

Command hooks receive JSON on **stdin**. HTTP hooks receive it as the
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

## Analytics use case

The canonical use case for lifecycle hooks is installation analytics.
An enterprise platform team can deploy an org-wide webhook via the
policy directory to track which packages are actively used:

Create `/etc/apm/policy.d/analytics.json`:

```json
{
  "version": 1,
  "hooks": {
    "post-install": [
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

- HTTP hook URLs must use `https://`.
- Tokens are never stored in hook files -- use env-var expansion in headers.
- All hooks are fire-and-forget with configurable timeouts (10s for HTTP,
  30s for commands by default).
- Hook failures are logged in verbose mode (`--verbose`) and never
  block the CLI.

## Hook output log

Hook stdout, stderr, and execution status are appended to a log file at
`~/.apm/logs/hooks.log` (or `$APM_HOME/logs/hooks.log`). This lets
administrators audit hook behaviour without enabling verbose CLI output.

Each entry includes a UTC timestamp, event name, hook type, target
command or URL, status, exit code (for commands), and any captured output:

```
[2026-06-16T08:25:43Z] event=pre-install type=command target=echo 'Check passed' status=ok exit_code=0
  stdout: Check passed

[2026-06-16T08:25:44Z] event=post-install type=http target=https://analytics.corp.net/events status=ok
  stdout: HTTP 200
```

The log file is created automatically on first hook execution.

## CLI commands

APM provides three commands to work with lifecycle hooks:

### ``apm hooks`` -- list discovered hooks

Run without a sub-command to see all hooks discovered from policy, user,
and project directories:

```bash
apm hooks
```

### ``apm hooks init`` -- scaffold a starter hook file

Generate a starter JSON hook file at ``.apm/hooks.json``:

```bash
apm hooks init            # creates .apm/hooks.json
apm hooks init --force    # overwrite existing file
```

### ``apm hooks validate`` -- check hook files for errors

Validate all discovered hook files across policy, user, and project
directories. Reports schema errors, unknown events, missing fields, and
non-HTTPS URLs:

```bash
apm hooks validate
```

Exits with a non-zero code if any errors are found.

### ``apm hooks test`` -- dry-run a synthetic event

Fire a synthetic event through all discovered hooks to verify wiring
without performing a real install/update/uninstall:

```bash
apm hooks test                    # fires post-install (default)
apm hooks test pre-uninstall      # fires a specific event
```

Hook output is written to ``~/.apm/logs/hooks.log`` as usual.
