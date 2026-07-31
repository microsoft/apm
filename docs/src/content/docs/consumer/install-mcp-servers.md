---
title: "Install MCP servers"
description: "Keep MCP target ownership portable across your team's machines with declarations in apm.yml."
---

`apm install` is the same driver for two artifact kinds: APM packages
(see [Install Packages](../install-packages/)) and MCP servers. This
page covers MCP servers: how you declare them, what gets written to
each runtime, and how tokens get injected.

## One-line answer

```bash
apm install --mcp io.github.github/github-mcp-server
```

This adds one entry under `dependencies.mcp:` in `apm.yml`. APM then attempts
runtime-specific MCP config writes for targets that pass selection, scope, and
adapter prerequisites.

## The `mcp:` section in apm.yml

The two sections have different propagation rules. In the root project,
`apm install` activates both sections for the author's environment. A
direct or transitive dependency package contributes only
`dependencies.mcp`; its `devDependencies.mcp` entries never enter the
consumer's target config, lockfile provenance, or audit baseline.
This keeps package-author debug servers out of consumer tool lists.

MCP servers live under `dependencies.mcp:` (or
`devDependencies.mcp:`). Three forms are valid -- pick the one that
matches the source you have:

```yaml
dependencies:
  mcp:
    # 1. Registry reference (bare string)
    - io.github.github/github-mcp-server

    # 2. Self-defined stdio (local process)
    - name: filesystem
      registry: false
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]

    # 3. Self-defined remote (HTTP / SSE)
    - name: linear
      registry: false
      transport: http
      url: https://mcp.linear.app/sse
      headers:
        Authorization: "Bearer ${LINEAR_TOKEN}"

    # 4. Self-defined remote with harness-specific extra keys
    - name: slack
      registry: false
      transport: http
      url: https://mcp.slack.com/mcp
      oauth:
        clientId: "<pre-registered-client-id>"
        callbackPort: 3118
```

Unknown keys like `oauth` above are **passthrough fields**: they are
preserved and written into the generated config for every harness you
install (so a Claude Code `oauth` block reaches all targets; harnesses
that do not recognise it ignore it). Keys that collide with a modeled
field (`command`, `url`, `headers`, `env`, ...) are rejected with a
warning so they cannot redirect a server. See
[Manifest Schema](../../reference/manifest-schema/) for the full rules.

The full grammar (overlays, `${input:...}` variables, `tools:`
allowlists, `package:` selection) is in
[Package Anatomy](../../concepts/package-anatomy/).

## Adding a server from the CLI

`apm install --mcp NAME` writes the entry into `apm.yml` for you,
then runs install. Three shapes match the three manifest forms:

```bash
# Registry
apm install --mcp io.github.github/github-mcp-server

# stdio (everything after `--` is the spawn command)
apm install --mcp filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace

# Remote
apm install --mcp linear --transport http --url https://mcp.linear.app/sse
```

`apm mcp install NAME ...` is an alias that forwards to the same code
path. The `apm mcp` group also provides `search`, `list`, and `show`
for discovery -- see the [CLI reference](../../reference/cli/install/).

## What `apm install` writes to disk

For every selected target that passes its scope and adapter prerequisites,
`apm install` writes a runtime-specific MCP config file. The schemas differ;
the `apm.yml` source of truth does not.

Registry-declared environment variables honor the registry's
`required` flag. Servers with optional auth install without token
prompts until you choose to configure one. See the
[manifest schema reference](../../reference/manifest-schema/#424-variable-references-in-headers-and-env)
for the full required-vs-optional runtime config rule.

MCP Registry v0.1 container packages use `registryType: oci`. APM
selects Docker automatically, keeps Docker run options before the image,
and places package arguments after the image. Copilot, Codex, Gemini,
and related adapters prefer packages in `npm`, OCI, then PyPI order.
VS Code prefers `npm`, PyPI, then OCI. OCI packages require Docker to be
available when the harness starts the server; no per-target launcher
configuration is needed.

| Harness | File | Scope | Format |
|---|---|---|---|
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` | global | JSON `mcpServers` |
| VS Code (Copilot) | `.vscode/mcp.json` | project | JSON `servers` |
| Claude Code | `.mcp.json` (project) or `$CLAUDE_CONFIG_DIR/.claude.json` (`-g`; unset/blank: `~/.claude.json`) | both | JSON `mcpServers` |
| Cursor | `.cursor/mcp.json` | project (only if `.cursor/` exists) | JSON `mcpServers` |
| Codex CLI | `.codex/config.toml` (project, only if `.codex/` exists) or `$CODEX_HOME/config.toml` (`-g`, when non-blank; otherwise `~/.codex/config.toml`) | both | TOML `[mcp_servers.*]` |
| Gemini CLI | `.gemini/settings.json` (project, only if `.gemini/` exists) or `~/.gemini/settings.json` (`-g`) | both | JSON `mcpServers` |
| Antigravity CLI | `.agents/mcp_config.json` (project, only if `.agents/` exists) or `~/.gemini/config/mcp_config.json` (`-g`) | both | JSON `mcpServers` |
| OpenCode | `opencode.json` | project (only if `.opencode/` exists) | JSON `mcp` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | global | JSON `mcpServers` |
| Kiro IDE | `.kiro/settings/mcp.json` (project, only if `.kiro/` exists) or `~/.kiro/settings/mcp.json` (`-g`) | both | JSON `mcpServers` |
| JetBrains Copilot | `%LOCALAPPDATA%\github-copilot\intellij\mcp.json` (Windows) or `$XDG_CONFIG_HOME/github-copilot/intellij/mcp.json` (macOS/Linux; defaults to `~/.config/github-copilot/intellij/mcp.json`) | global | JSON `servers` |

## How `targets:` gates which configs get written

MCP install resolves targets in this order:

1. Explicit CLI selection: `--runtime` (legacy, one runtime) or `--target`
   (one or more targets).
2. Canonical `targets:` / `target:` values declared in `apm.yml`.
3. The saved `apm config set target ...` default.
4. Machine discovery, only when the manifest and saved config do not
   restrict targets.

`--exclude` narrows whichever set wins. Progress output names that
post-exclusion selection; project, scope, and adapter gates can still skip a
write.

The same effective decision drives package, MCP, and LSP phases; APM does
not re-resolve each phase independently.

**Portability boundary:** A committed target list makes lockfile MCP ownership
deterministic across machines with different installed harnesses. If `targets:`
is omitted (or a legacy `all` declaration is folded to omission), machine
discovery is intentional and ownership can follow each machine's installed
harnesses. Declare targets when teammates or CI must produce the same MCP
ownership.

When a runtime is outside the active target set, APM does NOT write
its MCP config -- and announces the drop on stdout so you can confirm
the gate took effect:

```text
[i] Skipped MCP config for claude, codex  (active targets: copilot)
```

On reinstall, removing a previously configured target also removes the
APM-managed server entries from that target's native config. User-authored
servers and unrelated JSON or TOML settings remain unchanged.

This single rule replaces two older ones that used to coexist:

- A "directory opt-in" carve-out for Cursor / Gemini / OpenCode -- now
  redundant, because `targets:` (or auto-detection) drives the gate
  for those runtimes too.
- The pre-#1335 silent skip path, which dropped non-listed runtimes
  without telling you.

A malformed `targets:` field (both `target:` and `targets:` set,
`targets: []`, or an unknown target name) fails closed before machine discovery:
no MCP files are written and an `[x]` error names the field to fix. A greenfield
project with no `targets:`, no `--target` flag, no saved target, AND no detected
signals (`.github/copilot-instructions.md`, `.cursor/`, etc.) also fails closed
with the same `[x]` voice -- consistent with how `apm install` treats the same
input. The command exits non-zero before adding a direct `--mcp` entry to
`apm.yml` or deploying package files. Pin a target with `--target`, declare one
in `apm.yml`, or save one with `apm config set target <value>`. A native MCP
config write failure also exits non-zero and names the target path or
permissions to check. (#1335)

`apm install -g --mcp NAME` routes the write to each runtime's
user-scope MCP config (for example, Copilot CLI to
`~/.copilot/mcp-config.json`, Claude Code to
`$CLAUDE_CONFIG_DIR/.claude.json` when `CLAUDE_CONFIG_DIR` is set to a
non-whitespace absolute path. Unset or blank values use `~/.claude.json`;
relative values are rejected. Codex CLI writes to
`$CODEX_HOME/config.toml` when `CODEX_HOME` is set to a non-whitespace value or `~/.codex/config.toml` otherwise, Gemini CLI to `~/.gemini/settings.json`, Antigravity CLI to `~/.gemini/config/mcp_config.json`, Windsurf to
`~/.codeium/windsurf/mcp_config.json`, Kiro to `~/.kiro/settings/mcp.json`,
and JetBrains Copilot to its OS-specific user config). When the
package declares a `targets:` field (or the CLI passes `--target`),
only the matching runtimes receive the config write. When neither
restricts targets, all detected user-scope-capable runtimes are
configured. Workspace-only runtimes (VS Code, Cursor, OpenCode) are
skipped at user scope.

## stdio vs HTTP servers

MCP defines two transport families. APM exposes both:

- **stdio** -- APM (and your harness) spawns a local process and
  speaks MCP over its stdio. Requires `command:` and optional
  `args:`. Use `--env KEY=VALUE` (repeatable) for environment
  variables. Servers do not go through a shell, so `$VAR` and
  backticks in `args` are passed literally.
- **http / sse / streamable-http** -- APM points your harness at a
  remote endpoint. Requires `url:` (http or https only -- websockets
  and `file://` are rejected). Use `--header KEY=VALUE` (repeatable)
  for HTTP headers such as `Authorization`.

`--transport` is inferred when omitted: a `--url` implies a remote
transport, a post-`--` command implies `stdio`. The mutually-exclusive
combinations (`--url` plus stdio command, `--header` without `--url`,
etc.) are rejected with exit code 2.

## Token injection: GitHub MCP server

APM does not template arbitrary environment variables into MCP config
files (your harness does that at runtime). It does inject one
specific credential automatically:

When the Copilot CLI adapter writes a remote MCP config and the
server is identified as the GitHub MCP server, APM resolves a token
and adds an `Authorization: Bearer <token>` header.

The server is identified as "GitHub" only when it satisfies **both** of
these narrow checks
([copilot.py:1004](https://github.com/microsoft/apm/blob/main/src/apm_cli/adapters/client/copilot.py#L1004)):

1. The server name (case-insensitive) is one of:
   `github-mcp-server`, `github`, `github-mcp`,
   `github-copilot-mcp-server`.
2. **And** the parsed URL hostname matches the GitHub host allowlist
   (`github.com`, `*.github.com`, `githubcopilot.com` hosts, and
   registered GHES hostnames).

This is a parsed-host allowlist on hostname, not a substring check.
A URL like `https://github.com.evil.example` does not match because
the parsed hostname is `github.com.evil.example`, not `github.com`.

The token is resolved from this chain (first non-empty wins):

1. `GITHUB_COPILOT_PAT`
2. `GITHUB_TOKEN`
3. `GITHUB_APM_PAT`
4. `GITHUB_PERSONAL_ACCESS_TOKEN` (Copilot CLI compat)

If none are set, no header is injected and the server is written
without auth -- you will get an unauthenticated request at runtime.
For other authenticated remote servers, set headers explicitly with
`--header Authorization="Bearer ${MY_TOKEN}"`.

## Updating and replacing a server

Re-run `apm install --mcp NAME ...` against an existing entry:

| Situation | Behaviour |
|---|---|
| New `NAME` | Appended to `dependencies.mcp`. |
| Existing `NAME`, identical config | No-op. Logs `unchanged`. |
| Existing `NAME`, different config, TTY | Prompts to replace. |
| Existing `NAME`, different config, CI | Refuses with exit 2. Re-run with `--force`. |

Use `--dry-run` to preview the manifest change without writing.

## Sibling commands

The `apm mcp` group is for discovery and standalone install:

```
apm mcp search <query>    # search the configured registry
apm mcp list              # list available servers
apm mcp show <name>       # detailed server info
apm mcp install <name>    # alias for `apm install --mcp <name>`
```

Full flag tables and exit codes: [CLI reference](../../reference/cli/install/).

## Next

- Authoring an MCP server as a primitive of your own package -- see
  the producer ramp.
- Lockfile and trust boundary for transitive MCP servers --
  [Lifecycle](../../concepts/lifecycle/).
