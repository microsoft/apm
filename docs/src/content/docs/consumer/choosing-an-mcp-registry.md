---
title: "Choosing an MCP registry"
description: "Pick the MCP registry APM resolves server names from, and know which registries are curated."
---

`apm install --mcp NAME` and registry entries under `dependencies.mcp` resolve
server names against one MCP registry. Registries differ in who may publish and
whether anyone reviews entries. Pick the one that matches the trust you need.

## Registry options

| Registry | Who publishes | Curated |
|---|---|---|
| GitHub MCP Registry, `https://api.mcp.github.com` (APM default) | Entries selected by GitHub | Yes |
| Official MCP Registry, `https://registry.modelcontextprotocol.io` | Any publisher who verifies a namespace | No, open publishing |
| Other public catalogs | Depends on the operator | Depends on the operator |
| Private or enterprise registries | Your organization | Your organization's policy |

- **GitHub MCP Registry.** APM's built-in default. A curated catalog, so a
  server can exist elsewhere before it appears here.
- **Official MCP Registry.** Run by the Model Context Protocol project. Its
  FAQ describes it as a source for downstream catalogs and aggregators rather
  than for direct use by clients, so entries are not reviewed for you.
- **Other public catalogs.** Any service that implements the MCP Registry API.
  Check who operates it and how entries are reviewed before relying on it.
- **Private or enterprise registries.** Self-hosted implementations of the MCP
  Registry API, governed by your organization.

## When a server is not found

APM never switches registries on its own. When `apm install --mcp NAME` finds
no match, the error names the registry that was searched and shows how to
continue:

```text
[x] Server(s) not found in registry https://api.mcp.github.com: NAME
[i] Search this registry: apm mcp search "NAME"
[i] Use another registry (replace <URL>): apm install --mcp "NAME" --registry <URL>
[i] Or save it for every lookup: apm config set mcp-registry-url <URL>
[i] Choosing an MCP registry: https://microsoft.github.io/apm/consumer/choosing-an-mcp-registry/
```

The most common cause is a typo, so search the same registry first. If the
server lives in another registry you trust, choose it explicitly. The example
keeps any `--mcp-version`, `--target`, and `--global` you passed. After a
`--registry` miss, the search line says to set `MCP_REGISTRY_URL` first, since
`apm mcp search` has no `--registry` flag. After an `MCP_REGISTRY_URL` miss,
the `Or ...` line points at that variable instead of `apm config`. Misses for
`dependencies.mcp` entries in `apm.yml` show a shorter `apm mcp search` hint.

## Select a registry

```bash
# One install; the entry records this registry in apm.yml
apm install --mcp NAME --registry <URL>

# Every lookup on this machine
apm config set mcp-registry-url <URL>

# One shell session or CI job
export MCP_REGISTRY_URL=<URL>
```

Precedence is `--registry`, then `MCP_REGISTRY_URL`, then
`apm config set mcp-registry-url`, then the default. A per-dependency
`registry:` URL in `apm.yml` overrides the chain for that entry. See
[resolution order](../../reference/cli/config/#resolution-order) for the
`http://` rules and [Install MCP servers](../install-mcp-servers/) for the
rest of the MCP install flow.
