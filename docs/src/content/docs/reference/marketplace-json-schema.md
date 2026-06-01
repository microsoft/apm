---
title: "Marketplace JSON schema"
description: "The .claude-plugin/marketplace.json format APM emits and consumes."
sidebar:
  order: 9
---

A **marketplace** repo publishes a `marketplace.json` index that lists
plugins (packages) consumers can install. APM writes this file when you run
`apm pack` with a `marketplace:` block in `apm.yml`.

## File locations

APM can emit multiple marketplace artifacts. The schema documented here is
for the **Claude Code / Copilot CLI compatible** output:

- `.claude-plugin/marketplace.json` (default)
- `.agents/plugins/marketplace.json` (Codex output, optional)

The Codex repo format is different (see the Codex output section in
[Publish to a marketplace](../producer/publish-to-a-marketplace/)).

## Top-level shape

```json
{
  "$schema": "https://json.schemastore.org/claude-code-marketplace.json",
  "name": "acme-marketplace",
  "description": "Curated plugins for acme",
  "version": "1.2.0",
  "owner": { "name": "acme-org", "url": "https://github.com/acme-org" },
  "metadata": { "pluginRoot": "./packages" },
  "plugins": [ /* ... */ ]
}
```

Required fields:

- `name` — marketplace name
- `owner.name` — marketplace owner display name
- `plugins` — array of plugin entries

Optional fields:

- `description`, `version`
- `owner.email`, `owner.url`
- `metadata` — optional metadata (notably `pluginRoot`)
- `forceRemoveDeletedPlugins` — if true, removed plugins auto-uninstall
- `allowCrossMarketplaceDependenciesOn` — allowlist of trusted marketplaces

## Plugin entries

Each item in `plugins` is a plugin/package entry. APM emits a subset of the
full schema depending on what it knows (local vs remote packages). Common
fields you will see:

- `name` (required)
- `description`
- `version`
- `author` (`{ name, email?, url? }`)
- `homepage`, `repository`, `license`
- `tags`
- `source` (required)

### `source` forms

`source` can be **a string** (relative path) or an object with a `source`
kind. Common forms:

```json
{ "source": "github", "repo": "owner/repo", "ref": "v1.2.3" }
```

```json
{ "source": "url", "url": "https://github.com/owner/repo", "ref": "main" }
```

```json
{ "source": "git-subdir", "url": "https://github.com/owner/repo", "path": "tools/plugin" }
```

```json
"./packages/my-local-plugin"
```

APM also accepts the **Copilot CLI** legacy format where plugins use
`repository: "owner/repo"` instead of `source`.

### Other optional plugin fields

The schema allows additional plugin metadata used by Claude Code and Copilot
CLI (commands, hooks, agents, MCP servers, user configuration, etc.). APM
does not require these fields, but it will preserve them if present.

For the complete list, see the JSON Schema below.

## Schema

APM vendors the canonical JSON schema here:

- `tests/fixtures/schemas/claude-code-marketplace.schema.json`

That schema is mirrored at:

- https://json.schemastore.org/claude-code-marketplace.json

## Notes for APM authors

- In `apm.yml`, you author `marketplace.packages[]`. At build time, APM
  maps each entry to a `plugins[]` item in `marketplace.json`.
- `metadata.pluginRoot` lets you publish local plugins under a common base
  path. APM will strip that prefix when emitting local `source` paths.
- The manifest input (`apm.yml`) is normative; the `marketplace.json`
  **output shape is governed externally** (Claude/Copilot-compatible schema).
