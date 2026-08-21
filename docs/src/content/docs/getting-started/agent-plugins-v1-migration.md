---
title: Agent Plugins v1 migration
description: Choose Claude-compatible or explicit Agent Plugins 1.0 output and understand the current deployment boundary.
sidebar:
  order: 5
---

`apm pack` and `apm plugin init` currently default to schema-less,
Claude-compatible output. Pass `--plugin` to produce or scaffold an Agent
Plugins 1.0 artifact.

## Pick the output

| Situation | Command | Result |
|---|---|---|
| Current deployable default | `apm pack` / `apm plugin init` | Schema-less Claude-compatible plugin |
| Explicit Agent Plugins 1.0 artifact | `apm pack --plugin` / `apm plugin init --plugin` | Native whole-plugin artifact |
| Legacy APM bundle pipeline | `apm pack --format apm` | Legacy bundle for `apm unpack` only |

## How `plugin.json` routes

| Package-root state | Behavior |
|---|---|
| Exact `$schema`: `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json` | Validate and admit the whole native plugin |
| Another Agent Plugins schema version | Fail as unsupported |
| Malformed JSON, `$schema`, or Agent Plugin manifest | Fail validation |
| Schema-less `plugin.json` | Use legacy Claude/APM plugin behavior |
| No root `plugin.json` | Continue through normal package-layout detection |

:::caution[Current deployment boundary]
Agent Plugins 1.0 artifacts fail closed at deployment because no selected
target has a machine-verifiable, non-deprecated native Agent Plugin lifecycle.
APM does not fall back to primitive projection.
:::

## Repack, then install

```bash
apm pack
apm install ./build/<name>
```

If you publish archives instead of directories:

```bash
apm pack --archive
apm install ./build/<name>.zip
```

## CI check

```yaml
- run: apm install
- run: apm pack --check-versions --check-clean
- run: apm audit --ci
```

## Compatibility FAQ

### Agent Plugins 1.0

Use `--plugin` to produce or scaffold the native format. APM validates the
artifact as one plugin but cannot deploy it in this release.

### Claude/APM plugin

Use the current default. Its schema-less root `plugin.json` remains on the
legacy deployment path.

### APM

Use `--format apm` only for legacy bundle consumers. `apm install` rejects those bundles; use `apm unpack` only for that path.

## Related

- [Pack a bundle](../../producer/pack-a-bundle/)
- [Deploy a local bundle](../../consumer/deploy-a-bundle/)
- [apm pack](../../reference/cli/pack/)
- [apm plugin init](../../reference/cli/plugin/)
- [apm unpack](../../reference/cli/unpack/)
- [Security model](../../enterprise/security/)
