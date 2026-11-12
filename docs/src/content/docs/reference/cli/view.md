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
apm view --file PATH [-g]
```

`apm info` is accepted as a hidden alias for backward compatibility.

## Description

`apm view` has two modes, selected by the optional `FIELD` argument:

- **No field** -- read installed package metadata from `apm_modules/` (or `~/.apm/apm_modules/` with `-g`). Local-only; the package must be installed.
- **`versions` field** -- query available versions. Git packages list remote tags and branches without requiring a local install. Registry packages list version and published-timestamp columns. See the `apm view <package> versions` subcommand below for the full registry-routing precedence and escape-hatch details.

With `--file PATH`, `apm view` works the other way round: it starts from one deployed file and reports which package recorded it. See [`apm view --file PATH`](#apm-view---file-path).

When `PACKAGE` matches the `NAME@MARKETPLACE` pattern, `apm view` resolves the plugin against the marketplace manifest and prints its entry (name, version, description, source, tags) instead of a Git repository view. This applies whether or not `versions` is passed.

See [`apm outdated`](../outdated/) to compare locked versions against remotes, and [`apm install`](../install/) to add a package to the manifest.

## Subcommands

### `apm view <package>`

Reads metadata from the installed copy. Exits non-zero if `apm_modules/` is missing or the package is not installed; on a missing package, prints the list of installed packages to help disambiguate.

Output includes: name, version, description, author, source, install path, lockfile ref and commit (when available), context-file counts (skills, prompts, instructions), workflow count, and hook count.

### `apm view <package> versions`

Lists available versions for the package. Calls the remote -- requires network access.

For git packages, output is a table with name, type (`tag` or `branch`), and short commit SHA. For registry packages, output is a version and published-timestamp table. Registry packages are identified by three signals checked in order: (1) the `--registry [NAME]` flag forces the registry path; (2) a `source: registry` entry in `apm.lock.yaml` routes the package to the registry that installed it; (3) a configured default registry causes plain shorthands (e.g., `owner/repo`) to route to the registry even without a lockfile entry -- use a full git URL to override this. Private git repositories require `GITHUB_APM_PAT`; private registries use the registry token configured for that registry (see [authentication](../../../consumer/authentication/)).

### `apm view --file PATH`

Read-only reverse lookup for one deployed file, such as an installed `SKILL.md` or a file bundled next to it. It reads the deployment records in `apm.lock.yaml` (`~/.apm/apm.lock.yaml` with `-g`) and matches `PATH` exactly. It never guesses from skill names, directory prefixes or substrings. Unlike [`apm find`](../find/), which lists package names from `deployed_files`, it reports the active writer, the source and the current file status, with exit codes for scripting.

For each matching record it prints:

- **Owner** -- every recorded owner. On a shared path, the last writer is marked `active writer` and the others `surviving owner`.
- **Kind** -- this project (project-self), a local bundle, a direct dependency, or a transitive dependency and its parent.
- **Source, Ref, Commit** -- for dependencies, as recorded in the lockfile. A replaced dependency shows the replacement source it installs from. Credentials and control characters are removed.
- **Current file** -- kept separate from provenance: `matches the recorded deployed hash`, `modified since deployment`, `missing`, or `unverifiable` (for example no recorded hash, not a regular file, or a path through a symlink, including a symlinked home or project root, which is not followed).

A modified or missing file still shows its recorded owner. That is recorded provenance, not proof of what upstream ships today.

Run from the project root. `PATH` may be relative to it or absolute, and must sit under it (under your home directory with `-g`). Only records for the selected scope are matched. The command makes no network calls and writes nothing: no lockfile migration, repair or ownership adoption.

Exit codes:

| Code | Meaning |
| ---- | ------- |
| `0`  | Provenance recorded for this exact path |
| `1`  | No recorded provenance, or a recorded owner (active writer or surviving owner) has no dependency record. The file's origin is unknown or incomplete; do not treat it as project-owned or safe to edit |
| `2`  | Usage error, path outside the scope root, or missing or unreadable lockfile |

Before editing an installed skill, run this command. If the owner is a dependency, edit that package's source; `apm install` can overwrite edits made to the deployed copy. This is advice only: APM does not block writes.

## Arguments

| Argument  | Required | Description                                                                                       |
| --------- | -------- | ------------------------------------------------------------------------------------------------- |
| `PACKAGE` | yes, unless `--file` is used | `owner/repo`, short repo name (installed only), or `NAME@MARKETPLACE` for a marketplace plugin    |
| `FIELD`   | no       | Field selector. Only `versions` is supported today                                                |

## Options

| Option              | Description                                  |
| ------------------- | -------------------------------------------- |
| `-g`, `--global`    | Inspect a package installed in user scope (`~/.apm/apm_modules/`). With `--file`, read the user-scope lockfile |
| `--file PATH`       | Show the recorded owner(s) of one deployed file (read-only, exact path). Cannot be combined with `PACKAGE`, `FIELD` or `--registry` |
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

Find which package deployed an installed skill:

```bash
apm view --file .agents/skills/code-review/SKILL.md
```

Example output:

```text
[i] .agents/skills/code-review/SKILL.md (project scope, from apm.lock.yaml)
  Target: copilot
    Owner: acme/review-skills [active writer]
      Kind: direct dependency
      Package: review-skills 1.2.0
      Source: github.com/acme/review-skills
      Ref: v1.2.0
      Commit: 0123456789abcdef0123456789abcdef01234567
  Current file: matches the recorded deployed hash
[i] Deployed copy from a dependency. Edit the dependency's source instead; 'apm install' can overwrite edits made here.
```

Inspect a user-scope skill file:

```bash
apm view --file ~/.copilot/skills/code-review/SKILL.md -g
```

View a marketplace plugin's manifest entry:

```bash
apm view code-review@acme-plugins
```

## Related

- [`apm outdated`](../outdated/) -- compare locked versions against remote tags
- [`apm install`](../install/) -- add a package to `apm.yml` and install it
- [`apm find`](../find/) -- list the packages whose `deployed_files` include a path
- [Skills](../../../producer/author-primitives/skills/#skill-names-and-provenance) -- skill names and provenance
