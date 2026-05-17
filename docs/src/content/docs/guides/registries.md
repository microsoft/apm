---
title: "Registries"
description: "Declare REST-based APM registries in apm.yml and consume packages from them alongside Git-hosted dependencies."
sidebar:
  order: 6
---

A **registry** is a REST-based source for APM packages. Any service that implements the [Registry HTTP API](../../reference/registry-http-api/) qualifies. Registries sit alongside the existing Git resolver: declare a `registries:` block in `apm.yml` and individual dependencies route to the registry by name. Registries are strictly additive. A project without a `registries:` block sees zero behavior change — every existing dependency form continues to resolve through Git exactly as before.

::::caution[Experimental]
Package registries are currently behind an experimental flag. Enable them before adding `registries:` or registry-sourced dependencies:

```bash
apm experimental enable registries
```
::::

## Declare a registry

Add a top-level `registries:` block to `apm.yml`:

```yaml
registries:
  jf-skills:
    url: https://registry.example.com/apm/jf-skills
  default: jf-skills
```

Each entry is a name mapped to a base URL. The optional `default:` key names one of the configured entries; when set, plain string-shorthand APM dependencies route through it (see [Default routing](#default-routing) below). Registry URLs MUST start with `https://` (or `http://` for local development).

The registry name is used for env-var auth lookup. Use lowercase letters, digits, `-`, and `.`.

## Reference a registry-sourced dependency

There are two ways to point a dependency at a registry.

### 1. String shorthand routed through the default

When `registries.default` is set, plain `owner/repo` shorthand entries route through that registry — the same syntax already used for GitHub dependencies, but now resolved over HTTP:

```yaml
registries:
  jf-skills:
    url: https://registry.example.com/apm/jf-skills
  default: jf-skills

dependencies:
  apm:
    - acme/foo#^1.2.3        # resolved via jf-skills
    - acme/bar#main          # exact selector resolved via jf-skills
```

Routing is unconditional: every still-unrouted shorthand entry with a `#<ref>` is sent through the default registry. Object-form entries (`- git:`, `- path:`, `- id:`) are left alone.

### 2. Object form (whole package or virtual packages)

For explicit per-dep registry routing — or for **virtual packages** (a single file or sub-path inside a published package) — use the object form:

```yaml
dependencies:
  apm:
    # Whole package via the default registry (registry: omitted)
    - id: acme/toolkit
      version: ^2.0.0

    # Whole package routed to a specific registry
    - registry: jf-skills
      id: acme/toolkit
      version: ^2.0.0

    # Virtual package (sub-path inside a published package)
    - registry: jf-skills
      id: acme/prompt-pack
      path: prompts/review.prompt.md
      version: 1.4.0
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Package identity at the registry, in `owner/repo` form. |
| `version` | yes | Exact version/ref selector or semver range. |
| `registry` | no | Name from the `registries:` block. Defaults to `registries.default` when omitted. |
| `path` | no | Virtual sub-path inside the published package. Omit to install the whole package. |
| `alias` | no | Local alias (controls install directory name). |

## Version selectors

Registry-routed entries MUST specify a semver version selector. Non-semver
refs such as branch names or commit SHAs always stay on the Git resolver —
they are never forwarded to a registry:

| Selector | Behavior |
|---|---|
| `1.0.0`, `1.4.2` | Exact semver version |
| `^1.0.0`, `~1.2.3`, `>=1.2.0 <2.0.0` | Semver range; APM picks the highest matching registry version |
| unset (no `#<ref>`) | Rejected for registry-routed dependencies |

Registry ranges use the same full-version semver grammar as marketplace builds:
write all three version components (`major.minor.patch`) and combine multiple
constraints with spaces, for example `>=1.2.0 <2.0.0`.

```yaml
dependencies:
  apm:
    - acme/foo#^1.2.3                        # registry, semver range
    - acme/bar#main                          # git resolver (non-semver → stays on git)
    - git: https://github.com/acme/baz.git   # Git resolver
      ref: main
```

Registry-routed deps are byte-for-byte reproducible via `resolved_hash`;
Git-routed deps are SHA-reproducible via `resolved_commit`. Choose Git object
form when the source of truth is still a Git repository rather than the
registry's published version list.

## Default routing

When `registries.default` is set, the routing rules are:

| Entry form | Routed to |
|---|---|
| `owner/repo#<semver-range-or-version>` | Default registry |
| `- id:` object form (no `registry:`) | Default registry |
| `- id:` object form (with `registry:`) | Named registry |
| `- git:` object form | Git (unchanged) |
| `- path:` object form | Local filesystem (unchanged) |
| Virtual shorthand (`owner/repo/sub/path`) | Git (unchanged) — virtuals MUST use object form to route through a registry |

A shorthand entry without a selector (`acme/foo`) is rejected when `default:`
is set — registry-routed entries always require a `#<semver-version-or-range>`.
Non-semver refs (`#main`, `#v1.0.0`, commit SHAs) are not forwarded to the
registry; they resolve through Git as normal.

## Authentication

APM reads credentials from environment variables named after the registry. `{NAME}` is the registry name uppercased, with `-` and `.` mapped to `_`.

| Env var | Auth method |
|---|---|
| `APM_REGISTRY_TOKEN_{NAME}` | `Authorization: Bearer <token>` |
| `APM_REGISTRY_USER_{NAME}` + `APM_REGISTRY_PASS_{NAME}` | `Authorization: Basic <base64(user:pass)>` |

Bearer wins when both forms are set. When neither is set, APM tries the request anonymously and surfaces a remediation pointing at `APM_REGISTRY_TOKEN_<NAME>` on `401`/`403`.

```bash
# Registry name "jf-skills" -> APM_REGISTRY_TOKEN_JF_SKILLS
export APM_REGISTRY_TOKEN_JF_SKILLS=eyJ...

# Or HTTP Basic for enterprise registries that issue username/password
export APM_REGISTRY_USER_JF_SKILLS=alice@example.com
export APM_REGISTRY_PASS_JF_SKILLS=...
```

The `APM_REGISTRY_*` prefix is distinct from `GITHUB_APM_PAT_*`, `PROXY_REGISTRY_*`, and `ARTIFACTORY_APM_TOKEN` — there is no collision. For the broader auth model, see [Authentication](../../getting-started/authentication/).

## What gets recorded in the lockfile

Registry-sourced dependencies add four fields to their lockfile entry: `source: registry`, `version`, `resolved_url`, and `resolved_hash` (sha256 of the archive bytes). The lockfile bumps to `lockfile_version: "2"` opportunistically — only when at least one registry dep is present. Projects that never opt into a registry keep `lockfile_version: "1"` forever, even on a newer client.

```yaml
dependencies:
  - repo_url: acme/foo
    source: registry
    version: "1.4.0"
    resolved_url: https://registry.example.com/apm/jf-skills/v1/packages/acme/foo/versions/1.4.0/download
    resolved_hash: "sha256:abc123..."
    depth: 1
    package_type: apm_package
    deployed_files:
      - .github/skills/foo/SKILL.md
```

`resolved_url` is the trust anchor for re-installs — APM re-fetches from the URL stored in the lockfile, not from the registry name, and re-verifies bytes against `resolved_hash`. See [Lockfile spec](../../reference/lockfile-spec/) for full field semantics.

## Planned features

:::note[Planned]
The following are deferred to a later milestone and not yet implemented:

- **`apm publish` command** — publishing today is done by direct `PUT` against the registry HTTP API.
- **Yank** — marking a published version unavailable.
- **Signature verification** — cryptographic signing of registry-published packages.
:::

## User-level config

Registry URLs and tokens can be stored in `~/.apm/config.json` instead of environment variables, using `apm config set`:

```bash
# Store URL
apm config set registry.jf-skills.url https://registry.example.com/apm/jf-skills

# Store token (written to ~/.apm/config.json, never a repo-tracked file)
apm config set registry.jf-skills.token eyJ...

# Read back
apm config get registry.jf-skills.url

# Remove
apm config unset registry.jf-skills.token
```

Token precedence (highest wins): `APM_REGISTRY_TOKEN_<NAME>` env var → `~/.apm/config.json`.

:::caution
Never put `token:` inside `apm.yml` or `apm-policy.yml`. APM hard-fails when it finds a `token:` key under a registry entry in any repo-tracked YAML file.
:::

These commands are gated behind `apm experimental enable registries`.

## Policy governance

Org admins can mandate registry usage via `apm-policy.yml`:

```yaml
registry_source:
  require:
    - jf-skills          # every dep must be reachable via this registry
  allow_non_registry: false   # block any dep not routed through a registry
```

| Field | Default | Description |
|---|---|---|
| `require` | `[]` | Registry names that MUST be reachable. APM fails-closed if a listed registry has no URL in the project's `registries:` block. |
| `allow_non_registry` | `true` | When `false`, APM blocks installation of any dependency not routed through a configured registry. |

The policy check applies transitively — transitive deps pulled in by registry packages are also validated.

## See also

- [Manifest schema](../../reference/manifest-schema/) — formal grammar for the `registries:` block and `- id:` object form.
- [Lockfile spec](../../reference/lockfile-spec/) — v2 schema and registry-specific fields.
- [Authentication](../../getting-started/authentication/) — full token-resolution chain.

If you operate a registry server, see the [Registry HTTP API](../../reference/registry-http-api/) for the full wire contract.
