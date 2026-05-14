---
title: apm view
description: Inspect package metadata or list remote versions
sidebar:
  order: 5
---

Show local metadata for an installed package, or query remote refs without cloning.

## Synopsis

```bash
apm view PACKAGE [FIELD] [OPTIONS]
```

`apm info` is accepted as a hidden alias for backward compatibility.

## Description

`apm view` has two modes, selected by the optional `FIELD` argument:

- **No field** -- read installed package metadata from `apm_modules/` (or `~/.apm/apm_modules/` with `-g`). Local-only; the package must be installed.
- **`versions` field** -- query the remote for available tags and branches. No local install required.

When `PACKAGE` matches the `NAME@MARKETPLACE` pattern, `apm view` resolves the plugin against the marketplace manifest and prints its entry (name, version, description, source, tags) instead of a Git repository view. This applies whether or not `versions` is passed.

See [`apm outdated`](../outdated/) to compare locked versions against remotes, and [`apm install`](../install/) to add a package to the manifest.

## Subcommands

### `apm view <package>`

Reads metadata from the installed copy. Exits non-zero if `apm_modules/` is missing or the package is not installed; on a missing package, prints the list of installed packages to help disambiguate.

Output includes: name, version, description, author, source, install path, lockfile ref and commit (when available), context-file counts (skills, prompts, instructions), workflow count, and hook count.

### `apm view <package> versions`

Lists remote tags and branches for the package. Calls the remote -- requires network access, and for private repositories requires `GITHUB_APM_PAT` (see [authentication](../../../consumer/authentication/)).

Output is a table with name, type (`tag` or `branch`), and short commit SHA.

## Arguments

| Argument  | Required | Description                                                                                       |
| --------- | -------- | ------------------------------------------------------------------------------------------------- |
| `PACKAGE` | yes      | `owner/repo`, short repo name (installed only), or `NAME@MARKETPLACE` for a marketplace plugin    |
| `FIELD`   | no       | Field selector. Only `versions` is supported today                                                |

## Options

| Option           | Description                                  |
| ---------------- | -------------------------------------------- |
| `-g`, `--global` | Inspect a package installed in user scope (`~/.apm/apm_modules/`) |

## Examples

Show metadata for an installed package:

```bash
apm view microsoft/apm-sample-package
```

Short-name lookup (resolves against `apm_modules/`):

```bash
apm view apm-sample-package
```

List remote tags and branches without cloning:

```bash
apm view microsoft/apm-sample-package versions
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
