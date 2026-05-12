---
title: Pack a bundle
description: Build a plugin-format bundle from your .apm/ source so others can deploy it with a single apm install command.
---

A bundle is the artifact you hand to a consumer when you do not want to publish
to a registry. It is a directory (or `.tar.gz` of one) containing a
`plugin.json`, your primitive folders, and an embedded `apm.lock.yaml` that
pins every file by SHA-256. Build it with one command from a project that has
`.apm/` and `apm.yml`:

```bash
apm pack
```

This is the producer side of [Deploy a local bundle](../../consumer/deploy-a-bundle/).
Consumers who receive the artifact run `apm install ./your-bundle` and skip
the registry resolver entirely.

## What `apm pack` produces

By default `apm pack` writes a plugin-format directory under `./build/`:

```
build/<your-package>/
+-- plugin.json
+-- agents/
+-- skills/
+-- commands/
+-- hooks/
+-- apm.lock.yaml      # embedded: pins every file by SHA-256
```

The success line tells you exactly what to share:

```
$ apm pack
[+] Packed 7 file(s) -> build/my-pkg
[>] Plugin bundle ready -- contains plugin.json plus plugin-native
    directories (agents/, skills/, commands/, ...) and an embedded
    apm.lock.yaml for install-time integrity verification.
[i] Share with: apm install build/my-pkg
```

Add `--archive` to get a single `.tar.gz` instead of a directory; use `-o` to
change the output location (default `./build`).

```bash
apm pack --archive -o ./dist
# -> ./dist/my-pkg-<version>.tar.gz
```

## The plugin.json contract

`plugin.json` is the bundle's identity card. Only `name` is required. APM
synthesises one from `apm.yml` if you do not author it yourself, mapping these
fields:

| `apm.yml` field | `plugin.json` field |
|---|---|
| `name`         | `name` (required) |
| `version`      | `version` |
| `description`  | `description` |
| `author`       | `author: {name: ...}` |
| `license`      | `license` |

Author your own `plugin.json` at the project root (or under `.github/plugin/`,
`.claude-plugin/`, or `.cursor-plugin/`) when you need fields APM does not
synthesise -- otherwise leave it to `apm pack` and keep `apm.yml` as the
source of truth. See [Package anatomy](../../concepts/package-anatomy/) for
the full schema.

## Integrity: how install verifies the bundle

`apm pack` writes `pack.bundle_files` into the embedded `apm.lock.yaml` -- a
mapping of every file's relative path to its SHA-256 digest. On the consumer
side, `apm install <bundle>` rehashes every file and rejects the bundle if:

- any hash does not match
- any file listed in `pack.bundle_files` is missing
- any file is present in the bundle but not listed in the manifest
- any path is a symlink

The manifest is the source of truth. Tampering after pack time is detected
before any file lands in the project. You do not configure this -- it runs on
every `apm install <bundle>`.

## Distribution paths

Three common ways to hand off a bundle:

- **Directory + git.** Commit `build/<pkg>/` to a release branch or a separate
  artifacts repo. Consumers `git clone` and run `apm install ./build/<pkg>`.
- **Archive + GitHub release.** `apm pack --archive` then upload the
  `.tar.gz` as a release asset. Consumers download and run
  `apm install ./<pkg>-<version>.tar.gz`.
- **Marketplace entry.** If your project also has a `marketplace:` block in
  `apm.yml`, `apm pack` builds `marketplace.json` alongside the bundle. See
  [Publish to a marketplace](../publish-to-a-marketplace/).

For the consumer flags that apply (`--target`, `--global`, `--force`,
`--dry-run`), see [Deploy a local bundle](../../consumer/deploy-a-bundle/).

## Pitfalls

**Do not use `--format apm` for bundles you expect consumers to install.**
The legacy APM bundle layout has no `plugin.json` and `apm install` rejects
it with a targeted error. The flag exists for tooling that still consumes
the older layout; new bundles should use the default `--format plugin`. If
you only have a legacy artifact, repack it:

```bash
apm pack --format plugin --archive
```

**Do not set `--target`.** The flag is deprecated. Bundles are
target-agnostic: the consumer's project decides which harness layouts
receive files at install time. APM records the value in `pack.target` as
informational metadata only and prints a deprecation warning.

**Empty bundle warning.** If `apm pack` reports "No deployed files found",
your `apm.lock.yaml` has no `deployed_files` entries. Run `apm install` first
to populate it -- `apm pack` packs the files your last install actually
deployed, not the raw `.apm/` source tree.

**Dry-run before sharing.** Use `apm pack --dry-run --verbose` to see the
full file list (and any path remappings) without writing anything.

## What to read next

- [Deploy a local bundle](../../consumer/deploy-a-bundle/) -- the consumer
  side of this hand-off.
- [Publish to a marketplace](../publish-to-a-marketplace/) -- when a registry
  entry is a better fit than a bundle.
- [Package anatomy](../../concepts/package-anatomy/) -- the file layout and
  schema reference.
