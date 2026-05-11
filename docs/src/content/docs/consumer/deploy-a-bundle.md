---
title: Deploy a local bundle
description: Install a plugin-format bundle from a directory or tarball without touching apm.yml.
---

You have a bundle on disk -- a directory or `.tar.gz` someone handed you, or
the output of `apm pack --format plugin`. Drop it into a project with one
command:

```bash
apm install ./path/to/bundle
apm install ./dist/my-pkg-1.0.0.tar.gz
```

This is a sibling flow to [Install packages](../install-packages/). Instead
of declaring a dependency in `apm.yml` and resolving it from a registry, you
deploy a self-contained bundle directly. `apm.yml` is **not modified** --
the install is imperative, like `dpkg -i` next to `apt install`.

## What counts as a bundle

A plugin-format bundle is a directory (or gzipped tarball of one) with a
`plugin.json` at the root and primitive folders alongside it:

```
my-bundle/
+-- plugin.json
+-- agents/
+-- skills/
+-- commands/
+-- hooks/
+-- apm.lock.yaml        # optional: bundle integrity manifest
```

`plugin.json` requires only a `name`. APM also recognises `plugin.json`
under `.github/plugin/`, `.claude-plugin/`, or `.cursor-plugin/`. For the
full schema see [Package anatomy](../../concepts/package-anatomy/).

The optional `apm.lock.yaml` carries `pack.bundle_files` -- a SHA-256
manifest written by `apm pack --format plugin`. When present, APM verifies
every file against it before deploying. When absent (older bundles), APM
warns and proceeds.

## How the install works

```
$ apm install ./dist/my-pkg-1.0.0.tar.gz --target copilot
[>] Installing local bundle from ./dist/my-pkg-1.0.0.tar.gz
[+] Bundle integrity verified
[+] Deployed 7 files to .github/
```

Steps APM runs:

1. **Detect.** Path exists and contains `plugin.json` at the bundle root
   (tarballs are extracted to a temp directory first).
2. **Verify integrity.** Hash every file listed in `pack.bundle_files`;
   reject any symlink, hash mismatch, or unlisted file.
3. **Deploy.** Map `agents/`, `skills/`, `commands/`, `hooks/` into the
   harness layout for each `--target` you passed.
4. **Record.** Write a lockfile entry under the project's `apm.lock.yaml`
   so [drift detection](../drift-and-secure-by-default/) can audit the
   deployed files later.

`apm.yml` is never touched. Re-running the same command re-deploys (use
`--force` to overwrite locally-edited files).

## Flags that apply

Most `apm install` flags target the registry/resolver pipeline and are
**rejected** with a single error when used with a local bundle. The flags
that *do* work:

| Flag | Use |
|---|---|
| `--target`, `-t`     | Pick which harness layouts receive files. |
| `--global`, `-g`     | Deploy to `~/.apm/` instead of the current project. |
| `--force`            | Overwrite locally-edited files on collision. |
| `--dry-run`          | Show what would be deployed; write nothing. |
| `--verbose`, `-v`    | Print per-file deploy details. |
| `--as ALIAS`         | Override the display label in logs (local-bundle only). |

Flags like `--update`, `--only`, `--dev`, `--mcp`, `--registry`, and
`--allow-insecure` are not meaningful here -- the imperative deploy path
does not run the resolver, MCP registry lookup, or policy chain that those
flags configure. APM rejects them up front with one consolidated error.

For the full flag list see the [`apm install` reference](../../reference/cli/install/).

## Targets

Pass `--target` to scope the deploy. Without it APM auto-detects from the
current project. If the bundle was packed for targets you are not
installing into, APM prints a warning naming the missing targets and
proceeds with what it can deploy.

A bundle packed with `pack.target: all` is target-agnostic and installs
cleanly into any harness layout.

## Legacy `--format apm` bundles are rejected

Bundles produced by the older `apm pack --format apm` carry an
`apm.lock.yaml` at their root but **no `plugin.json`**. `apm install`
rejects these with a targeted error:

```
'./dist/my-pkg-0.1.0.tar.gz' was packed with '--format apm' (legacy
format). 'apm install <bundle>' requires the plugin format. Repack with
'apm pack --format plugin --archive', or use 'apm unpack' to deploy the
legacy bundle.
```

Two ways forward:

- **Repack.** If you own the bundle, run
  `apm pack --format plugin --archive` and install the new artifact.
- **Unpack.** If you only have the legacy artifact, use `apm unpack
  <bundle>` to extract it. `apm unpack` is deprecated and will be removed
  in v0.14; prefer repacking when you can.

A plain directory without `plugin.json` is not treated as a bundle at all.
APM falls through to the dependency resolver and treats the path as a
local-path dependency -- a different flow covered in [Install packages](../install-packages/).

## What to read next

- [Install packages](../install-packages/) -- the manifest-driven flow.
- [Drift and secure by default](../drift-and-secure-by-default/) -- audit
  what a bundle deployed.
- [Package anatomy](../../concepts/package-anatomy/) -- the file layout
  reference.
