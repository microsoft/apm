---
title: "Registries"
description: "Declare REST-based APM registries in apm.yml or ~/.apm/config.json and consume packages from them alongside Git-hosted dependencies."
sidebar:
  order: 6
---

A **registry** is a REST-based source for APM packages. Any service that implements the [Registry HTTP API](../../reference/registry-http-api/) qualifies. Registries sit alongside the existing Git resolver: declare registry names and URLs in `apm.yml`, in `~/.apm/config.json`, or both, then route individual dependencies to a registry by name or through a default. Registries are strictly additive — when the experimental flag is off, or when no default registry is configured at any layer, every existing dependency form continues to resolve through Git exactly as before.

::::caution[Experimental]
Package registries are currently behind an experimental flag. Enable them before adding `registries:` or registry-sourced dependencies:

```bash
apm experimental enable registries
```
::::

## Compatible backends

| Backend | Status | Notes |
|---|---|---|
| Any server implementing the [Registry HTTP API](../../reference/registry-http-api/) | Supported | Base URL like `https://registry.example.com/api/<name>`; must expose §3 endpoints and §6 publish validation |
| GitHub / Git remotes | Not a registry | Default resolver; use `- git:` when a default registry is active |
| APM marketplace | Different surface | Git-hosted index via `apm pack` — not `apm publish` |

## Try it now

Install a package that is already on your registry. Replace the URL, token, and dependency with yours.

```bash
apm experimental enable registries
apm config set registry.corp.url https://registry.example.com/api/my-team
apm config set registry.corp.token "$YOUR_TOKEN"
apm config set registry.corp.default true

mkdir registry-demo && cd registry-demo
cat > apm.yml <<'EOF'
name: registry-demo
version: 1.0.0
dependencies:
  apm:
    - acme/internal-tools#^1.0.0
EOF

apm install
```

On success, `apm.lock` records `source: registry` and `resolved_hash`. A `404` usually means the package is not published or the `owner/repo` is wrong; `401`/`403` usually means the token is missing — registry name `corp` maps to `APM_REGISTRY_TOKEN_CORP`. See [Pitfalls](#pitfalls) for env-var typos and default-routing surprises.

## Declare a registry

### Project manifest (`apm.yml`)

Add a top-level `registries:` block when the team shares registry URLs in-repo:

```yaml
registries:
  jf-skills:
    url: https://registry.example.com/apm/jf-skills
  default: jf-skills
```

Each entry is a name mapped to a base URL. The optional `default:` key names one of the configured entries; when set, plain string-shorthand APM dependencies route through it (see [Default routing](#default-routing) below). Registry URLs MUST start with `https://` (or `http://` for local development).

The registry name is used for env-var auth lookup. Use lowercase letters, digits, `-`, and `.`.

### User-level default (`~/.apm/config.json`)

For developer workstations, you can configure URL, token, and default entirely in user config and omit `registries:` from `apm.yml`:

```bash
apm experimental enable registries
apm config set registry.jf-skills.url https://registry.example.com/apm/jf-skills
apm config set registry.jf-skills.token eyJ...
apm config set registry.jf-skills.default true
```

Only one registry may be default at a time. Setting `registry.<name>.default true` clears any previous default. Project `registries.default` in `apm.yml` wins when both are set.

## Reference a registry-sourced dependency

There are two ways to point a dependency at a registry.

### 1. String shorthand routed through the default

When a default registry is configured — via `registries.default` in `apm.yml` or `registry.<name>.default true` in `~/.apm/config.json` — plain `owner/repo` shorthand entries route through that registry. The same syntax already used for GitHub dependencies, but now resolved over HTTP:

```yaml
registries:
  jf-skills:
    url: https://registry.example.com/apm/jf-skills
  default: jf-skills

dependencies:
  apm:
    - acme/foo#^1.2.3        # semver range → resolved via jf-skills
    - acme/bar#stable        # any ref, including opaque labels → resolved via jf-skills
```

Routing is unconditional: every still-unrouted shorthand entry with a `#<ref>` is sent through the default registry, regardless of what the ref looks like. Object-form entries (`- git:`, `- path:`, `- id:`) are left alone.

### 2. Object form

Use the object form for explicit per-dep registry routing, or to install a **virtual package** (a single file or sub-directory inside a published package):

```yaml
dependencies:
  apm:
    # Whole package via the default registry
    - id: acme/toolkit
      version: ^2.0.0

    # Whole package routed to a specific registry
    - registry: jf-skills
      id: acme/toolkit
      version: ^2.0.0

    # Virtual package — one file from inside a published package
    - registry: jf-skills
      id: acme/prompt-pack
      path: prompts/review.prompt.md
      version: 1.4.0
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Package identity at the registry, in `owner/repo` form. |
| `version` | yes | Exact version or semver range. |
| `registry` | no | Name from the merged registry map. Defaults to the effective default registry when omitted. |
| `path` | no | Sub-path to a file or directory within the published package. Omit to install the whole package. |
| `alias` | no | Local alias (controls install directory name). |

## Version selectors

Registry-routed entries must specify a version selector — the registry uses it
to look up or range-match against the versions it has published. Version strings
are opaque to APM; the registry decides what they mean. Semver ranges are
supported when the registry publishes semver-tagged versions:

| Selector | Behavior |
|---|---|
| `1.0.0`, `v1.4.2` | Exact version string — matched literally against the registry catalogue |
| `^1.0.0`, `~1.2.3`, `>=1.2.0 <2.0.0` | Semver range — APM picks the highest matching version |
| `stable`, `latest` | Opaque label — matched literally; server decides what it resolves to |
| unset (no `#<ref>`) | Rejected — a version is always required for registry-routed dependencies |

```yaml
dependencies:
  apm:
    - acme/foo#^1.2.3                        # semver range → registry
    - acme/bar#stable                        # opaque label → registry
    - acme/baz#v2.0.0                        # v-prefixed → registry
    - git: https://github.com/acme/qux.git   # explicit Git pin
      ref: main
```

Registry-routed deps are byte-for-byte reproducible via `resolved_hash`;
Git-routed deps are SHA-reproducible via `resolved_commit`.

## Default routing

When a default registry is configured at any layer, all string-shorthand entries route to the
registry — regardless of what the ref looks like. Use the explicit `- git:`
object form to keep a dependency on Git when a default registry is active:

:::caution[Behavior change — not a warning at install time]
There is no one-time migration prompt. Existing Git shorthand deps begin routing to the registry as soon as a default is configured. Plan the audit before enabling the default; see [Pitfalls — default registry rerouting](#default-registry-silently-reroutes-git-shorthand) and [Migration paths](../../troubleshooting/migration/#6-default-registry-adoption-git--registry-routing).
:::

**Default precedence (highest wins):** project `apm.yml` `registries.default` → `registry.<name>.default true` in `~/.apm/config.json`.

| Entry form | Routed to |
|---|---|
| `owner/repo#<any-ref>` | Default registry |
| `- id:` object form (no `registry:`) | Default registry |
| `- registry:` object form (with `registry:`) | Named registry |
| `- git:` object form | Git (always — explicit override) |
| `- path:` object form | Local filesystem (unchanged) |

A shorthand entry without any ref (`acme/foo`) is always rejected — a version
selector is required for registry-routed dependencies.

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

`resolved_url` is the trust anchor for re-installs — APM re-fetches from the URL stored in the lockfile, not from the registry name, and re-verifies bytes against `resolved_hash`. A hash mismatch aborts the install before extraction. See [Lockfile spec](../../reference/lockfile-spec/) for full field semantics.

## End to end: publish and install

```bash
# Producer — package root with apm.yml, .apm/, and a registries: block
apm publish --dry-run -v
apm publish

# Consumer — another repo (or registry-demo above)
apm install acme/internal-tools#^1.0.0
```

That is the loop. [`apm publish`](../../reference/cli/publish/) reads `apm.yml`, builds a **flat registry archive** (`.tar.gz` with `apm.yml` and `.apm/` at the tarball root), and uploads via `PUT /v1/packages/{owner}/{repo}/versions/{version}`. Consumers with a default registry configured install with the same `owner/repo#version` shorthand they would use for GitHub.

Registry archives use the **APM source layout** that `apm install` and the [Registry HTTP API §6](../../reference/registry-http-api/#6-server-validation-rules-publish) expect — not the plugin bundle wrapper from `apm pack --archive` (`{name}-{version}/plugin.json`). If you already ship marketplace plugin bundles, either repack as a flat archive or pass `--tarball`.

**Auto-pack requirements:**

- `apm.yml` with `name:` and `version:` (and `source:` when the registry identity differs from the package name)
- A `.apm/` directory with your primitives (skills, instructions, hooks, etc.)

Auto-pack writes `{name}-{version}.tar.gz` in the project root and skips macOS `._*` / `.DS_Store` sidecars.

**Skill-only or custom layouts** — build the tarball yourself and pass `--tarball`:

```bash
tar czf my-skill-0.0.1.tar.gz apm.yml SKILL.md
apm publish --tarball my-skill-0.0.1.tar.gz
```

Some registries accept archives without validating `apm.yml` on upload; APM still validates on install. Prefer a valid flat layout at publish time.

```bash
# Auto-pack flat archive and publish to the only configured registry
apm publish

# Choose a registry when multiple are configured
apm publish --registry corp-main

# Publish a pre-built flat tarball (skip auto-pack)
apm publish --tarball ./build/my-package-1.0.0.tar.gz

# Preview what would be uploaded without uploading
apm publish --dry-run
```

| Option | Description |
|---|---|
| `--registry NAME` | Registry name from the `registries:` block. Required when multiple registries are configured. |
| `--package OWNER/REPO` | Override owner/repo identity (default: parsed from `source:` in `apm.yml`). |
| `--tarball PATH` | Path to a pre-built flat `.tar.gz` tarball. Skips auto-pack. |
| `--dry-run` | Preview without uploading. |
| `--verbose` / `-v` | Show detailed output. |

`apm.yml` must declare a `version:` field. Publishing the same version twice returns `409 Conflict` — bump the version to publish again.

:::note[`apm pack` vs `apm publish`]
[`apm pack`](../../reference/cli/pack/) produces distributable **plugin bundles** (and marketplace artifacts) for Git/marketplace flows. [`apm publish`](../../reference/cli/publish/) produces **flat registry archives** for REST registries. The two commands serve different distribution surfaces.
:::

## Planned features

:::note[Planned]
The following are deferred to a later milestone and not yet implemented:

- **Yank** — marking a published version unavailable.
- **Signature verification** — cryptographic signing of registry-published packages.
:::

## User-level config

Registry URLs can live in `apm.yml` (committed, shared with your team), in `~/.apm/config.json` (user-scoped), or both — APM merges them at install time. Tokens are per-user and must never be committed:

```bash
# Store URL, token, and default for a workstation-only setup
apm config set registry.jf-skills.url https://registry.example.com/apm/jf-skills
apm config set registry.jf-skills.token eyJ...
apm config set registry.jf-skills.default true

# Or store only the token when the URL is already in apm.yml
apm config set registry.jf-skills.token eyJ...

# Read back
apm config get registry.jf-skills.token
apm config get registry.jf-skills.default

# Remove
apm config unset registry.jf-skills.token
apm config unset registry.jf-skills.default
```

Token precedence (highest wins): `APM_REGISTRY_TOKEN_<NAME>` env var → `~/.apm/config.json`.

URL precedence (highest wins): `apm-policy.yml` → project `apm.yml` → workspace `~/.apm/apm.yml` → `~/.apm/config.json`.

`apm config set registry.<name>.url` is also useful for workspace-level URL overrides (e.g. redirecting a registry to a staging server, or reinstalling from a lockfile when the project removed its `registries:` block). For normal team use, the URL usually lives in `apm.yml`; for single-machine private-registry workflows, URL + default in `config.json` lets `apm.yml` list only dependencies.

:::caution
Never put credentials in `apm.yml` or `apm-policy.yml`. Use `APM_REGISTRY_TOKEN_<NAME>` env vars or `apm config set registry.<name>.token` instead.
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
| `require` | `[]` | Registry names that MUST be reachable. APM fails-closed if a listed registry has no URL in the merged registry map (from `apm.yml`, `~/.apm/apm.yml`, or `~/.apm/config.json`). |
| `allow_non_registry` | `true` | When `false`, APM blocks installation of any dependency not routed through a configured registry. |

The policy check applies transitively — transitive deps pulled in by registry packages are also validated.

## Pitfalls

### Misspelled env vars look like auth failures

When no registry token is found, APM sends the request **anonymously** first and only prints credential remediation on `401`/`403`. A typo in the env var name (for example `APM_REGISTRY_TOKEN_JF_SKILL` instead of `APM_REGISTRY_TOKEN_JF_SKILLS`) is treated the same as a missing token — you get a generic auth error, not “unknown env var.”

Verify the exact variable name:

```bash
# Registry name jf-skills -> APM_REGISTRY_TOKEN_JF_SKILLS
echo "$APM_REGISTRY_TOKEN_JF_SKILLS" | wc -c   # should be > 1
apm config get registry.jf-skills.token        # config.json fallback
```

Use `apm config set registry.<name>.token` when debugging locally so a missing export does not masquerade as a server-side permission problem.

### Default registry silently reroutes Git shorthand

Enabling a default registry (`registries.default` or `registry.<name>.default true`) routes **every** `owner/repo#ref` shorthand to the registry — including deps that previously installed from GitHub. There is no migration warning on `apm install`; the first signal is often a registry 404 (“no versions”) instead of a git clone.

**Before turning on a default registry:**

1. Audit `apm.yml` (and transitive packages) for shorthand deps that must stay on Git.
2. Pin those entries explicitly:

   ```yaml
   dependencies:
     apm:
       - git: https://github.com/microsoft/apm-sample-package.git
         ref: v1.0.0
   ```

3. Run `apm install --dry-run` or a trial install in a branch and confirm lockfile `source:` fields.

See [Migration paths — default registry adoption](../../troubleshooting/migration/#6-default-registry-adoption-git--registry-routing).

### Registry names that sanitize to the same env var

Env var names derive from the registry name by uppercasing and mapping `-` and `.` to `_`. Distinct registry names can collapse to the **same** env var:

| Registry names in `apm.yml` | Shared env var |
|---|---|
| `corp-main` | `APM_REGISTRY_TOKEN_CORP_MAIN` |
| `corp.main` | `APM_REGISTRY_TOKEN_CORP_MAIN` |
| `Corp-Main` | `APM_REGISTRY_TOKEN_CORP_MAIN` |

Do not configure two different registries whose names sanitize identically — they would share one token slot. Prefer hyphenated names (`corp-main`) and avoid dots in registry names when multiple registries coexist.

## See also

- [Manifest schema](../../reference/manifest-schema/) — formal grammar for the `registries:` block and `- id:` object form.
- [Lockfile spec](../../reference/lockfile-spec/) — lockfile schema and registry-specific fields.
- [Authentication](../../getting-started/authentication/) — full token-resolution chain.

If you operate a registry server, see the [Registry HTTP API](../../reference/registry-http-api/) for the full wire contract.
