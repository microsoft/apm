---
title: apm approve
description: Approve executable primitives from dependency packages.
sidebar:
  order: 25
---

## Synopsis

```bash
apm approve [PACKAGE_REF...] [OPTIONS]
apm policy explain <PACKAGE_REF>
```

## Description

When the executable gate is enabled, APM blocks unapproved executable
primitives (hooks, `bin/` executables, self-defined MCP servers, LSP servers,
and canvas extensions) from dependency packages. Trust is expressed through
one noun, `executables`, across three layers:

| Layer | Store | Who manages it | Committed? | Authority |
|-------|-------|----------------|------------|-----------|
| Project | `apm.yml` `executables.{allow,deny}` | Maintainer / CI setup (`apm approve`/`apm deny`) | Yes | Admin (shared) |
| User | `$APM_HOME/config.json` `executables.{allow,deny}` | `apm approve --user` / `apm deny --user` | No | Lowest; below org/project deny |
| Org | `apm-policy.yml` `executables:` | Org admin | Yes (policy repo) | Ceiling on deny |

`apm approve` adds a grant; [`apm deny`](../deny/) adds a block. By default,
both commands write the **project** `apm.yml` (committed, so the whole team
inherits the decision). `--user` writes your personal
`$APM_HOME/config.json` instead -- a machine-local grant with lower authority than
org or project denies. Use `apm deny --user` to narrow trust on one machine.

Text primitives (skills, agents, instructions) are never gated. Local project
content (the root `.apm/` directory) is always trusted.

### What is gated

| Type | Gated | Notes |
|------|-------|-------|
| Hooks (`.apm/hooks/`, `hooks/`) | Yes | Auto-fire in IDE on lifecycle events |
| Bin executables (`bin/`) | Yes | Deployed to agent PATH via symlinks |
| MCP servers (self-defined) | Yes | `registry: false` servers write to IDE MCP config |
| LSP servers | Yes | Supported runtimes may start generated server commands automatically |
| Canvas extensions (`.apm/extensions/`) | Yes | Deploys executable Node.js to IDE extensions |
| Text primitives (skills, agents, instructions) | No | No code execution risk |

### Precedence (deny-wins, first match wins)

The install gate and `apm audit` resolve trust through one shared ladder. The
first matching rung decides:

```
1. org deny_all / org deny   -> denied (absolute ceiling)
2. user deny                 -> denied (narrowing)
3. project deny              -> denied (committed narrowing)
4. project allow             -> allowed
5. user allow                -> allowed
6. org recommend             -> allowed (user-overridable)
7. (no match)                -> gated pending approval (denied but approvable)
```

Deny always wins. The org layer is the ceiling on deny -- personal consent
cannot widen past an org or project deny. The default (rung 7) is **gated
pending approval**, not a hard deny: a package with executables and no opinion
anywhere is parked until you approve it, and `apm install` still succeeds (see
[`apm install`](../install/)).

There is no `enforce` mandate runtime or cryptographic signing in this release.
An org `executables.enforce` rung degrades to `recommend` (allowed but still
overridable by a deny). Local bundle hook, MCP, LSP, and canvas approvals are bound to
the exact SHA-256 content key printed by `apm install`.

A dependency redirected by a [`replace:` rule](../../../producer/test-packages/#swap-your-copy-in-across-the-graph)
is trusted by its selected source, never by the original. `apm approve` and
`apm deny` accept either name and write the selected source key, for example
`../local-hooky` or `my-org/hooky-fork#patched`. `apm deny` also denies the
original name, so removing the `replace:` rule does not revive an old grant.

### Pinned approvals

`apm approve` pins `hooks` and `mcp` grants to content: each hook file and
each self-defined MCP server gets a SHA-256 fingerprint of what runs. `bin`,
`lsp`, and `canvas` stay approved by type.

- **MCP server**: transport, command, args, env (keys plus values or `${VAR}`
  references), url, headers, cwd, registry, version, package, tools, enabled,
  and extra keys. A registry-resolved server from a transitive package that
  sets its own `registry:` or an overlay also binds the resolved registry
  entry, so pinning it needs registry access.
- **Hook file**: the hook JSON document plus the bytes of every script it
  references that APM copies. Code a hook downloads at run time (`npx`,
  `curl`) or loads indirectly is not covered.

Each item is then admitted in this order (first match wins):

1. Deny rungs 1-3 above -> parked (`denied`). Deny wins over every grant,
   including pins and `--trust-transitive-mcp`, for self-defined MCP servers,
   hooks, and transitive MCP servers that need trust.
2. A pin for the item -> deploys if the fingerprint matches, otherwise parked
   (`definition-changed`). A project pin decides alone; a user pin applies
   only when the project has no pin for that item.
3. A transitive MCP server that is self-defined, sets its own `registry:`, or
   carries an overlay, with no matching pin -> parked
   (`untrusted-transitive`). `--trust-transitive-mcp` admits it for that run
   only. It never admits a server whose pin no longer matches; a changed
   definition always needs `apm approve`.
4. Type-level rungs 4-7 above.

When an approved definition changes upstream, install parks the item and
warns `Parked MCP server 'kit-docs' (acme/agent-kit): definition changed since
approval` (or `Parked hook file '<path>' ...`), naming the review and
re-approve commands.

A type-level `hooks: true` or `mcp: true` grant still deploys any content, and
every install warns once that it is `not pinned to content`. Run
`apm approve <package>` to pin it. `mcp: true` never admits a transitive MCP
server. A Claude, Copilot or Codex native plugin is loaded intact by the
harness, so `apm approve` records ONE item for it, `plugin: sha256:<64 hex>`,
over the path and bytes of every file in the plugin root (as installed in
`apm_modules/`). Executable bits are left out so an approval committed on macOS
or Linux also holds on Windows; APM still checks file modes on each machine
when it deploys the copy. Any byte change parks the whole plugin: it is not
deployed or declared, and
install warns `Parked native plugin <pkg>: content changed since approval`
with the re-approve command. Per-file hook pins never admit a native plugin.
A plugin that runs files under its root `.apm/` or `apm.yml` that are not
copies of pinned files cannot be pinned: `apm approve` refuses it (exit 1)
with `APM cannot pin files under .apm/ or apm.yml`, and install parks it.
A denied plugin points at `apm policy explain <pkg>`.

### The gate opt-in

The gate is enabled when any layer opts in: the project declares an
`executables:` block (even empty `{}`), or the org policy carries a non-empty
`executables:` block. Without any opt-in, executables deploy unconditionally
(backward-compatible).

The first project approval creates that block. When neither `apm.yml` nor org
policy enables the gate yet, `apm approve` lists the other installed packages
the next install will park and asks before writing:

```
[!] This approval turns on the executable gate. The next install will park 1 other package:
      acme/tools (bin)
    Review: apm approve --pending  |  Approve them: apm approve acme/tools
    Continue? [y/N]:
```

Without a TTY it writes and keeps the warning; answering `N` writes nothing
and exits 1. `--user` never turns the gate on.

### Approving a denied package

Naming a package lifts its deny in the store you write (`apm.yml`, or
`$APM_HOME/config.json` with `--user`), the mirror of `apm deny` dropping the
matching allow: approve prints `Cleared deny for <pkg>`. `--all` and
`--recommended` skip a package that any layer denies (project, `--user` or org
policy), say why, write no approval for it, and approve the rest. When a named
package is still blocked by a deny in another layer, approve exits 1 and names
that layer and how to lift it.

## Options

### `apm approve`

| Flag | Description |
|------|-------------|
| `PACKAGE_REF` | One or more packages to approve (e.g. `owner/repo`). |
| `--pending` | List packages with unapproved or changed executables, one line per item with its state. |
| `--all` | Pin and approve every package `--pending` lists. |
| `--recommended` | Pin and approve the org `executables.recommend` set. |
| `--list` | Show the fleet-level effective trust decision and deciding layer per installed package. |
| `--user` | Write the grant, in the same pinned shape, to `$APM_HOME/config.json` instead of `apm.yml`. Works outside a project, for packages `apm install -g` installed. |

#### Output

`apm approve <package>` lists what the package runs, one line per item: type,
item, summary, and the first 8 hex of the fingerprint. MCP summaries show the
stdio command and args or the http URL; env keys are shown, values only for
`${VAR}` references and code-loading keys such as `NODE_OPTIONS`,
`LD_PRELOAD`, `DYLD_*`, and `PYTHONPATH`; headers show names only.

```
$ apm approve acme/agent-kit
[i] acme/agent-kit declares:
      hooks  .apm/hooks/lint-on-save.json  bash ./scripts/lint.sh  ca978112
      mcp    kit-docs                      stdio  node ./server.js  (env: DOCS_TOKEN=${DOCS_TOKEN})  3e23e816
[+] Approved acme/agent-kit at these exact definitions (1 hook file, 1 MCP server)
[i] Updated apm.yml (1 approved). Run 'apm install' to deploy; commit apm.yml to share.
```

Re-approving a type-level `true` grant also prints
`acme/agent-kit hooks: was approved for any content; now pinned to 1 hook file`.
Re-approving replaces the package's pins with the current definitions.

`apm approve` refuses a package when:

- its installed files differ from `apm.lock.yaml`: run `apm install`, then
  retry;
- an item cannot be pinned, for example a transitive registry server while the
  MCP registry is unreachable (`needs registry access to pin`).

It exits 1 when any requested package was refused, or when you decline the
first-approval prompt (`Nothing written.`), so a script never commits an
approval that was not recorded. The first pinned approval in a project also
prints `Teammates need APM 0.37 or later to read pinned approvals.`

`--pending` marks each item `[approved]`, `[approved, any content]`,
`[changed since approval]`, `[denied]`, or `[not approved]`. `--list` labels
`hooks` and `mcp` grants `pinned` or `any content`, for example
`hooks[+:project-allow, pinned]`.

### `apm policy explain`

`apm policy explain <PACKAGE_REF>` prints the effective executable-trust
decision for a package: allowed or blocked per executable type, the deciding
policy layer, and any shadowed (overridden) lower-authority layers. It is a
subcommand of the [`apm policy`](../policy/) group -- the per-package companion
to `apm policy status` (the policy-chain view).

```bash
apm policy explain owner/repo
```

For a fleet-level view, `apm doctor` runs an executable-trust drift check that
flags any package allowed locally but denied by org policy and points to `apm
policy explain` for the per-package detail.

## Store format

`apm approve` writes one entry per package, keyed by the version-blind
identity `apm approve --pending` prints (`owner/repo`, `./pkgs/x`,
`local:pkgs/leaf`). `hooks` and `mcp` map each item (package-relative hook
file path, or server name) to its full `sha256:<64 hex>` fingerprint; other
types are `true`. A native plugin gets one `plugin:` fingerprint over its
whole root; approve also records its hook and MCP item pins, so a split
install of the same package (a target that copies files) stays approved:

```yaml
# apm.yml  (committed)
executables:
  allow:
    acme/agent-kit:
      hooks:
        .apm/hooks/lint-on-save.json: sha256:ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb
      mcp:
        kit-docs: sha256:3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d
    acme/tools:
      bin: true
    acme/superpowers:
      plugin: sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08
      hooks:
        hooks/hooks.json: sha256:2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae
  deny:
    "evil/pkg":
      hooks: true
      mcp: true
      bin: true
      canvas: true
      lsp: true
```

An item set to `true`, or a short or malformed digest, fails with
`apm.yml executables.allow.<pkg>.<type>.<item>: expected 'sha256:<64 hex>'.
Run 'apm approve <pkg>' to record it.` A pinned map under `deny` denies the
whole type. Once `apm.yml` holds pins, every client needs APM 0.37 or later;
older clients reject the item form and fail closed.

The legacy top-level `allowExecutables:` block is **deprecated**. It is still
read as an alias for `executables.allow` for one minor cycle and is migrated to
`executables.allow` on the next `apm approve` / `apm deny` write. Prefer
`executables.allow`.

The personal store uses the same shape under `executables` in
`$APM_HOME/config.json` (default `~/.apm/config.json`). The standalone
`$APM_HOME/approvals.yml` file has been **removed**; its contents are migrated
into `$APM_HOME/config.json` automatically on first read. `apm audit --ci`
ignores the personal store when it checks MCP consent, so CI and laptops agree.

Existing `owner/repo#1.2.0` keys still match the package regardless of the
installed version; `apm approve` folds them into the version-blind key. Local
bundle grants are different: copy the full `name#version@sha256:<digest>` key
from the install diagnostic. A changed local bundle does not inherit the prior
grant.

## Examples

```bash
apm approve owner/repo          # pin and record in apm.yml (committed)
apm approve --user owner/repo   # pin for this machine only
apm approve --pending           # review unapproved or changed items
apm approve --all               # after review, approve everything pending
apm approve --recommended       # accept the org-recommended set
apm approve --list              # effective trust state per installed package
```

To block a package instead, see [`apm deny`](../deny/).

## Non-interactive / CI usage

In CI environments (`CI=true`, `APM_NON_INTERACTIVE=1`, or when stdin is not a
TTY), `apm install` parks unapproved executables and prints the approval
remedy instead of prompting. On a TTY, the install prompt records pinned
approvals in the personal store. To share trust, run `apm approve <pkg>` and
commit `apm.yml`. Required-but-untrusted executables are enforced by
`apm audit` through the `required-executable-untrusted` signal.

If CI relied on `--trust-transitive-mcp`, run `apm approve <pkg>` and commit
`apm.yml`: the flag is not saved, and `apm audit --ci` fails a server the
lockfile admitted without approval in `apm.yml` (see
[`config-consistency`](../../baseline-checks/#config-consistency)).

## See also

- [`apm deny`](../deny/) -- block executable primitives for packages
- [`apm install`](../install/) -- the install command that enforces the gate
- [`apm audit`](../audit/) -- audit installed packages
- [apm-policy.yml schema](../../policy-schema/) -- the org `executables:` ceiling
