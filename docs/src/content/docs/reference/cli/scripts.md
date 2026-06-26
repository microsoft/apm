---
title: apm scripts
description: Inspect, test, and scaffold lifecycle scripts for install/update/uninstall events.
sidebar:
  order: 22
---

Lifecycle scripts fire custom actions (shell commands, HTTPS webhooks) at key
moments during `apm install`, `apm update`, and `apm uninstall`. This command
group provides all tooling to discover, validate, test, scaffold, and manage
trust for lifecycle script files.

Project-source scripts (`apm-scripts.yml`) are **skipped by default** until
explicitly trusted, preventing arbitrary command execution on clone.

For the full conceptual guide, schema reference, and security model, see
[Lifecycle Scripts](../../../enterprise/lifecycle-scripts/).

## Synopsis

```bash
apm scripts
apm scripts init [--force]
apm scripts validate
apm scripts test [EVENT] [--verbose] [--execute]
apm scripts trust
apm scripts untrust
```

## Subcommands

### `apm scripts` (list)

List all lifecycle scripts discovered from policy, user, and project sources.
Equivalent to `apm scripts list`.

```bash
apm scripts
```

Output columns: event name, script type (`command` or `http`), target (command
string or URL), and source (`policy`, `user`, or `project`).

Returns an informational message when no scripts are discovered.

### `apm scripts init`

Scaffold a starter `apm-scripts.yml` file at the repo root (sibling of
`apm.yml`).

```bash
apm scripts init            # creates apm-scripts.yml at the repo root
apm scripts init --force    # overwrite an existing file
```

| Flag | Description |
|---|---|
| `--force` | Overwrite an existing `apm-scripts.yml`. |

### `apm scripts validate`

Validate all discovered script files (project `apm-scripts.yml`, admin/user
`*.json`) for schema errors.

```bash
apm scripts validate
```

Checks across all three discovery sources (policy, user, project). Reports:

- Missing or unsupported `version` field
- Missing `scripts` object
- Unknown lifecycle event names
- Unknown script types
- Missing `bash`/`command` for command scripts
- Missing or non-HTTPS `url` for HTTP scripts
- Embedded credentials in URLs

Exits `1` if any errors are found.

### `apm scripts test`

Fire a synthetic lifecycle event through all discovered scripts. Dry-run by
default: shows which scripts would run without executing them. Pass `--execute`
to actually run them.

```bash
apm scripts test                        # dry-run post-install (default event)
apm scripts test post-update            # dry-run a specific event
apm scripts test post-install --execute # actually run post-install scripts
apm scripts test pre-install -v         # verbose dry-run
```

| Flag | Description |
|---|---|
| `--execute` | Actually run the scripts. Default is a non-executing dry-run. |
| `--verbose`, `-v` | Show detailed output per script. |

Supported events: `pre-install`, `post-install`, `pre-update`, `post-update`,
`pre-uninstall`, `post-uninstall`. Default: `post-install`.

Note: `apm scripts test` bypasses the project-script trust gate -- it is an
explicit developer inspection tool for their own repository.

Script output is written to `~/.apm/logs/scripts.log`.

### `apm scripts trust`

Trust the project script file at `apm-scripts.yml` so its scripts run during
`apm install`, `apm update`, and `apm uninstall`.

```bash
apm scripts trust
```

Trust is bound to the exact file contents (SHA-256). Any edit to
`apm-scripts.yml` revokes trust and requires re-running this command.

Trust records are stored at `~/.apm/scripts-trust.json` (or
`$APM_HOME/scripts-trust.json`). To audit or reset trust manually, edit or
delete that file.

### `apm scripts untrust`

Revoke trust for `apm-scripts.yml`. Project scripts will stop running on
the next install/update/uninstall.

```bash
apm scripts untrust
```

## Environment variables

| Variable | Effect |
|---|---|
| `APM_NO_SCRIPTS=1` | Disable all lifecycle scripts for one invocation. Useful in CI and untrusted clone environments. |
| `APM_HOME` | Override the base directory for user scripts (`$APM_HOME/scripts/`) and trust store (`$APM_HOME/scripts-trust.json`). |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Error (validation failures, unreadable script file, could not record trust). |

## Security notes

- Project scripts are skipped until explicitly trusted (`apm scripts trust`).
- Org policy `executables.deny_all: true` suppresses all lifecycle scripts.
- Set `APM_NO_SCRIPTS=1` for a per-run disable without touching policy.
- HTTP script URLs must use `https://`.
- Credential-pattern environment variables (TOKEN, SECRET, PAT, KEY, etc.) are
  blocked from HTTP header expansion unless listed in `allowedEnvVars`.

See [Lifecycle Scripts - Security](../../../enterprise/lifecycle-scripts/#security-considerations)
and [Security and supply chain](../../../enterprise/security/) for the full security model.
