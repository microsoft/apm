---
title: apm-policy.yml schema
description: Canonical field reference for apm-policy.yml -- the file org admins publish to govern apm install, apm audit --ci, and related commands.
sidebar:
  order: 5
---

> **Normative reference:** this page documents the v0.2 working-draft policy schema as enforced by the current CLI. The normative, ratified contract for v0.1 is defined in [OpenAPM v0.1, Section 6 (Policy)](/apm/specs/openapm-v01/) and published as JSON Schema at [`policy-v0.1.schema.json`](/apm/specs/schemas/policy-v0.1.schema.json).

The `apm-policy.yml` schema. One file per org or repo. Loaded by `apm install`, `apm audit --ci`, `apm policy status`, and the install preflight before any package is written to disk.

For the workflow (where to put the file, how to roll it out), see [Govern with apm-policy.yml](../enterprise/apm-policy-getting-started/). For CLI usage of `apm policy status`, see [apm policy](./cli/policy/). For the wider governance picture (rulesets, registry proxy, CI gating), see [Governance overview](../enterprise/governance-overview/).

## What apm-policy.yml governs

`apm-policy.yml` is the **install policy** for agent primitives. In plain terms it decides:

- **Which sources are trusted** -- which dependencies and MCP servers `apm install` will accept (the `allow` / `deny` / `registry_source` rules).
- **Which versions install** -- whether refs must be pinned to bounded constraints, and how version conflicts on required packages resolve.
- **What the lockfile records and verifies** -- which artifacts the lockfile records, and what the install-time audit pass checks over them once written.

`apm install` enforces the dependency and MCP source rules during install; `apm audit --ci` evaluates every rule on this page. Together they govern what gets installed -- not what a running agent is later allowed to do.

## What apm-policy.yml does NOT govern

`apm-policy.yml` is not a runtime permission system. It does not control:

- **Runtime permissions** -- what an agent may read, write, or call once it is running.
- **Sandboxing** -- process isolation or resource limits for a running agent.
- **Agent behavior** -- what a running agent actually does with the primitives it was given.
- **Marketplace enablement** -- which tools or extensions a harness exposes to the agent.

Those controls are owned by the **agent harness** that runs your agents, not by APM. APM stops at install: it decides what reaches disk, and the harness decides what runs.

## File location

Discovery order, in priority:

1. `--policy <ref>` flag on `apm install` or `apm audit`.
2. Auto-discovery from the project's git remote -- fetches `<owner>/.github/apm-policy.yml` via the GitHub Contents API.

The `<ref>` accepts:

- A local file path.
- An `https://` URL (plain `http://` is rejected).
- The literal string `org` (force git-remote auto-discovery).
- `<owner>/<repo>` shorthand (defaults to `github.com`).
- `<host>/<owner>/<repo>` for GHES or other hosts.

## Top-level fields

| Field              | Type                | Default          | Required | Notes                                                                             |
|--------------------|---------------------|------------------|----------|-----------------------------------------------------------------------------------|
| `name`             | string              | `""`             | no       | Human label. Surfaces in `apm policy status`.                                     |
| `version`          | string              | `""`             | no       | Free-form. Useful for change tracking.                                            |
| `extends`          | string or null      | `null`           | no       | Parent policy reference. See [Inheritance](#inheritance).                         |
| `enforcement`      | enum                | `warn`           | no       | `off` / `warn` / `block`. See [Enforcement modes](#enforcement-modes).            |
| `fetch_failure`    | enum                | `warn`           | no       | `warn` / `block`. Behavior when a remote policy cannot be fetched or parsed.      |
| `cache`            | object              | `{ttl: 3600}`    | no       | Cache settings for remote policy fetches.                                         |
| `dependencies`     | object              | see section      | no       | Rules over APM dependencies.                                                       |
| `mcp`              | object              | see section      | no       | Rules over MCP servers (direct and transitive).                                   |
| `compilation`      | object              | see section      | no       | Rules over `apm compile` outputs.                                                 |
| `manifest`         | object              | see section      | no       | Rules over `apm.yml` content.                                                     |
| `unmanaged_files`  | object              | see section      | no       | Rules over files in target directories not tracked by the lockfile.               |
| `security`         | object              | see section      | no       | Rules over APM's security checks (install-time content audit + external scanners; requires `external-scanners` flag). |
| `registry_source`  | object              | see section      | no       | Mandate registry usage and block non-registry sources (requires `registries` flag). |
| `bin_deploy`       | object              | see section      | no       | Control whether `marketplace_plugin` bin/ executables are deployed to `~/.claude/skills/<name>/bin/`. |

Unknown top-level keys produce a warning, never an error -- so newer policy files load on older clients.

## Enforcement modes

`enforcement` is the global outcome dial. Per-rule severities feed into this dial:

| Mode    | Behavior                                                                                       |
|---------|------------------------------------------------------------------------------------------------|
| `off`   | Policy is loaded and reported, but never blocks. Useful during rollout.                        |
| `warn`  | Violations print `[!]` warnings but exit code stays 0. Default.                                |
| `block` | Violations print `[x]` errors and exit non-zero. `apm install` aborts before writing to disk. |

`fetch_failure` controls fail-closed behavior on the policy fetch itself (network down, malformed YAML, host mismatch). Set to `block` in production CI to refuse to run when policy cannot be loaded.

## cache

| Field | Type    | Default | Notes                                                                  |
|-------|---------|---------|------------------------------------------------------------------------|
| `ttl` | integer | `3600`  | Seconds. Must be `> 0`. Applies to remote `extends:` and URL fetches. |

## dependencies

Rules over the `dependencies:` and `mcp:` blocks declared in consumer `apm.yml` files.

When a dependency matches `deny`, `apm install` will **not download or deploy that artifact**. With `enforcement: block`, the install aborts before the denied package is downloaded or deployed. `deny` is an install-time decision, not a runtime block: it stops a denied artifact from being installed, and does not constrain what an already-installed or otherwise-present artifact does at runtime.

| Field                | Type                  | Default          | Notes                                                                                       |
|----------------------|-----------------------|------------------|---------------------------------------------------------------------------------------------|
| `allow`              | list of patterns or null | `null`        | `null` = no opinion. `[]` = nothing allowed. `[...]` = only these.                          |
| `deny`               | list of patterns or null | `null`        | `null` = no opinion (transparent during merge). `[]` = explicitly empty. Always wins over `allow`. |
| `require`            | list of refs or null  | `null`           | `null` = no opinion (transparent during merge). `[]` = explicitly empty. Packages every consumer manifest must include. |
| `require_resolution` | enum                  | `project-wins`   | `project-wins` / `policy-wins` / `block` -- how to resolve version conflicts on required packages. |
| `max_depth`          | integer               | `50`             | Maximum transitive dependency depth. Must be `> 0`.                                         |
| `require_pinned_constraint` | boolean        | `false`          | When `true`, every APM dep declared in `apm.yml` must use a bounded constraint (exact, `^`/`~`/bounded range, literal tag, or SHA). Transitive deps are also classified and pass when their parent manifests pinned them. Unbounded refs (missing ref, `*`, bare branch, bare `>=X.Y`) are routed through `policy.enforcement` (`warn` / `block`). **Enabling on existing projects will likely surface violations; roll out with `enforcement: warn` first.** |

### `require_pinned_constraint` reference

Examples (with `require_pinned_constraint: true` and `enforcement: block`):

```yaml
# apm.yml
dependencies:
  apm:
    - acme/skills              # FAIL: no ref (NO_REF)
    - other/lib#>=1.0.0        # FAIL: unbounded upper (OPEN_UPPER)
    - third/lib#*              # FAIL: wildcard (WILDCARD)
    - acme/lib#main            # FAIL: bare branch (BARE_BRANCH)
    - fourth/lib#^1.2.0        # OK: caret range
    - fifth/lib#~1.2.3         # OK: tilde range
    - sixth/lib#1.5.3          # OK: exact version (bare)
    - sixth_eq/lib#=1.5.3      # OK: exact version (npm/cargo explicit equality)
    - sixth_pip/lib#==1.5.3    # FAIL: pip-style operator not supported (BARE_BRANCH)
    - seventh/lib#v1.5.3       # OK: literal tag
    - eighth/lib#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  # OK: SHA
    - ./packages/local         # OK: local-path dep (no version surface)
```

Diagnostic shape (ASCII-only, ``[x]`` for block, ``[!]`` for warn):

```text
[x] Policy violation: dependency-pinned-constraint
    4 dependency(ies) use unbounded constraints
    (hint: pin to a semver range, literal tag, or SHA)
    - acme/skills: no ref; resolves to default branch
    - other/lib: unbounded upper; pair with '<X.Y' or use a caret range
    - third/lib: wildcard '*' matches any version
    - acme/lib: bare branch 'main' tracks a moving tip
```

Patterns are matched against `<owner>/<repo>` (or `<host>/<owner>/<repo>`). Wildcards via shell-style globs, e.g. `contoso/*`.

## mcp

Rules over MCP server references, including transitive ones declared by deep dependencies.

| Field              | Type                  | Default | Notes                                                                                |
|--------------------|-----------------------|---------|--------------------------------------------------------------------------------------|
| `allow`            | list or null          | `null`  | Same null/empty/list semantics as `dependencies.allow`.                              |
| `deny`             | list                  | `[]`    | Always wins.                                                                         |
| `transport.allow`  | list or null          | `null`  | Allowed transports: `stdio`, `sse`, `http`, `streamable-http`. `null` = no opinion. |
| `self_defined`     | enum                  | `warn`  | `allow` / `warn` / `deny` -- treatment of MCP servers defined inline in `apm.yml`.   |
| `trust_transitive` | boolean               | `false` | When `false`, transitive MCP servers must be explicitly allow-listed.                |

## compilation

| Field                  | Type                  | Default | Notes                                                                          |
|------------------------|-----------------------|---------|--------------------------------------------------------------------------------|
| `target.allow`         | list or null          | `null`  | Allowed `apm compile` targets, e.g. `vscode`, `claude`, `all`.                |
| `target.enforce`       | string or null        | `null`  | If set, `apm compile` must select this target.                                 |
| `strategy.enforce`     | enum or null          | `null`  | `distributed` / `single-file`. Pins the compilation strategy.                  |
| `source_attribution`   | boolean               | `false` | Require source-attribution headers in compiled outputs.                        |

## manifest

| Field                       | Type                    | Default | Notes                                                                  |
|-----------------------------|-------------------------|---------|------------------------------------------------------------------------|
| `required_fields`           | list of strings         | `[]`    | Top-level keys every `apm.yml` must declare (e.g. `description`, `license`). |
| `scripts`                   | enum                    | `allow` | `allow` / `deny` -- whether `scripts:` blocks are permitted.            |
| `content_types`             | object or null          | `null`  | `{allow: [...]}`. Restricts primitive content types (e.g. `skill`, `prompt`). |
| `require_explicit_includes` | boolean                 | `false` | When `true`, manifests must declare an explicit `includes:` list rather than rely on directory globs. |

## unmanaged_files

Files in primitive target directories that are not recorded in `apm.lock.yaml`.

| Field         | Type           | Default  | Notes                                                                            |
|---------------|----------------|----------|----------------------------------------------------------------------------------|
| `action`      | enum           | `ignore` | `ignore` / `warn` / `deny`. `deny` blocks installs that would leave drift.      |
| `directories` | list of paths  | `[]`     | Subset of target directories to check. Empty = all known target directories.     |
| `exclude`     | list of globs  | `null`   | Workspace path globs to suppress from the report. Use to silence known harness-managed artifacts. Excluded paths are never reported. `null` = no opinion (transparent in the `extends:` merge); merges as a union down the chain. |

Each reported file is a divergence-visibility finding, not a security verdict
-- `apm.lock.yaml` is hand-editable YAML, so this surfaces drift rather than
proving a supply-chain attack. Every finding is enriched in place:

- a factual reason -- `not tracked in apm.lock.yaml`;
- a lazy primitive-type tag (`[type: skill|agent|instruction|mcp]`) classified
  only for the already-flagged file, never the whole tree;
- a deny-conflict note -- `matches deny rule (<pattern>)` -- when the path
  matches this policy's own `dependencies.deny` or `mcp.deny`. This is surfaced
  for a human to resolve; APM never removes or blocks the file on this basis.

```text
[!] .github/agents/rogue.agent.md [type: agent] -- not tracked in apm.lock.yaml; matches deny rule (**/rogue*)
```

## security

Rules over APM's security checks. Requires the `external-scanners`
experimental flag to take effect; ignored otherwise.

| Field                | Type            | Default | Notes                                                                            |
|----------------------|-----------------|---------|----------------------------------------------------------------------------------|
| `audit.on_install`   | enum or null    | `null`  | `off` / `warn` / `block`. Minimum install-time audit mode (a **floor**). `null` = no opinion. `warn` records findings; `block` halts installs on critical findings. |
| `audit.external`     | list of strings | `null`  | External SARIF scanner names (e.g. `skillspector`) that MUST run during the install audit. A required scanner that is unavailable fails the install closed. |
| `audit.scanners`     | mapping or null | `null`  | Per-scanner governance, keyed by scanner name. Each value accepts `allow_args` (boolean). **Restrict-only**: see below. Unknown scanner names are a warning, not an error (forward-compat). |

### Per-scanner governance (`audit.scanners`)

```yaml
security:
  audit:
    scanners:
      skillspector:
        allow_args: false      # forbid extra-args passthrough at install time
```

`audit.scanners` is **restrict-only**. A policy may tighten a scanner's
behaviour but can never expand it:

- `allow_args: false` strips any user/CLI `external.<name>.args` (and
  `--external-args`) for that scanner at install time, locking it to a vetted
  invocation. `true` / omitted leaves the user's args in place (still
  allowlist-validated by the adapter).
- Policy **never contributes argv tokens** itself and **never forces LLM mode
  on**. LLM opt-in stays a local user decision (`--external-llm` or
  `external.<name>.llm`). This removes any project-policy argv-injection or
  egress-coercion surface.

The floor is enforced during `apm install` (where org policy is loaded). A bare
`apm audit --external` run does not load org policy and relies on the adapter's
allowlist validation for arg safety.

`audit.on_install` is a floor: it can only raise the effective mode chosen by
`apm install --audit` / `apm config audit-on-install`, never relax it. `apm
install --force` downgrades a `block` to `warn` for that invocation; `apm
install --no-policy` skips the policy floor entirely.

## Inheritance

`extends:` resolves a parent policy. Maximum chain depth is **5**; cycles are rejected.

`extends:` accepts:

- `org` -- the same org's `.github/apm-policy.yml`.
- `<owner>/<repo>` -- another repo on the same host.
- `https://...` -- a direct URL.

For supply-chain safety, `extends:` references are pinned to the **leaf policy's host** -- a policy fetched from `github.com` cannot extend one on `evil.example.com`.

### Merge rules

Most fields tighten as the policy chain descends. The exceptions are
`deny`/`require` lists, where a child may use `[]` to explicitly clear an
inherited list (see the tri-state table below).

| Field family                | Merge rule                                                                       |
|-----------------------------|----------------------------------------------------------------------------------|
| `enforcement`               | Stricter wins (`block` > `warn` > `off`).                                        |
| `fetch_failure`             | Child overrides if set.                                                          |
| `cache.ttl`                 | `min(parent, child)`.                                                            |
| `*.allow` lists             | Set intersection. `null` is transparent (no opinion).                            |
| `*.deny` / `require` lists  | Union, deduplicated, parent order preserved. Omitting the field (or setting it to `null`) is transparent  --  the parent value passes through unchanged. `[]` is an explicit empty override. |
| `dependencies.max_depth`    | `min(parent, child)`.                                                            |
| `dependencies.require_resolution` | Stricter wins (`block` > `policy-wins` > `project-wins`).                  |
| `dependencies.require_pinned_constraint` | Logical OR -- once a parent enables it, child cannot relax.            |
| `mcp.self_defined`          | Stricter wins (`deny` > `warn` > `allow`).                                       |
| `mcp.trust_transitive`      | Logical AND (`true` only if both sides true).                                    |
| `manifest.scripts`          | Stricter wins (`deny` > `allow`).                                                |
| `unmanaged_files.action`    | Stricter wins (`deny` > `warn` > `ignore`).                                      |
| `unmanaged_files.exclude`   | Union, deduplicated; additive-only. `null` and `[]` both preserve the parent list  --  unlike `deny`/`require`, a child cannot clear an inherited `exclude`. |
| `security.audit.on_install` | Stricter wins (`block` > `warn` > `off`). `null` is transparent.                 |
| `security.audit.external`   | Union, deduplicated. `null` is transparent.                                      |
| `security.audit.scanners`   | Union of scanner names; per scanner `allow_args` is AND-merged (any ancestor `false` wins -- tightening). `null` is transparent.                  |
| `compilation.*.enforce`     | First non-null wins (parent precedence).                                         |
| `compilation.source_attribution` | Logical OR.                                                                 |

A merged chain is reported by `apm policy status` with each layer's source attributed.

## Allow-list semantics

For every `allow:` field, the three states are distinct:

| Value    | Meaning                                      | Inheritance behavior            |
|----------|----------------------------------------------|---------------------------------|
| omitted  | "no opinion"                                 | Transparent during merge.       |
| `[]`     | "explicitly nothing"                         | Intersects to nothing downstream. |
| `[...]`  | "only these patterns"                        | Intersected with child list.    |

`deny` and `require` lists support the same three-state semantics as `allow`:

| Value    | Meaning                    | Inheritance behavior                                     |
|----------|----------------------------|----------------------------------------------------------|
| omitted / `null` | "no opinion"     | Transparent during merge  --  parent value passes through. |
| `[]`     | "explicitly empty"         | Overrides parent; no entries accumulate.                |
| `[...]`  | "these entries"            | Unioned with parent list (parent order preserved).      |

## Complete example

```yaml
# .github/apm-policy.yml -- shipped from contoso/.github
name: contoso-baseline
version: "2025.05"
extends: contoso-enterprise/policy

enforcement: block
fetch_failure: block

cache:
  ttl: 1800

dependencies:
  allow:
    - contoso/*
    - microsoft/apm-skills-*
  deny:
    - "*/legacy-*"
  require:
    - contoso/security-baseline
  require_resolution: policy-wins
  max_depth: 25

mcp:
  allow:
    - github/github-mcp-server
    - contoso/internal-mcp-*
  transport:
    allow:
      - stdio
      - streamable-http
  self_defined: deny
  trust_transitive: false

compilation:
  target:
    allow:
      - vscode
      - claude
  strategy:
    enforce: distributed
  source_attribution: true

manifest:
  required_fields:
    - description
    - license
  scripts: deny
  content_types:
    allow:
      - skill
      - prompt
      - instruction
  require_explicit_includes: true

unmanaged_files:
  action: deny
  directories:
    - .github/instructions
    - .github/prompts
  exclude:
    - .github/copilot-instructions.md
```

## registry_source

::::caution[Experimental]
Requires `apm experimental enable registries`. Ignored on clients without the flag enabled.
::::

Mandate that APM dependencies come from configured registries. Checks apply transitively (including transitive deps pulled in by registry packages).

| Field | Type | Default | Description |
|---|---|---|---|
| `require` | `list<string>` | `[]` | Registry names that MUST be reachable. APM **fails-closed** if a listed name has no URL in the merged registry map (from project `apm.yml`, workspace `~/.apm/apm.yml`, or `~/.apm/config.json`) — this is intentional: a missing registry config is treated as a policy violation, not a no-op. |
| `allow_non_registry` | `bool` | `true` | When `false`, APM blocks installation of any dependency not routed through a configured registry. |

```yaml
registry_source:
  require:
    - jf-skills
  allow_non_registry: false
```

## bin_deploy

Controls whether `apm install -g` deploys `bin/` executables from `marketplace_plugin` packages into `~/.claude/skills/<name>/bin/`, alongside the package's `.claude-plugin/plugin.json`. Here `<name>` is the package's install directory name (typically the repository name).

This realizes Claude Code's "skills-directory plugin" contract: a folder under a skills directory that contains `.claude-plugin/plugin.json` loads as `<name>@skills-dir`, and its root `bin/` is added to the Bash tool's `PATH`. The package's `.claude-plugin/plugin.json` is required for Claude to load the folder as a plugin; APM copies it alongside `bin/` when the package ships one. The contract is Claude-specific, so deployment only targets Claude. Restart Claude Code (or run `/reload-plugins`) after install for new executables to be picked up.

**Security note:** deployed executables are made executable (user-only execute bit; group and other execute bits are cleared) and placed on Claude Code's `PATH`, so Claude can invoke them without further confirmation. By default, APM mirrors npm's trust model: installing a package implies trusting its declared artifacts, including executables. Use this field to opt out globally or per-package in enterprise environments.

**Scope:** bin/ deployment only activates for global (`-g`, user-scope) installs. Project-scope installs do not deploy executables.

**Authoring plugins that ship `bin/`:** see [Repo shapes for marketplace producers](../producer/repo-shapes/#shipping-bin-executables-claude-code-only) for the producer-side contract (directory layout, executable bit, scope and trust posture).

| Field | Type | Default | Description |
|---|---|---|---|
| `deny_all` | `bool` | `false` | When `true`, suppresses bin/ deployment for every `marketplace_plugin` package, regardless of individual `deny` entries. |
| `deny` | `list<string>` | `[]` | Package canonical strings whose bin/ executables must not be deployed. Entries are matched case-sensitively; copy each string verbatim from `apm deps list` (e.g. `myorg/myplugin`). |

```yaml
bin_deploy:
  # Block all bin/ deploys organisation-wide:
  deny_all: true
```

```yaml
bin_deploy:
  # Allow bin/ deploys except for one specific package:
  deny:
    - myorg/untrusted-plugin
```

## FAQ

**Does my harness's managed configuration replace APM?**

No. apm-policy.yml controls what gets installed; your harness controls what runs; they do not overlap.

## See also

- [apm policy](./cli/policy/) -- the `apm policy status` command.
- [Govern with apm-policy.yml](../enterprise/apm-policy-getting-started/) -- end-to-end rollout guide.
- [Enforce in CI](../enterprise/enforce-in-ci/) -- wiring `apm audit --ci` into branch protection.
- [Governance overview](../enterprise/governance-overview/) -- the full enterprise control surface.
