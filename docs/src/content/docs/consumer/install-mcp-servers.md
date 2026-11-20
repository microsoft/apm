---
title: Add MCP and LSP servers
description: Declare MCP and LSP servers once in apm.yml; APM writes each harness's config, keeps secrets as references, and holds back servers you did not choose.
---

Use this page to give every teammate the same MCP servers without
hand-editing a JSON file per tool. An **MCP server** is a program, local or
remote, that gives an agent tools (GitHub, a database, a browser). Each
harness (the agent tool: GitHub Copilot, Claude Code, Cursor, Codex) reads
servers from its own config file. You declare a server once in `apm.yml`;
`apm install` writes every harness's file and records what it wrote in
`apm.lock.yaml`.

## Add a server

```bash frame="terminal"
# From an MCP registry
apm install --mcp io.github.github/github-mcp-server

# A local process: everything after -- is the command
apm install --mcp fetch -- npx -y @modelcontextprotocol/server-fetch

# A remote endpoint
apm install --mcp linear --transport http --url https://mcp.linear.app/mcp
```

Each command adds an entry to `apm.yml` and installs it:

```diff title="apm.yml"
 dependencies:
-  mcp: []
+  mcp:
+    - name: fetch
+      registry: false
+      transport: stdio
+      command: npx
+      args:
+        - -y
+        - '@modelcontextprotocol/server-fetch'
```

`--transport` (`stdio`, `http`, `sse`, `streamable-http`) is inferred from
`--url` or the command when omitted. Add `--env KEY=VALUE` for a local
process and `--header KEY=VALUE` for a remote one. Rerunning with a different
definition asks before replacing it (in CI it exits 2; pass `--force`).

## Choosing an MCP registry

A registry name such as `io.github.github/github-mcp-server` resolves against
one MCP registry:

| Registry | Who publishes |
|---|---|
| GitHub MCP Registry, `https://api.mcp.github.com` (default) | Entries selected by GitHub |
| Official MCP Registry, `https://registry.modelcontextprotocol.io` | Anyone who verifies a namespace; not reviewed |
| Your organization's registry | Your organization |

APM never switches registries on its own: a miss names the registry it
searched. Choose another with `--registry URL` (recorded on the entry as
`registry:`), `MCP_REGISTRY_URL` for a shell or CI job, or
`apm config set mcp-registry-url URL` for your machine, in that precedence.
`apm mcp search QUERY` searches the selected registry.

## How `targets:` gates which configs get written

APM writes for the harnesses in your [`targets:`](../choose-harnesses/)
and prints the ones it skipped:

```text frame="none"
[i] Skipped MCP config for claude, codex  (active targets: copilot)
```

| Harness | Project file | User file (`-g`) |
|---|---|---|
| GitHub Copilot | `.mcp.json` | `$COPILOT_HOME/mcp-config.json` (`~/.copilot/`) |
| Claude Code | `.mcp.json` (when `.claude/` exists) | `$CLAUDE_CONFIG_DIR/.claude.json` (`~/.claude.json`) |
| Cursor | `.cursor/mcp.json` | `~/.cursor/mcp.json` |
| Codex | `.codex/config.toml` | `$CODEX_HOME/config.toml` (`~/.codex/`) |

Gemini, OpenCode, Kiro, Windsurf and others: see the
[Targets matrix](../../reference/targets-matrix/#mcp-servers). Each harness
gets its own keys for remote transports; a harness that cannot use a
transport (for example SSE on Codex) is skipped with a warning.

### Limit a server to some harnesses

Add `targets:` to an MCP entry to write it only for those harnesses:

```yaml title="apm.yml"
  mcp:
    - name: io.github.github/github-mcp-server
      transport: http
      targets: [copilot, claude]
```

APM uses the list to decide where to write the server; it never copies
`targets:` into a harness's config. An install scoped with `--target` leaves
servers that other targets declared in place.

:::caution[Breaking in 0.37]
GitHub Copilot servers move from `.github/mcp.json` and `.vscode/mcp.json` to
root `.mcp.json`, which Claude Code also reads, so Copilot's `tools`
allow-list does not restrict Claude Code. The next `apm install` moves
entries APM wrote; entries you edited stay and APM names them. VS Code's
deprecated Local mode no longer gets servers. Preview with `apm install --dry-run`,
which lists the servers that move and those only removed. Then run `apm install`,
review static `env:` values, and commit. APM empties `.github/mcp.json` rather than
deleting it, and `apm uninstall` reports servers it removes from retired files.
:::

### OpenCode 2 configuration

If `opencode.json` already has an `mcp.servers` object, APM writes the
OpenCode 2 layout there. Otherwise it writes the OpenCode 1 `mcp` layout,
which OpenCode 2 also reads. To opt in, add `"mcp": {"servers": {}}` and
reinstall. APM never converts entries it did not write.

## Keep secrets out of config files

Write secrets as references, never values:

```yaml title="apm.yml"
    - name: linear
      registry: false
      transport: http
      url: https://mcp.linear.app/mcp
      headers:
        Authorization: "Bearer ${LINEAR_TOKEN}"
```

APM writes each harness's own reference syntax (Cursor gets
`${env:LINEAR_TOKEN}`) so the harness reads the value when it starts the
server. Static values are written as authored. Registry servers prompt once
for required variables. Harness-by-harness support:
[MCP variable compatibility](../../reference/mcp-variable-compatibility/).

### Token injection (GitHub MCP server)

When a remote server is named `github-mcp-server`, `github`, `github-mcp` or
`github-copilot-mcp-server` and its URL host is a GitHub host, APM adds an
`Authorization` header from the earliest variable set among
`GITHUB_COPILOT_PAT`, `GITHUB_TOKEN`, `GITHUB_APM_PAT` and
`GITHUB_PERSONAL_ACCESS_TOKEN`. It writes a reference where the harness
supports one. An `Authorization` header in `apm.yml` wins. Other servers need
an explicit `--header`.

### Repairing existing credentials

Older releases could write resolved values. Start by replacing any static secret
in `apm.yml` with a `${VAR}` reference. For Claude Code's project
`.mcp.json`, the next `apm install` replaces the values APM recorded that it wrote;
otherwise it exits with an error naming each field. For Cursor and others,
edit the value to the harness's reference syntax by hand, keeping other
fields. Rotate any credential that was committed: editing the file does not
revoke it.

## Servers that come from dependencies

A plugin APM installs whole to `.agents/plugins/` keeps its MCP and LSP
servers: Claude Code and Copilot load them from the plugin under your
organization's MCP policy, and APM writes no entry for them in those harnesses;
other targets still get APM's copy
([Install plugins](../install-plugins/#native-plugins)).


A server declared by a package you depend on is code that runs on your
machine. APM holds back a server from a transitive package when it is
self-defined, names its own registry or overrides configuration, and tells you
how to admit it. Re-declare it in your `apm.yml`, or
[approve the package](../approve-executables/) and commit `apm.yml`.
`--trust-transitive-mcp` admits it for one run, and `apm audit --ci`
still fails it. A dependency's `devDependencies` servers never reach you.

## Resolve a connection name conflict

If a harness config already holds a different server under the same name,
APM keeps yours, names the target and exits 1. Choose one:

1. Rename your entry in that harness's config to keep both.
2. Or delete your entry, leaving the rest of the file, so APM's entry replaces it.
3. Rerun the same command with the same scope and targets.

`--force` does not take over a connection, and deleting the lockfile does not
either.

## LSP servers

An LSP server gives an agent code intelligence for a language. Declare it and
install the server binary yourself; APM writes the config, not the binary:

```yaml title="apm.yml"
dependencies:
  lsp:
    - name: gopls
      command: gopls
      args: ["serve"]
      extensionToLanguage:
        ".go": go
```

APM writes LSP config for Claude Code
(`.claude/skills/apm-lsp/.claude-plugin/plugin.json`, Claude Code 2.1.157 or
later; trust the workspace, then run `/reload-plugins`), Copilot CLI
(`.github/lsp.json`) and OpenCode (`opencode.json`). Fields:
[Manifest schema](../../reference/manifest-schema/#43-dependencieslsp--listlspdependency).

## Verify

```bash frame="terminal"
cat .mcp.json
```

```json frame="none"
{
  "mcpServers": {
    "fetch": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-fetch"
      ]
    }
  }
}
```

Your harness still asks you to trust new project servers; APM does not
answer that prompt for you.

:::tip[Other options]
`apm mcp list`, `search` and `show` browse a registry. `--only mcp` installs
servers and skips packages. `--exclude cursor` skips one harness. Every flag:
[`apm install`](../../reference/cli/install/) and [`apm mcp`](../../reference/cli/mcp/).
:::
