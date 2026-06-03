---
title: "Marketplace Schema"
description: "The marketplace.json format -- how APM defines plugin marketplaces for Claude Code, Copilot CLI, and APM itself."
sidebar:
  order: 7
---

<dl>
<dt>Version</dt><dd>0.1 (Working Draft)</dd>
<dt>Date</dt><dd>2026-05-03</dd>
<dt>Editors</dt><dd>APM Maintainers</dd>
<dt>Repository</dt><dd>https://github.com/microsoft/apm</dd>
<dt>Format</dt><dd>JSON</dd>
<dt>Canonical Schema</dt><dd><a href="https://github.com/microsoft/apm/blob/main/tests/fixtures/schemas/claude-code-marketplace.schema.json">tests/fixtures/schemas/claude-code-marketplace.schema.json</a></dd>
</dl>

## Status of This Document

This is a **Working Draft**. The `marketplace.json` shape APM emits is byte-for-byte compliant with [Anthropic's plugin-marketplace specification](https://docs.claude.com/en/docs/claude-code/plugin-marketplaces). This document describes the schema as APM consumes and produces it; the canonical machine-readable form is the JSON Schema fixture linked above.

This document may be updated, replaced, or made obsolete at any time. It is inappropriate to cite this document as other than work in progress.

---

## Abstract

A marketplace is a curated index of plugin packages, distributed as a single `marketplace.json` file. APM resolves marketplace entries to dependency closures, applies version locking, and installs plugins exactly as it does for any other APM dependency. This specification defines the shape of `marketplace.json`, the supported source types, key aliases for backward compatibility with Copilot CLI, and the versioning and `pluginRoot` semantics.

---

## 1. Conformance

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119).

A conforming marketplace MUST be a single JSON object that satisfies all MUST-level requirements in this specification. A conforming consumer is a program that loads a `marketplace.json`, resolves each plugin entry per the source-type rules in [section 4.1](#41-source-types), and either installs the plugin or surfaces a structured error.

---

## 2. Document Structure

A conforming `marketplace.json` MUST be a JSON object with the following shape:

```json
{
  "$schema": "...",
  "name": "...",
  "version": "...",
  "description": "...",
  "owner": {
    "name": "..."
  },
  "metadata": {
    "pluginRoot": "./plugins"
  },
  "plugins": []
}
```

The `name`, `owner`, and `plugins` members are REQUIRED. The `plugins` array MAY be empty.

---

## 3. Top-Level Fields

| Field | Type | Required | Description | Constraints | Notes |
|---|---|---|---|---|---|
| `$schema` | `string` | OPTIONAL | JSON Schema reference for editor autocomplete and validation. | None. | Ignored at load time. |
| `name` | `string` | MUST | Marketplace display name. | Minimum length 1. | Used as the default marketplace alias when registering without `--name`. |
| `version` | `string` | OPTIONAL | Marketplace manifest version. | None. | Informational; it does not control plugin checkout. |
| `description` | `string` | OPTIONAL | Human-readable description of the marketplace. | None. | Display metadata only. |
| `owner` | `object` | MUST | Marketplace maintainer or curator information. | MUST contain `name`. | See [section 3.1](#31-owner-object). |
| `plugins` | `array` | MUST | Collection of available plugins in this marketplace. | Each item MUST satisfy [section 4](#4-plugin-entries). | MAY be empty. |
| `forceRemoveDeletedPlugins` | `boolean` | OPTIONAL | Indicates that plugins removed from the marketplace are automatically uninstalled and flagged for users. | None. | Consumers that do not implement forced removal MAY ignore it. |
| `metadata` | `object` | OPTIONAL | Optional marketplace metadata. | Known schema keys are `pluginRoot`, `version`, and `description`; additional metadata MAY be present. | See [section 5](#5-plugin-root-directory). |
| `allowCrossMarketplaceDependenciesOn` | `array<string>` | OPTIONAL | Marketplace names whose plugins may be auto-installed as dependencies. | Each item is a string. | Only the root marketplace's allowlist applies; trust is not transitive. |

### 3.1. Owner Object

| Field | Type | Required | Description | Constraints | Notes |
|---|---|---|---|---|---|
| `name` | `string` | MUST | Display name of the marketplace maintainer, curator, author, or organization. | Minimum length 1. | Required whenever `owner` is present; top-level `owner` itself is REQUIRED. |
| `email` | `string` | OPTIONAL | Contact email for support or feedback. | None. | The schema does not enforce email syntax. |
| `url` | `string` | OPTIONAL | Website, GitHub profile, or organization URL. | None. | The schema does not enforce URI syntax for this field. |

---

## 4. Plugin Entries

Each member of `plugins` MUST be an object. Each plugin entry MUST contain `name` and `source`.

| Field | Type | Required | Description | Constraints | Notes |
|---|---|---|---|---|---|
| `$schema` | `string` | OPTIONAL | JSON Schema reference for editor autocomplete and validation. | None. | Ignored at load time. |
| `name` | `string` | MUST | Unique identifier matching the plugin name. | Minimum length 1. | Consumers SHOULD treat names as unique within one marketplace. |
| `source` | `string \| object` | MUST | Where to fetch the plugin from. | MUST match one of [section 4.1](#41-source-types). | Resolver input. |
| `version` | `string` | OPTIONAL | Semantic version string, for example `1.2.3`. | None. | Informational in `marketplace.json`; `source.ref` controls checkout. |
| `description` | `string` | OPTIONAL | Brief, user-facing explanation of what the plugin provides. | None. | Display metadata. |
| `author` | `object` | OPTIONAL | Plugin creator or maintainer information. | If present, MUST contain `name`. | Same shape as [section 3.1](#31-owner-object). |
| `homepage` | `string` | OPTIONAL | Plugin homepage or documentation URL. | MUST be a URI. | Display metadata. |
| `repository` | `string` | OPTIONAL | Source code repository URL. | None. | Display metadata; distinct from source resolution. |
| `license` | `string` | OPTIONAL | SPDX license identifier, for example `MIT` or `Apache-2.0`. | None. | Display metadata. |
| `keywords` | `array<string>` | OPTIONAL | Tags for plugin discovery and categorization. | Each item is a string. | Preserved for Claude Code compatibility. |
| `tags` | `array<string>` | OPTIONAL | Tags for searchability and discovery. | Each item is a string. | APM authoring also accepts `keywords` as an alias when building from `apm.yml`. |
| `category` | `string` | OPTIONAL | Category for organizing plugins, for example `productivity` or `development`. | None. | Display metadata. |
| `dependencies` | `array` | OPTIONAL | Plugins that must be enabled for this plugin to function. | Each item MUST be a string dependency selector or an object with `name` and optional `marketplace`. | Bare names are resolved against the declaring plugin's own marketplace. |
| `hooks` | `string \| object \| array` | OPTIONAL | Additional hook declarations, or a path to hook declarations. | Path forms MUST begin with `./`; JSON file paths MUST end in `.json`. | When omitted, hosts MAY load hooks from the plugin root's conventional hooks directory. |
| `commands` | `string \| object \| array` | OPTIONAL | Additional slash command declarations, inline command metadata, or paths to command files. | Path forms MUST begin with `./`; Markdown file paths MUST end in `.md`. | Object keys become command names. |
| `agents` | `string \| array<string>` | OPTIONAL | Additional agent files. | Path forms MUST begin with `./` and end in `.md`. | Adds to agents discovered in the plugin root. |
| `skills` | `string \| array<string>` | OPTIONAL | Additional skill directories. | Path forms MUST begin with `./`. | Adds to skills discovered in the plugin root. |
| `outputStyles` | `string \| array<string>` | OPTIONAL | Additional output style directories or files. | Path forms MUST begin with `./`. | Adds to output styles discovered in the plugin root. |
| `themes` | `string \| array<string>` | OPTIONAL | Additional theme directories or files. | Path forms MUST begin with `./`. | Adds to themes discovered in the plugin root. |
| `channels` | `array<object>` | OPTIONAL | MCP-backed message channels exposed by the plugin. | Each item MUST contain `server`. | Channel user configuration is prompted at enable time when declared. |
| `mcpServers` | `object \| array` | OPTIONAL | MCP server declarations or paths to MCP server declarations. | Supported server shapes include command, SSE, HTTP, and SDK transports. | Server paths MUST begin with `./` when path form is used. |
| `lspServers` | `object \| array` | OPTIONAL | Language server declarations or paths to declarations. | Declarations include command and extension-to-language mappings. | Used by hosts that support plugin-provided LSP servers. |
| `monitors` | `string \| array \| object` | OPTIONAL | Background watch scripts the host arms as persistent monitor tasks. | Path forms MUST begin with `./`. | Monitors are unsandboxed and have the same trust tier as hooks. |
| `settings` | `object` | OPTIONAL | Settings to merge into user settings while the plugin is enabled. | Only documented allowlisted keys are applied. | Additional object content is preserved by the schema. |
| `userConfig` | `object` | OPTIONAL | User-configurable values prompted at enable time. | Config entries declare type, title, description, and related validation metadata. | Sensitive values are stored in secure storage; non-sensitive values are saved to settings. |
| `strict` | `boolean` | OPTIONAL | Requires the plugin manifest to be present in the plugin folder. | Defaults to `true`. | If `false`, the marketplace entry provides the manifest. |

### 4.1. Source Types

APM supports the following marketplace source forms:

| Type | JSON form | Required fields | Description | Example |
|---|---|---|---|---|
| String `source` | `"./tools/local-plugin"` | None beyond the string itself. | Path to the plugin root, relative to the marketplace root. The canonical path form begins with `./`; APM also resolves bare names through `metadata.pluginRoot` as described in [section 5](#5-plugin-root-directory). | `"./tools/local-plugin"` |
| `github` | `{ "source": "github", "repo": "owner/repo" }` | `source`, `repo` | GitHub repository in `owner/repo` format. | `{ "source": "github", "repo": "acme/code-review-plugin" }` |
| `url` | `{ "source": "url", "url": "https://..." }` | `source`, `url` | Full Git repository URL, such as HTTPS or `git@` SSH syntax. | `{ "source": "url", "url": "https://github.com/acme/style-guide.git" }` |
| `git-subdir` | `{ "source": "git-subdir", "url": "owner/repo", "path": "plugins/tool" }` | `source`, `url`, `path` | Plugin located in a subdirectory of a larger repository. The repository is cloned sparsely so only the selected subdirectory is materialized. | `{ "source": "git-subdir", "url": "acme/monorepo", "path": "plugins/eslint-rules" }` |

The canonical JSON Schema also contains an `npm` source object for Claude Code schema compatibility. APM marketplace resolution does not support npm marketplace sources; marketplace authors targeting APM SHOULD use one of the supported source forms above.

Source objects MAY also include the following optional fields:

| Field | Applies to | Type | Description | Constraints |
|---|---|---|---|---|
| `ref` | `github`, `url`, `git-subdir` | `string` | Git branch or tag to use. | Defaults to the repository default branch when omitted. |
| `sha` | `github`, `url`, `git-subdir` | `string` | Specific commit SHA to use. | Exactly 40 lowercase hexadecimal characters. |

### 4.1.1. Source Key Aliases

APM accepts both legacy Copilot CLI key names and current Claude Code key names in `marketplace.json` source objects:

| Current key | Legacy alias | Notes |
|---|---|---|
| `source` | `type` | Discriminator values are `github`, `git-subdir`, and `url`. |
| `repo` | `repository` | For `github`, the value MUST be `owner/repo`. |
| `sha` | `commit` | Resolved commit SHA. |
| `url` | `repo` | For `git-subdir`, current Claude Code schema names the repository field `url`; APM also accepts `repo` for compatibility with existing marketplace prose and generated manifests. |
| `path` | `subdir` | Subdirectory containing the plugin for `git-subdir` sources. |

Marketplace authors SHOULD use the current keys emitted by `apm pack`. Legacy aliases are accepted for backward compatibility.

### 4.2. Versioned Plugins

The plugin-level `version` field is informational. APM displays it in commands such as `apm view` and uses it for warnings such as version immutability checks, but it does not determine which bytes are installed. The resolver input is the Git selector in `source.ref` or the immutable commit in `source.sha`.

```json
{
  "name": "Acme Plugins",
  "owner": { "name": "Acme Corp" },
  "plugins": [
    {
      "name": "code-review",
      "description": "Automated code review agent",
      "version": "2.1.0",
      "source": {
        "source": "github",
        "repo": "acme/code-review-plugin",
        "ref": "v2.1.0"
      }
    }
  ]
}
```

In this example, `version` is display metadata and `source.ref` is the checkout instruction.

---

## 5. Plugin Root Directory

`metadata.pluginRoot` specifies the base directory for bare-name relative sources in the marketplace repository.

```json
{
  "name": "Acme Plugins",
  "owner": { "name": "Acme Corp" },
  "metadata": { "pluginRoot": "./plugins" },
  "plugins": [
    { "name": "my-tool", "source": "my-tool" }
  ]
}
```

With `pluginRoot` set to `./plugins`, the source `"my-tool"` resolves to the `plugins/my-tool` directory in the marketplace repository. Sources that already contain a path separator, such as `./custom/path`, are not affected by `pluginRoot`.

The `metadata` object MAY also contain `version` and `description` strings for marketplace metadata. Consumers MUST NOT treat `metadata.version` as a plugin version or resolver input.

---

## 6. Examples

### 6.1. Minimal

```json
{
  "name": "Acme Plugins",
  "owner": { "name": "Acme Corp" },
  "plugins": [{ "name": "local-tools", "source": "./tools/local-plugin" }]
}
```

### 6.2. Full-Featured

```json
{
  "$schema": "https://json.schemastore.org/claude-code-marketplace.json",
  "name": "Acme Plugins",
  "version": "1.0.0",
  "description": "Curated plugins for the acme-org engineering team",
  "owner": {
    "name": "acme-org",
    "url": "https://github.com/acme-org",
    "email": "maintainers@acme-org.example"
  },
  "metadata": {
    "homepage": "https://example.com/plugins",
    "pluginRoot": "./plugins"
  },
  "allowCrossMarketplaceDependenciesOn": ["shared-tools"],
  "plugins": [
    {
      "name": "code-review",
      "description": "Automated code review agent",
      "version": "2.1.0",
      "source": {
        "source": "github",
        "repo": "acme/code-review-plugin",
        "ref": "v2.1.0"
      },
      "homepage": "https://github.com/acme/code-review-plugin",
      "tags": ["review", "quality"],
      "license": "MIT"
    },
    {
      "name": "style-guide",
      "description": "Shared engineering style guide",
      "source": {
        "source": "url",
        "url": "https://github.com/acme/style-guide.git",
        "sha": "0123456789abcdef0123456789abcdef01234567"
      }
    },
    {
      "name": "eslint-rules",
      "description": "ESLint rules from the Acme monorepo",
      "source": {
        "source": "git-subdir",
        "url": "acme/monorepo",
        "path": "plugins/eslint-rules",
        "ref": "main"
      }
    },
    {
      "name": "local-tools",
      "description": "Plugin shipped alongside this marketplace",
      "source": "./tools/local-plugin"
    }
  ]
}
```

### 6.3. Copilot CLI Compatibility

```json
{
  "name": "Acme Plugins",
  "owner": { "name": "Acme Corp" },
  "plugins": [
    {
      "name": "legacy-review",
      "description": "Legacy Copilot CLI marketplace entry",
      "source": {
        "type": "github",
        "repository": "acme/code-review-plugin",
        "ref": "v2.1.0",
        "commit": "0123456789abcdef0123456789abcdef01234567"
      }
    }
  ]
}
```

APM normalizes this legacy source object to the current key names before dependency resolution.

---

## 7. Validation

The canonical machine-readable schema is [`tests/fixtures/schemas/claude-code-marketplace.schema.json`](https://github.com/microsoft/apm/blob/main/tests/fixtures/schemas/claude-code-marketplace.schema.json). Validate a marketplace with any JSON Schema draft-07 validator. For example:

```bash
python -m jsonschema -i marketplace.json tests/fixtures/schemas/claude-code-marketplace.schema.json
```

Validation catches missing required fields, invalid URI-formatted fields such as `homepage`, invalid fixed-length commit SHAs in `source.sha`, and source objects that do not match any supported schema branch.

---

## 8. Versioning and Backward Compatibility

The top-level `version` field is informational about the marketplace manifest. It is not the version of this specification and it is not used as a plugin resolver input.

APM accepts both Copilot CLI legacy keys and Claude Code current keys for marketplace source objects. APM emits the current Claude Code form when building `marketplace.json` with `apm pack`.

Future breaking schema changes SHOULD be gated behind a `$schema` URL bump. Consumers SHOULD ignore unknown metadata fields that they do not understand, but MUST reject documents that fail required fields or source-shape validation.

---

## 9. References

- [Anthropic plugin-marketplace specification](https://docs.claude.com/en/docs/claude-code/plugin-marketplaces)
- [GitHub Copilot CLI marketplace.json reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference#marketplacejson)
- [Canonical JSON Schema](https://github.com/microsoft/apm/blob/main/tests/fixtures/schemas/claude-code-marketplace.schema.json)
- [Marketplaces guide](../../guides/marketplaces/)
- [Marketplace authoring guide](../../guides/marketplace-authoring/)
