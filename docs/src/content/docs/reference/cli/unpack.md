---
title: apm unpack
description: Extract an APM bundle into a project directory with verification and security scanning.
sidebar:
  order: 18
---

:::caution[Deprecated]
`apm unpack` is deprecated and will be removed in a future release. For plugin-format bundles, prefer [`apm install <bundle-path>`](../install/) -- it shares the same air-gapped path, integrates with target resolution, and records deployed files in the project lockfile. `apm unpack` remains the deploy path for legacy `--format apm` tarballs without executable content (see [Behavior](#behavior)).
:::

## Synopsis

```bash
apm unpack BUNDLE_PATH [OPTIONS]
```

## Description

`apm unpack` extracts an APM bundle (a `.zip` or legacy `.tar.gz` archive, or an already-unpacked bundle directory) into a target project. It runs the built-in security scan against the bundle contents before writing any files, and -- unless `--skip-verify` is set -- checks that every entry in the bundle's `apm.lock.yaml` `deployed_files` list is actually present in the archive.

Extraction is **additive-only**: only files listed in the bundle's lockfile are written. Existing project files at colliding paths are overwritten by the bundle version. Files outside the bundle's manifest are never touched, and the bundle's `apm.lock.yaml` is treated as metadata -- it is not copied into the output directory.

`BUNDLE_PATH` accepts a `.zip` archive (the default), a legacy `.tar.gz` archive, or the directory form of an unpacked bundle.

:::caution[Executable content is refused]
`apm unpack` copies files verbatim, so it cannot apply the
[executables gate](../approve/) or org policy. It refuses, before writing or
previewing anything, a bundle that carries hooks or hook scripts, MCP or LSP
configuration, `bin/` executables, or canvas extensions (with the `canvas`
flag off, canvas files are dropped instead). Install such bundles with
`apm install <bundle>`; repack a legacy `--format apm` bundle with
`apm pack --archive` first.
:::

## Options

| Flag | Default | Description |
|---|---|---|
| `-o`, `--output PATH` | `.` | Target project directory. Created if it does not exist. |
| `--skip-verify` | off | Skip the bundle completeness check against the bundle's `apm.lock.yaml`. Useful for partial bundles. |
| `--dry-run` | off | List files that would be unpacked without writing anything. |
| `--force` | off | Deploy despite critical hidden-character findings from the security scan. Use only after independent verification. |
| `--verbose`, `-v` | off | Show per-file paths and full diagnostic context. |

## Examples

Unpack a legacy archive into the current directory:

```bash
apm pack --format apm --archive
apm unpack ./build/my-pkg-1.0.0.zip
```

Unpack into a specific project directory:

```bash
apm unpack bundle.zip --output /path/to/project
```

Preview the extraction plan without writing files:

```bash
apm unpack bundle.zip --dry-run
```

Skip verification when working with a partial bundle:

```bash
apm unpack bundle.zip --skip-verify
```

Override a critical hidden-character finding after manual review:

```bash
apm unpack bundle.zip --force
```

## Behavior

- **Bundle formats.** `apm install` deploys only plugin-format bundles. Legacy `--format apm` tarballs (whole-project bundles produced by older `apm pack` invocations) are deployed via `apm unpack`, except executable content (see the caution above), and have no equivalent `install` path. `apm unpack` refuses a plugin-format bundle (root `plugin.json`) and points to `apm install`.
- **Additive writes only.** Files not listed in the bundle's lockfile are left alone; the bundle never deletes project files.
- **Overwrite on collision.** When a bundle file shares a path with a local file, the bundle file wins.
- **Managed destinations only.** Every `deployed_files` entry must land under a known integration directory (`.github/`, `.claude/`, `.agents/`, ...) of the output directory, with no symlinked path component. Any other entry fails the unpack before a file is written. Bundle paths that run through a symlink are skipped.
- **Security scan.** Bundle contents are scanned before deployment. Critical hidden-character findings block extraction unless `--force` is passed (exit code `1`). Non-critical warnings are surfaced with a hint to run [`apm audit`](../audit/).
- **Verification.** By default, every entry in the bundle's `deployed_files` must exist inside the archive. `--skip-verify` disables this check; missing files are then reported as skipped.
- **Target mismatch warning.** If the bundle was packed for a different harness target than the output project's detected target, `apm unpack` warns and -- with `--verbose` -- suggests the `apm pack --target` command the publisher should run.
- **Lockfile is metadata.** The bundle's `apm.lock.yaml` is read for verification and target metadata but is never written to the output directory.
- **Exit codes.** `0` on success (including `--dry-run`); `1` on missing bundle, invalid bundle, executable content (`--force` does not override this), or critical security findings without `--force`.

## Related

- [`apm pack`](../pack/) -- produce the bundles that `apm unpack` extracts.
- [`apm install`](../install/) -- the preferred deploy path for plugin-format bundles.
- [`apm audit`](../audit/) -- inspect hidden-character findings flagged during extraction.
- [Pack a bundle](../../../producer/build-a-plugin/) -- producer guide covering bundle formats and distribution.
