---
title: apm view
description: Inspect package metadata or list package versions
sidebar:
  order: 5
---

Show local metadata for an installed package, or query package versions without cloning.

## Synopsis

```bash
apm view PACKAGE [FIELD] [OPTIONS]
```

`apm info` is accepted as a hidden alias for backward compatibility.

## Description

`apm view` has two modes, selected by the optional `FIELD` argument:

- **No field** -- read installed package metadata from `apm_modules/` (or `~/.apm/apm_modules/` with `-g`). Local-only; the package must be installed.
- **`versions` field** -- query available versions. Git packages list remote tags and branches without requiring a local install. Registry packages list version and published-timestamp columns. Registry routing uses the first matching signal: (1) `--registry [NAME]` flag, (2) `apm.lock.yaml` records the package as a registry dependency, (3) a default registry is configured and the package reference is a plain shorthand (e.g., `owner/repo`). Use a full git URL to force the git path when a default registry is configured.

When `PACKAGE` matches the `NAME@MARKETPLACE` pattern, `apm view` resolves the plugin against the marketplace manifest and prints its entry (name, version, description, source, tags) instead of a Git repository view. This applies whether or not `versions` is passed.

See [`apm outdated`](../outdated/) to compare locked versions against remotes, and [`apm install`](../install/) to add a package to the manifest.

## Subcommands

### `apm view <package>`

Reads metadata from the installed copy. Exits non-zero if `apm_modules/` is missing or the package is not installed; on a missing package, prints the list of installed packages to help disambiguate.

Output includes: name, version, description, author, source, install path, lockfile ref and commit (when available), context-file counts (skills, prompts, instructions), workflow count, and hook count.

### `apm view <package> versions`

Lists available versions for the package. Calls the remote -- requires network access.

For git packages, output is a table with name, type (`tag` or `branch`), and short commit SHA. For registry packages, output is a version and published-timestamp table. Registry packages are identified by three signals checked in order: (1) the `--registry [NAME]` flag forces the registry path; (2) a `source: registry` entry in `apm.lock.yaml` routes the package to the registry that installed it; (3) a configured default registry causes plain shorthands (e.g., `owner/repo`) to route to the registry even without a lockfile entry -- use a full git URL to override this. Private git repositories require `GITHUB_APM_PAT`; private registries use the registry token configured for that registry (see [authentication](../../../consumer/authentication/)).

## Arguments

| Argument  | Required | Description                                                                                       |
| --------- | -------- | ------------------------------------------------------------------------------------------------- |
| `PACKAGE` | yes      | `owner/repo`, short repo name (installed only), or `NAME@MARKETPLACE` for a marketplace plugin    |
| `FIELD`   | no       | Field selector. Only `versions` is supported today                                                |

## Options

| Option              | Description                                  |
| ------------------- | -------------------------------------------- |
| `-g`, `--global`    | Inspect a package installed in user scope (`~/.apm/apm_modules/`) |
| `--registry [NAME]` | List versions from a registry. Omit a value to use the lockfile entry or configured default; provide `NAME` to force a specific named registry. Only valid with the `versions` field. |

## Examples

Show metadata for an installed package:

```bash
apm view microsoft/apm-sample-package
```

Short-name lookup (resolves against `apm_modules/`):

```bash
apm view apm-sample-package
```

List git tags and branches without cloning:

```bash
apm view microsoft/apm-sample-package versions
```

List registry versions for an installed registry package:

```bash
apm view acme/web-skills versions
```

List versions from the configured default registry (for an unlocked shorthand):

```bash
apm view acme/web-skills versions --registry
```

List versions from a specific named registry:

```bash
apm view acme/web-skills versions --registry my-registry
```

Force the git path even when a default registry is configured:

```bash
apm view https://github.com/acme/web-skills versions
```

Inspect a package installed at user scope:

```bash
apm view microsoft/apm-sample-package -g
```

View a marketplace plugin's manifest entry:

```bash
apm view code-review@acme-plugins
```

## Related

- [`apm outdated`](../outdated/) -- compare locked versions against remote tags
- [`apm install`](../install/) -- add a package to `apm.yml` and install it
