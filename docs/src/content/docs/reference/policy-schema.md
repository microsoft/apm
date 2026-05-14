---
title: apm-policy.yml schema
description: Canonical field reference for apm-policy.yml -- the file org admins publish to govern apm install, apm audit --ci, and related commands.
sidebar:
  order: 5
---

The `apm-policy.yml` schema. One file per org or repo. Loaded by `apm install`, `apm audit --ci`, `apm policy status`, and the install preflight before any package is written to disk.

For the workflow (where to put the file, how to roll it out), see [Govern with apm-policy.yml](../../enterprise/apm-policy-getting-started/). For CLI usage of `apm policy status`, see [apm policy](../cli/policy/). For the wider governance picture (rulesets, registry proxy, CI gating), see [Governance overview](../../enterprise/governance-overview/).

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
| `dependencies`    | object              | see section      | no       | Rules over APM dependencies.                                                       |
| `mcp`              | object              | see section      | no       | Rules over MCP servers (direct and transitive).                                   |
| `compilation`      | object              | see section      | no       | Rules over `apm compile` outputs.                                                 |
| `manifest`         | object              | see section      | no       | Rules over `apm.yml` content.                                                     |
| `unmanaged_files`  | object              | see section      | no       | Rules over files in target directories not tracked by the lockfile.               |

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

| Field                | Type                  | Default          | Notes                                                                                       |
|----------------------|-----------------------|------------------|---------------------------------------------------------------------------------------------|
| `allow`              | list of patterns or null | `null`        | `null` = no opinion. `[]` = nothing allowed. `[...]` = only these.                          |
| `deny`               | list of patterns or null | `null`        | `null` = no opinion (transparent during merge). `[]` = explicitly empty. Always wins over `allow`. |
| `require`            | list of refs or null  | `null`           | `null` = no opinion (transparent during merge). `[]` = explicitly empty. Packages every consumer manifest must include. |
| `require_resolution` | enum                  | `project-wins`   | `project-wins` / `policy-wins` / `block` -- how to resolve version conflicts on required packages. |
| `max_depth`          | integer               | `50`             | Maximum transitive dependency depth. Must be `> 0`.                                         |

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
| `mcp.self_defined`          | Stricter wins (`deny` > `warn` > `allow`).                                       |
| `mcp.trust_transitive`      | Logical AND (`true` only if both sides true).                                    |
| `manifest.scripts`          | Stricter wins (`deny` > `allow`).                                                |
| `unmanaged_files.action`    | Stricter wins (`deny` > `warn` > `ignore`).                                      |
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
```

## See also

- [apm policy](../cli/policy/) -- the `apm policy status` command.
- [Govern with apm-policy.yml](../../enterprise/apm-policy-getting-started/) -- end-to-end rollout guide.
- [Enforce in CI](../../enterprise/enforce-in-ci/) -- wiring `apm audit --ci` into branch protection.
- [Governance overview](../../enterprise/governance-overview/) -- the full enterprise control surface.
