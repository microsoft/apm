---
title: "Lifecycle Hooks"
description: "Run custom actions (shell commands, HTTP webhooks, scripts) at install, update, and uninstall time."
sidebar:
  order: 12
---

APM supports **lifecycle hooks** -- custom actions that fire automatically
at key moments during install, update, and uninstall operations. Hooks are
fire-and-forget: a failing hook never blocks the CLI.

## Supported events

| Event            | Fires when                           |
|------------------|--------------------------------------|
| `pre-install`    | Before the install pipeline runs     |
| `post-install`   | After a successful install completes |
| `pre-update`     | Before the update pipeline runs      |
| `post-update`    | After a successful update completes  |
| `pre-uninstall`  | Before uninstall begins              |
| `post-uninstall` | After a successful uninstall         |

## Hook types

### Shell command

Run an inline shell command:

```yaml
lifecycle_hooks:
  post-install:
    - type: command
      run: "echo 'installed' >> /tmp/apm.log"
```

### HTTP webhook

Send a JSON payload to an HTTPS endpoint:

```yaml
lifecycle_hooks:
  post-install:
    - type: webhook
      url: "https://analytics.internal.company.com/apm/events"
      token_env: "ANALYTICS_TOKEN"
```

- **HTTPS only** -- `http://` URLs are rejected.
- **No redirects** -- `allow_redirects=False`.
- **2-second timeout** -- the call runs in a daemon thread.
- **Bearer token** -- read from the env var named by `token_env`.

### Script file

Execute a script under the project root:

```yaml
lifecycle_hooks:
  post-install:
    - type: script
      path: ".apm/hooks/post-install.sh"
```

Scripts must be within the project root (path traversal is rejected).

## Configuration levels

Hooks can be declared at three levels. They are merged at runtime --
policy hooks run first and cannot be removed by the project.

### 1. Project (`apm.yml`)

```yaml
# apm.yml
lifecycle_hooks:
  post-install:
    - type: webhook
      url: "https://analytics.corp.net/apm"
      token_env: "APM_ANALYTICS_TOKEN"
```

### 2. Global (`~/.apm/config.json`)

```json
{
  "lifecycle_hooks": {
    "post-install": [
      { "type": "command", "run": "echo installed" }
    ]
  }
}
```

### 3. Policy (`apm-policy.yml`)

Policy-level hooks are enforced organisation-wide:

```yaml
# apm-policy.yml
lifecycle_hooks:
  require:
    post-install:
      - type: webhook
        url: "https://analytics.internal.company.com/apm/events"
        token_env: "ANALYTICS_TOKEN"
  deny_types:
    - script
```

`deny_types` blocks specific hook types across the organisation.

## Event payload

All hook types receive the same event data. Webhooks get it as a JSON
body; commands and scripts receive it in the `APM_HOOK_EVENT` environment
variable (JSON-encoded).

```json
{
  "schema_version": 1,
  "event": "post-install",
  "packages": [
    { "name": "org/repo", "reference": "v1.0.0" }
  ],
  "scope": "project",
  "timestamp": "2026-06-13T14:50:15Z",
  "cli_version": "0.14.1"
}
```

## Analytics use case

The canonical use case for lifecycle hooks is installation analytics.
An enterprise platform team can deploy an org-wide webhook via policy
to track which packages are actively used:

```yaml
# apm-policy.yml
lifecycle_hooks:
  require:
    post-install:
      - type: webhook
        url: "https://analytics.internal.company.com/apm/events"
        token_env: "APM_ANALYTICS_TOKEN"
    post-uninstall:
      - type: webhook
        url: "https://analytics.internal.company.com/apm/events"
        token_env: "APM_ANALYTICS_TOKEN"
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

- Webhook URLs must use `https://`.
- Bearer tokens are never stored in config -- they are read from env
  vars at runtime.
- Script paths are validated to stay within the project root.
- All hooks are fire-and-forget with a short timeout (2s for webhooks,
  30s for commands/scripts).
- Hook failures are logged in verbose mode (`--verbose`) and never
  block the CLI.
