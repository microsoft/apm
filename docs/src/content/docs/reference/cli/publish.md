---
title: apm publish
description: Upload a flat registry archive to a REST-based APM package registry.
sidebar:
  order: 18
---

## Synopsis

```bash
apm publish [OPTIONS]
```

## Description

`apm publish` uploads a package version to a configured registry via `PUT /v1/packages/{owner}/{repo}/versions/{version}`.

By default the command **auto-packs** a flat registry archive in the project root (`{name}-{version}.zip`) containing `apm.yml`, `.apm/`, and standard root-level documentation files (`README.md`, `CHANGELOG.md`, `LICENSE` / `LICENCE`, matched case-insensitively) at the archive root. Symlinks are excluded. This is **not** the plugin bundle layout from [`apm pack`](../pack/) (`{name}-{version}/plugin.json`).

Requires the experimental `registries` feature:

```bash
apm experimental enable registries
```

The project's `apm.yml` must declare a `registries:` block with at least one registry URL. Publish credentials resolve via `APM_REGISTRY_TOKEN_{NAME}` or `apm config set registry.<name>.token`.

## Options

| Flag | Default | Description |
|---|---|---|
| `--registry NAME` | _(required when multiple registries configured)_ | Registry name from the `registries:` block. |
| `--package OWNER/REPO` | _(required)_ | Package identity to publish as (e.g. `acme/my-skill`). Owner and repository are normalized to lowercase. |
| `--zip PATH` | auto-pack | Path to a pre-built `.zip`. Skips auto-pack. (renamed from `--tarball` in v0.20.0) |
| `--dry-run` | off | Print what would be uploaded; do not call the registry. |
| `--verbose`, `-v` | off | Show auto-pack details (archive path). |

## Examples

Auto-pack and publish when only one registry is configured:

```bash
apm publish --package acme/my-skill
```

Choose a registry and preview first:

```bash
apm publish --package acme/my-skill --registry corp-main --dry-run -v
apm publish --package acme/my-skill --registry corp-main
```

Publish a pre-built zip:

```bash
apm publish --package acme/my-skill --zip ./build/my-skill-0.0.1.zip
```

Specify the registry package identity explicitly:

```bash
apm publish --package acme/my-package --registry corp-main
```

## Output

### Successful publish

```
[i] Publishing acme/my-package@1.2.3 to corp-main...
[+] Published acme/my-package@1.2.3
  digest      : sha256:abc123...
  published_at: 2026-05-26T10:15:00Z
  registry    : https://registry.example.com/apm/corp-main
```

With `--verbose`, auto-pack also prints:

```
[i] Packing flat registry archive -> my-package-1.2.3.zip
```

### Dry run

```
[i] Would publish acme/my-package@1.2.3 to corp-main (https://registry.example.com/apm/corp-main)
[i]   archive : /path/to/project/my-package-1.2.3.zip  (12,345 bytes)
[i] (dry-run -- nothing uploaded)
```

### Common errors

| Message | Cause |
|---|---|
| `requires the experimental registries feature` | Run `apm experimental enable registries`. |
| `apm.yml not found` | Run from the package root. |
| `requires a flat APM package (.apm/ directory)` | Add `.apm/` or pass `--zip`. |
| `Multiple registries configured` | Pass `--registry NAME`. |
| `Version '...' already exists ... immutable` | HTTP 409 -- bump `version:` in `apm.yml`. |
| `Registry rejected the package (validation failed)` | HTTP 422 -- archive layout invalid for the server. |
| `Forbidden -- your token does not have publish permission` | HTTP 403 -- check `APM_REGISTRY_TOKEN_{NAME}`. |
| `401` / credentials remediation | HTTP 401 -- token missing or expired. |

Some registries return `201` with an empty body; APM still treats the upload as successful when the HTTP status is success-class.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Published successfully, or `--dry-run` completed without error. |
| `1` | Publish failure: missing `apm.yml` or `.apm/`, invalid manifest, auth error (401/403), version conflict (409), server validation rejection (422), network/registry error, registries feature disabled, or other unhandled error. |
| `2` | Usage error: cannot infer `owner/repo`, multiple registries without `--registry`, unknown `--registry` name, or invalid flag combination. |

## Related

- [Registries (guide)](../../../guides/registries/) -- declare registries, auth, default routing, and policy.
- [`apm pack`](../pack/) -- plugin bundles and marketplace artifacts (different layout from registry publish).
- [`apm install`](../install/) -- consumer side; installs registry packages with `resolved_hash` verification.
- [Registry HTTP API](../../registry-http-api/) -- wire contract for `PUT .../versions/{version}`.
