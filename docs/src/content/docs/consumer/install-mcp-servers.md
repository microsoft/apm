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

Registry metadata prefetches use up to four concurrent lookups while preserving
the order of the requested server references.

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

# Global stdio install (writes user APM + runtime config only)
apm install -g --target claude --mcp fetch -- npx -y @modelcontextprotocol/server-fetch

# Global registry install (requires one unique registry match)
apm install -g --target claude --mcp io.github.github/github-mcp-server
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

When a required registry runtime variable has a default, APM prompts once
per variable and displays that default as the suggested answer. Press Enter
to accept it or provide an override. Secret defaults remain accepted on
Enter but are never displayed. For OCI/Docker launchers, non-secret selected
values replace every `{variable}` reference across the package's runtime and
package arguments before the native config is written. VS Code renders secret
variables as target-native secret-input references instead, so secret bytes
never enter `mcp.json`. A required variable without a collected value or
default declines that target configuration; VS Code treats `workspaceFolder`
as its built-in `${workspaceFolder}` token.

Claude Code updates self-defined connections only when the native entry matches
its recorded APM baseline. Updates remove previously managed fields omitted
from the new declaration, including `env`, while preserving runtime-owned
`oauthAccount` metadata. Switching from `http` to `stdio` removes `url` and
`headers`. Modified or unverifiable entries require
[manual reconciliation](#resolve-a-connection-name-conflict); matching entries
remain byte-identical.

Managed Claude stdio updates retain a valid native-only `cwd` when both
declarations omit it; switching to HTTP removes incompatible stdio fields.
This exception permits updates, not foreign adoption or cleanup. Edits to a
declared `cwd` or other managed fields still require reconciliation.

For VS Code and Copilot-family adapters, non-container `npm`, `pypi`,
and generic packages preserve typed v0.1 `runtimeArguments` and
`packageArguments` in authored order, with exactly one semantic package
identity. Legacy `value_hint` arguments remain supported. Registry
defaults resolve normally, secret variables use target-native references,
unresolved optional groups are omitted atomically, and malformed or
unresolved required entries fail closed.

| Harness | File | Scope | Format |
|---|---|---|---|
| GitHub Copilot CLI | `.github/mcp.json` | project | JSON `mcpServers` |
| GitHub Copilot CLI | `$COPILOT_HOME/mcp-config.json` (`-g`, unset/blank: `~/.copilot/mcp-config.json`) | global | JSON `mcpServers` |
| VS Code (Copilot) | `.vscode/mcp.json` | project | JSON `servers` |
| Claude Code | `.mcp.json` (project) or `$CLAUDE_CONFIG_DIR/.claude.json` (`-g`; unset/blank: `~/.claude.json`) | both | JSON `mcpServers` |
| Cursor | `.cursor/mcp.json` (project, only if `.cursor/` exists) or `~/.cursor/mcp.json` (`-g`) | both | JSON `mcpServers` |
| Codex CLI | `.codex/config.toml` (project, only if `.codex/` exists) or `$CODEX_HOME/config.toml` (`-g`, when non-blank; otherwise `~/.codex/config.toml`) | both | TOML `[mcp_servers.*]` |
| Gemini CLI | `.gemini/settings.json` (project, only if `.gemini/` exists) or `~/.gemini/settings.json` (`-g`) | both | JSON `mcpServers` |
| Antigravity CLI | `.agents/mcp_config.json` (project, only if `.agents/` exists) or `~/.gemini/config/mcp_config.json` (`-g`) | both | JSON `mcpServers` |
| Hermes Agent | `$HERMES_HOME/config.yaml` (unset/blank: `~/.hermes/config.yaml`; explicit `--target hermes` only) | home-scoped | YAML `mcp_servers` |
| OpenCode | `opencode.json` (project, only if `.opencode/` exists) or `~/.config/opencode/opencode.json` (`-g`) | both | JSON `mcp` (v1) or `mcp.servers` (v2) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | global | JSON `mcpServers` |
| Kiro IDE | `.kiro/settings/mcp.json` (project, only if `.kiro/` exists) or `~/.kiro/settings/mcp.json` (`-g`) | both | JSON `mcpServers` |
| JetBrains Copilot | `%LOCALAPPDATA%\github-copilot\intellij\mcp.json` (Windows) or `$XDG_CONFIG_HOME/github-copilot/intellij/mcp.json` (macOS/Linux; defaults to `~/.config/github-copilot/intellij/mcp.json`) | global | JSON `servers` |

Export referenced variables in the environment that launches OpenCode. APM writes
runtime references rather than their resolved values; authored static values remain
literal. See [OpenCode variable handling](../../reference/manifest-schema/#424-variable-references-in-headers-and-env)
for supported fields, registry overlays, and syntax, and
[Repairing existing credentials](#repairing-existing-credentials) for older configs.

For OpenCode, set the top-level `enabled` field on an MCP dependency to pass a
value unchanged into that server's `opencode.json` entry. If omitted, APM keeps
the existing `true` default. APM does not validate or coerce explicit values;
OpenCode interprets them. Other targets ignore this OpenCode-only field.
Reinstall applies changes to `enabled`, including its JSON type; removing
the field restores `true`. For the OpenCode 2 layout, see
[OpenCode v2 configuration](#opencode-v2-configuration).

Global Cursor and OpenCode installs preserve your personal servers, including
ones that share a name with a package-declared server. Reinstalling and
pruning change only the entries APM wrote for that target. If you move a direct
`--mcp` server to a different `--target`, APM writes the new target and then
removes the entry from the old one; if that removal fails, the server stays in
both targets and retrying the same install removes the old entry.

If a user config is unreadable, contains JSONC, has a non-object server map, or
uses a symlink below the home directory, APM warns and fails without rewriting
it, even on a no-op reinstall. Fix the reported config and rerun the original
command with `--global` and the same `--target` selection. A home-directory
alias itself remains supported.

### OpenCode v2 configuration

OpenCode 1 and OpenCode 2 read the same `opencode.json` files, but only
OpenCode 2 understands its native layout, which nests servers under
`mcp.servers`. APM therefore chooses the layout from the target file
itself:

- If the file declares an `mcp.servers` object, APM writes each server
  there in the OpenCode 2 format.
- Otherwise APM keeps writing the OpenCode 1 layout, which OpenCode 2 also
  reads.

To opt in, add an empty map to the target file and reinstall:

```json
{
  "mcp": {
    "servers": {}
  }
}
```

In the OpenCode 2 layout, `enabled: false` is written as `disabled: true`,
and `enabled: true` or an omitted value writes no field, the OpenCode 2
default. Other values have no OpenCode 2 equivalent, so APM warns and
leaves the file unchanged. A `disabled` value in the `extra` block is
ignored, so `enabled` alone decides. To expose a server's tools directly instead of
through Code Mode, set `codemode: false` in the dependency's `extra` block;
APM writes extra fields verbatim, so use the OpenCode 2 spelling for any
other native setting, such as snake_case `oauth` fields.

APM does not convert configuration it did not write. When it switches to
`mcp.servers`, it removes an OpenCode 1 entry of the same name only if the
entry is unchanged from what APM wrote; an edited entry stays in place with
a warning, and OpenCode 2 uses the `mcp.servers` entry. Your own servers,
`mcp.timeout` and other settings are preserved, and pruning removes only
entries APM wrote. Remove `mcp.servers` before running OpenCode 1 against
the same file, since OpenCode 1 cannot read it.

## Resolve a connection name conflict

For self-defined Claude and Codex connections, a normal package update or root
override updates an unchanged managed connection. A differing native entry
is preserved, the affected target is named, and install exits `1`.
Successful writes, including registry entries written before a later
self-defined conflict, remain recorded. This is not a whole-command rollback.

If Claude has a personal `notes` connection and a package requests a different
`notes` connection for Claude and Codex, APM preserves the personal connection
even if Codex succeeds. Choose which connection to keep:

1. To keep both, rename your personal connection in the affected runtime's
   native config, leaving unrelated settings intact.
2. To replace the personal connection, remove only that native entry yourself.
3. Rerun the original install command with the same scope and targets, such as
   `apm install --global --target claude,codex`.

An identical foreign connection stays byte-identical and unowned: APM revokes
any stale ownership claim without attempting target retirement.
Registry conflicts do not prevent nonconflicting self-defined updates; the
command still exits `1` and retains ownership of its successful writes.
`--force` grants no takeover permission; `--frozen` does not turn a conflicting
connection into a successful install or repair lock intent. Prune, target
narrowing, and uninstall likewise preserve entries that differ from a
trustworthy [managed baseline or receipt](../../reference/lockfile-spec/#canonical-deployment-rows).
If an older ownership record cannot be verified, rename or remove only the
affected native entry, then retry. Deleting the lockfile never authorizes
adoption of existing native entries; do not reset it or the entire native config.

These operations change connection settings, not notes or other data stored
by the MCP service.

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

At project scope, the `copilot` target writes MCP state to
`.github/mcp.json`, which Copilot CLI discovers from the repository. The
`vscode` target writes only `.vscode/mcp.json`. At global scope, `copilot`
writes `$COPILOT_HOME/mcp-config.json`, or `~/.copilot/mcp-config.json` when
`COPILOT_HOME` is unset.

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
verified APM-managed entries from that target's native config. Cleanup checks
the exact native key: retiring `@company/notes` does not touch personal `notes`.
User-authored servers and unrelated JSON or TOML settings remain unchanged.

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
`$COPILOT_HOME/mcp-config.json` when `COPILOT_HOME` is set, otherwise
`~/.copilot/mcp-config.json`; Claude Code to
`$CLAUDE_CONFIG_DIR/.claude.json` when `CLAUDE_CONFIG_DIR` is set to a
non-whitespace absolute path (both for installation and stale server cleanup). Unset or blank values use `~/.claude.json`;
relative values are rejected. Codex CLI writes to
`$CODEX_HOME/config.toml` when `CODEX_HOME` is set to a non-whitespace value or
`~/.codex/config.toml` otherwise, Cursor to `~/.cursor/mcp.json`, OpenCode to
`~/.config/opencode/opencode.json`, Gemini CLI to `~/.gemini/settings.json`,
Antigravity CLI to `~/.gemini/config/mcp_config.json`, Hermes to
`$HERMES_HOME/config.yaml` whenever selected explicitly (or
`~/.hermes/config.yaml` when unset or blank), Windsurf to
`~/.codeium/windsurf/mcp_config.json`, Kiro to `~/.kiro/settings/mcp.json`,
and JetBrains Copilot to its OS-specific user config).
When the user-scope manifest declares a `targets:` field (or the CLI passes
`--target`), only the matching runtimes receive the config write. When no CLI
target, user-scope manifest target, or saved `apm config target` restricts
targets, all detected user-scope-capable runtimes are configured.
The workspace-only VS Code target is skipped with a warning
when a mixed target set also contains a global-capable runtime. If none of the
selected targets supports user scope, the command exits `2` before changing the
user manifest, lockfile, or runtime configuration. The direct command creates
or updates `~/.apm/apm.yml`; it does not fall back to the current project's
manifest. With `--dry-run`, it previews the user-scope entry and runtime
targets without creating that manifest, lockfile, or runtime configuration.
APM leaves an existing Cursor or OpenCode config untouched and reports that
target as failed when it cannot parse the file as JSON (for example, OpenCode
JSONC comments) or when its user-scope path runs through a symlink.

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

Codex requires HTTPS for non-loopback remote endpoints. Plain HTTP is
accepted only for literal loopback addresses such as `localhost`,
`ip6-localhost`, `127.0.0.0/8`, and `::1`, which keeps local development
servers usable without sending cleartext traffic off the machine. For example:

```sh
apm install --target codex --mcp local-dev --url http://localhost:3000/mcp
```

This writes the endpoint to the Codex MCP configuration.

Codex's [native configuration schema](https://developers.openai.com/codex/config-schema.json)
does not accept an MCP `id` setting. After upgrading APM, repair older generated
entries with:

```sh
apm install --only mcp --target codex
```

Add `--global` for user-scope configuration. APM repairs only entries that
match a trustworthy saved configuration, including the historical empty
`id = ""` spelling of self-defined connections. A recorded owner name alone
does not verify an entry with a nonempty registry ID, old header spelling,
or other native edits. Such entries stay untouched and an unresolved
self-defined connection conflict makes the install incomplete.

If Codex still reports an unsupported `id` on an entry you have verified,
back up `.codex/config.toml` (or the user-scope file) and remove only that
entry's `id = ...` setting. Keep generated comments such as
`# apm-registry-id: "registry-uuid"`: APM uses them to prevent duplicate
registry installs. Regeneration may expand inline TOML tables while retaining
their values and comments; it does not change Codex's project trust settings.
If the connection still conflicts, follow
[native conflict recovery](#resolve-a-connection-name-conflict) rather than
retrying with `--force`.

`--transport` is inferred when omitted: a `--url` implies a remote
transport, a post-`--` command implies `stdio`. The mutually-exclusive
combinations (`--url` plus stdio command, `--header` without `--url`,
etc.) are rejected with exit code 2.

## Token injection: GitHub MCP server

For Cursor, env-var references are written using Cursor's native
`${env:NAME}` syntax, so referenced secret values are resolved by Cursor
when it starts the MCP server and are not written into the Cursor config for
the install scope (`.cursor/mcp.json` for a project, `~/.cursor/mcp.json`
with `-g`). Explicit static values in `mcp.env` remain static and are written
as authored; keep secrets out of those values.

APM translates supported environment-variable references using each
target's native syntax. It also injects one specific GitHub credential
automatically when the server is recognized as the GitHub MCP server:

When an adapter using the shared GitHub auth path writes a remote MCP
config and the server is identified as the GitHub MCP server, APM selects
a token source and applies the target's auth behavior described below.

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

The token is selected from this chain (first non-empty wins):

1. `GITHUB_COPILOT_PAT`
2. `GITHUB_TOKEN`
3. `GITHUB_APM_PAT`
4. `GITHUB_PERSONAL_ACCESS_TOKEN` (Copilot CLI compat)

If the manifest declares a nonempty string `Authorization` value (case-insensitive header name),
that explicit value takes precedence over automatic GitHub authentication.
Registry-provided headers alone do not disable automatic authentication.
For Copilot and Cursor, this also applies to dictionary-shaped headers
accepted from a custom registry: manifest overrides retain their priority.
This compatibility does not certify the custom response against an upstream
registry schema.
Environment references are translated according to the target's interpolation
rules. This MCP selection is environment-only: it does not use repository
authentication's per-org variables or credential helpers.

For a target that supports runtime environment substitution, automatic
GitHub auth writes a target-native reference to the selected variable
(for example, `${GITHUB_TOKEN}` or `${env:GITHUB_TOKEN}`); the resolved
credential value is not written into the generated config. Literal-only
targets keep their existing automatic-token behavior. If none of the
listed variables is set, no automatic header is added.

Reinstalling the same declaration does not automatically repair credentials
already written to runtime config. Follow
[Repairing existing credentials](#repairing-existing-credentials) to preserve
custom settings and rotate exposed credentials.
For other authenticated remote servers, set headers explicitly with
`--header Authorization="Bearer ${MY_TOKEN}"`.

## Updating and replacing a server

Re-run `apm install --mcp NAME ...` against an existing declaration in `apm.yml`:

| Situation | Behaviour |
|---|---|
| New `NAME` | Appended to `dependencies.mcp`. |
| Existing `NAME`, identical config | No-op. Logs `unchanged`. |
| Existing `NAME`, different config, TTY | Prompts to replace. |
| Existing `NAME`, different config, CI | Refuses with exit 2. Re-run with `--force`. |

Use `--dry-run` to preview the manifest change without writing.
`--force` permits replacing that declaration, not taking over a conflicting
native connection. Follow
[native conflict recovery](#resolve-a-connection-name-conflict) for the latter.

### Repairing existing credentials

First replace any authored static credential in `apm.yml` with an environment
reference. Otherwise regenerating the entry writes that static value again.
Follow the target's [interpolation rules](../../reference/manifest-schema/#424-variable-references-in-headers-and-env)
when making variables available and editing its config.

For Cursor, open the config for the scope you installed into (`.cursor/mcp.json`
for a project, `~/.cursor/mcp.json` with `-g`; see
[What `apm install` writes to disk](#what-apm-install-writes-to-disk)). Replace
only the affected header or `env` values with `${env:NAME}` references.
Preserve the authorization scheme, for example `Bearer ${env:GITHUB_TOKEN}`,
and make the variable available to Cursor. Keep all other fields and servers,
and inspect the diff. An ordinary reinstall of the same declaration does not
perform this repair.

If you instead regenerate an entry, save its custom fields first, remove only
that server entry, reinstall, and restore those fields without restoring the
old credential. Do not delete the whole config file. Rotate any credential
exposed in a committed or shared config; editing the file does not revoke it.

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
