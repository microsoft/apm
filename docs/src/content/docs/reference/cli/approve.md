---
title: apm approve / apm deny
description: Manage the executable approval gate for dependency packages.
sidebar:
  order: 25
---

## Synopsis

```bash
apm approve [PACKAGE_REF...] [OPTIONS]
apm deny [PACKAGE_REF...] [OPTIONS]
apm policy explain <PACKAGE_REF>
```

## Description

APM blocks executable primitives (hooks, `bin/` executables, self-defined MCP
servers, and canvas extensions) from dependency packages by default. Trust is
expressed through one noun, `executables`, across three layers:

| Layer | Store | Who manages it | Committed? | Authority |
|-------|-------|----------------|------------|-----------|
| Project | `apm.yml` `executables.{allow,deny}` | Maintainer / CI setup (`apm approve`/`apm deny`) | Yes | Admin (shared) |
| User | `~/.apm/config.json` `executables.{allow,deny}` | `apm approve --user` / `apm deny --user` | No | Lowest; can only narrow |
| Org | `apm-policy.yml` `executables:` | Org admin | Yes (policy repo) | Ceiling on deny |

`apm approve` adds a grant; `apm deny` adds a block. By default both write the
**project** `apm.yml` (committed, so the whole team inherits the decision).
`--user` writes your personal `~/.apm/config.json` instead -- a machine-local
override that can only narrow trust, never widen past an org or project deny.

Text primitives (skills, agents, instructions) are never gated. Local project
content (the root `.apm/` directory) is always trusted.

### What is gated

| Type | Gated | Notes |
|------|-------|-------|
| Hooks (`.apm/hooks/`, `hooks/`) | Yes | Auto-fire in IDE on lifecycle events |
| Bin executables (`bin/`) | Yes | Deployed to agent PATH via symlinks |
| MCP servers (self-defined) | Yes | `registry: false` servers write to IDE MCP config |
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

There is no `enforce` mandate runtime, no cryptographic signing, and no
content-hash binding in this release. An org `executables.enforce` rung
degrades to `recommend` (allowed but still overridable by a deny).

### The gate opt-in

The gate is enabled when any layer opts in: the project declares an
`executables:` block (even empty `{}`), or the org policy carries a non-empty
`executables:` block. Without any opt-in, executables deploy unconditionally
(backward-compatible).

## Options

### `apm approve`

| Flag | Description |
|------|-------------|
| `PACKAGE_REF` | One or more packages to approve (e.g. `owner/repo`). |
| `--pending` | List all packages with unapproved executables. |
| `--all` | Approve all currently blocked packages. |
| `--recommended` | Bulk-accept the org `executables.recommend` set. |
| `--list` | Show the fleet-level effective trust decision and deciding layer per installed package. |
| `--user` | Write the grant to `~/.apm/config.json` instead of `apm.yml`. |

### `apm deny`

| Flag | Description |
|------|-------------|
| `PACKAGE_REF` | One or more packages to deny. Denying a not-yet-installed package is allowed (a pre-emptive block). |
| `--user` | Write the block to `~/.apm/config.json` instead of `apm.yml`. |

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

The project `executables.allow` / `executables.deny` maps are keyed by
`owner/repo#version` (or version-blind `owner/repo`) with per-type boolean
flags:

```yaml
# apm.yml  (committed)
executables:
  allow:
    "owner/repo#1.2.0":
      hooks: true
      bin: true
  deny:
    "evil/pkg":
      hooks: true
      mcp: true
      bin: true
      canvas: true
```

The legacy top-level `allowExecutables:` block is **deprecated**. It is still
read as an alias for `executables.allow` for one minor cycle and is migrated to
`executables.allow` on the next `apm approve` / `apm deny` write. Prefer
`executables.allow`.

The personal store uses the same shape under `executables` in
`~/.apm/config.json`. The standalone `~/.apm/approvals.yml` file has been
**removed**; its contents are migrated into `~/.apm/config.json` automatically
on first read.

Grant keys are package-scoped in v1: a bare `owner/repo` key and a
`owner/repo#1.2.0` key both match the package name regardless of the installed
version. Use the versioned form for audit readability, not as a per-release
trust boundary.

## Examples

Approve a specific package (writes committed project trust):

```bash
apm approve owner/repo
```

Approve for this machine only:

```bash
apm approve --user owner/repo
```

List packages awaiting approval:

```bash
apm approve --pending
```

After review, approve everything still pending:

```bash
apm approve --all
```

Accept the org-recommended set:

```bash
apm approve --recommended
```

Inspect effective trust state across installed packages:

```bash
apm approve --list
```

Block a package (deny always wins):

```bash
apm deny evil/pkg
```

## Non-interactive / CI usage

In CI environments (`CI=true`, `APM_NON_INTERACTIVE=1`, or when stdin is not a
TTY), `apm install` parks unapproved executables and prints the approval
remedy instead of prompting. Pre-approve packages by committing them to the
project `executables.allow` block (the way to share trust via source control).
Required-but-untrusted executables are enforced by `apm audit` through the
`required-executable-untrusted` signal:

```yaml
# apm.yml
executables:
  allow:
    "ci-hooks/acme#1.2.0":
      hooks: true
      bin: true
```

## See also

- [`apm install`](../install/) -- the install command that enforces the gate
- [`apm audit`](../audit/) -- audit installed packages
- [apm-policy.yml schema](../../policy-schema/) -- the org `executables:` ceiling
