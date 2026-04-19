---
title: "Marketplaces"
sidebar:
  order: 5
---

Marketplaces are curated indexes of plugins hosted as GitHub repositories. Each marketplace contains a `marketplace.json` file that maps plugin names to source locations. APM resolves these entries to Git URLs, so plugins installed from marketplaces get the same version locking, security scanning, and governance as any other APM dependency.

## How marketplaces work

A marketplace is a GitHub repository with a `marketplace.json` at its root. The file lists plugins with their source type and location:

```json
{
  "name": "Acme Plugins",
  "plugins": [
    {
      "name": "code-review",
      "description": "Automated code review agent",
      "source": { "type": "github", "repo": "acme/code-review-plugin" }
    },
    {
      "name": "style-guide",
      "source": { "type": "url", "url": "https://github.com/acme/style-guide.git" }
    },
    {
      "name": "eslint-rules",
      "source": { "type": "git-subdir", "repo": "acme/monorepo", "subdir": "plugins/eslint-rules" }
    },
    {
      "name": "local-tools",
      "source": "./tools/local-plugin"
    }
  ]
}
```

Both Copilot CLI and Claude Code `marketplace.json` formats are supported. Copilot CLI uses `"repository"` and `"ref"` fields; Claude Code uses `"source"` (string or object). APM normalizes entries from either format into its canonical dependency representation.

### Supported source types

| Type | Description | Example |
|------|-------------|---------|
| `github` | GitHub `owner/repo` shorthand | `acme/code-review-plugin` |
| `url` | Full HTTPS or SSH Git URL | `https://github.com/acme/style-guide.git` |
| `git-subdir` | Subdirectory within a Git repository (`repo` + `subdir`) | `acme/monorepo` + `plugins/eslint-rules` |
| String `source` | Subdirectory within the marketplace repository itself | `./tools/local-plugin` |

npm sources are not supported. Copilot CLI format uses `"repository"` and optional `"ref"` fields instead of `"source"`.

### Plugin root directory

Marketplaces can declare a `metadata.pluginRoot` field to specify the base directory for bare-name sources:

```json
{
  "metadata": { "pluginRoot": "./plugins" },
  "plugins": [
    { "name": "my-tool", "source": "my-tool" }
  ]
}
```

With `pluginRoot` set to `./plugins`, the source `"my-tool"` resolves to `owner/repo/plugins/my-tool`. Sources that already contain a path separator (e.g. `./custom/path`) are not affected by `pluginRoot`.

## Register a marketplace

### From a GitHub repository

```bash
apm marketplace add acme/plugin-marketplace
```

This registers the marketplace and fetches its `marketplace.json`. By default APM tracks the `main` branch.

**Options:**
- `--name/-n` -- Custom display name for the marketplace
- `--branch/-b` -- Branch to track (default: `main`)
- `--host` -- Git host FQDN for non-github.com hosts (default: `github.com` or `GITHUB_HOST` env var)

```bash
# Register with a custom name on a specific branch
apm marketplace add acme/plugin-marketplace --name acme-plugins --branch release

# Register from a GitHub Enterprise host (two equivalent forms)
apm marketplace add acme/plugin-marketplace --host ghes.corp.example.com
apm marketplace add ghes.corp.example.com/acme/plugin-marketplace
```

### From a URL

```bash
apm marketplace add https://plugins.example.com
```

APM automatically appends `/.well-known/agent-skills/index.json` to bare origins, following the Agent Skills Discovery RFC v0.2.0. You can also pass the full index URL:

```bash
apm marketplace add https://plugins.example.com/.well-known/agent-skills/index.json
```

The index must conform to the Agent Skills RFC schema (`$schema: "https://aka.ms/agent-skills-discovery/v0.2.0/schema"`). Plugins may use `type: "skill-md"` (direct Markdown content) or `type: "archive"` (downloadable `.tar.gz` archive). APM enforces HTTPS-only, validates SHA-256 digests when present, and supports conditional refresh via `ETag`/`Last-Modified` headers.

**Options:**
- `--name/-n` -- Custom display name for the marketplace

```bash
# Register with a custom name
apm marketplace add https://plugins.example.com --name company-skills
```

## List registered marketplaces

```bash
apm marketplace list
```

Shows all registered marketplaces with their source repository and branch.

## Browse plugins

View all plugins available in a specific marketplace:

```bash
apm marketplace browse acme-plugins
```

## Search a marketplace

Search plugins by name or description in a specific marketplace using `QUERY@MARKETPLACE`:

```bash
apm search "code review@skills"
```

**Options:**
- `--limit` -- Maximum results to return (default: 20)

```bash
apm search "linting@awesome-copilot" --limit 5
```

The `@MARKETPLACE` scope is required -- this avoids name collisions when different
marketplaces contain plugins with the same name. To see everything in a marketplace,
use `apm marketplace browse <name>` instead.

## Install from a marketplace

Use the `NAME@MARKETPLACE` syntax to install a plugin from a specific marketplace:

```bash
apm install code-review@acme-plugins
```

APM resolves the plugin name against the marketplace index, fetches the underlying Git repository, and installs it as a standard APM dependency. The resolved source appears in `apm.yml` and `apm.lock.yaml` just like any direct dependency.

For full `apm install` options, see [CLI Commands](../../reference/cli-commands/).

## Provenance tracking

Marketplace-resolved plugins are tracked in `apm.lock.yaml` with full provenance:

```yaml
apm_modules:
  acme/code-review-plugin:
    resolved: https://github.com/acme/code-review-plugin#main
    commit: abc123def456789
    discovered_via: acme-plugins
    marketplace_plugin_name: code-review
```

The `discovered_via` field records which marketplace was used for discovery. `marketplace_plugin_name` stores the original plugin name from the index. The `resolved` URL and `commit` pin the exact version, so builds remain reproducible regardless of marketplace availability.

## Cache behavior

APM caches marketplace indexes locally with a 1-hour TTL. Within that window, commands like `search` and `browse` use the cached index. After expiry, APM fetches a fresh copy from the network. If the network request fails, APM falls back to the expired cache (stale-if-error) so commands still work offline.

Force a cache refresh:

```bash
# Refresh a specific marketplace
apm marketplace update acme-plugins

# Refresh all registered marketplaces
apm marketplace update
```

## Registry proxy support

When `PROXY_REGISTRY_URL` is set, marketplace commands (`add`, `browse`, `search`, `update`) fetch `marketplace.json` through the registry proxy (Artifactory Archive Entry Download) before falling back to the GitHub Contents API. When `PROXY_REGISTRY_ONLY=1` is also set, the GitHub API fallback is blocked entirely, enabling fully air-gapped marketplace discovery.

```bash
export PROXY_REGISTRY_URL="https://art.corp.example.com/artifactory/github"
export PROXY_REGISTRY_ONLY=1  # optional: block direct GitHub access

apm marketplace add anthropics/skills   # fetches via Artifactory
apm marketplace browse skills           # fetches via Artifactory
```

This builds on the same proxy infrastructure used by `apm install`. See the [Registry Proxy guide](../registry-proxy/) for full configuration details.

## Manage marketplaces

Remove a registered marketplace:

```bash
apm marketplace remove acme-plugins

# Skip confirmation prompt
apm marketplace remove acme-plugins --yes
```

Removing a marketplace does not uninstall plugins previously installed from it. Those plugins remain pinned in `apm.lock.yaml` to their resolved Git sources.
