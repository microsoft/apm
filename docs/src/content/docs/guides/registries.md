---
title: "Registries"
description: "Install APM packages from public, private, or self-hosted REST registries. Covers apm.yml declaration, per-registry credentials, dependency routing, publish workflow, and policy governance."
sidebar:
  order: 6
---

A **registry** is a REST endpoint that hosts APM packages. Install from a public registry with zero auth, or point at a private / self-hosted registry (Artifactory, JFrog, or any service that implements the [Registry HTTP API](../../reference/registry-http-api/)).

```bash
apm experimental enable registries
apm install acme/code-review-prompts#^2.0.0
```

That is the consumer loop.

::::caution[Experimental]
Package registries are behind an experimental flag. Enable them before adding `registries:` or registry-sourced dependencies; the flag gates only the `registries:` block parsing, the registry resolver, and `registry.*` config keys. Existing Git-based dependencies are unaffected.

```bash
apm experimental enable registries     # enable
apm experimental list                  # verify  -> registries    enabled
apm experimental reset registries      # revert
```
::::

## Compatible backends

| Backend | Status | Notes |
|---|---|---|
| Any server implementing the [Registry HTTP API](../../reference/registry-http-api/) | Supported | Base URL like `https://registry.example.com/api/<name>`; must expose section 3 endpoints and section 6 publish validation |
| GitHub / Git remotes | Not a registry | Default resolver; use `- git:` when a default registry is active |
| APM marketplace | Different surface | Git-hosted index via `apm pack` -- not `apm publish` |

## 1. Install from a public registry

The simplest case: a hosted public registry that needs no auth. Declare the registry URL in `apm.yml`, mark it default, and install.

```yaml
# apm.yml
name: my-project
version: 1.0.0

registries:
  public-apm:
    url: https://registry.example.com/api/public-apm
  default: public-apm

dependencies:
  apm:
    - acme/code-review-prompts#^2.0.0
```

```bash
apm experimental enable registries
apm install
```

On success, `apm.lock.yaml` records `source: registry`, `version`, `resolved_url`, and `resolved_hash` for each registry dep. A `404` usually means the package is not published or the `owner/repo` is wrong.

Registry URLs MUST start with `https://` (or `http://` for local development). Registry names use lowercase letters, digits, `-`, and `.`. Unknown keys under a registry entry are rejected at parse time (typo guard).

## 2. Install from a private or self-hosted registry

Add credentials. Everything from section 1 still applies; the only new step is setting `APM_REGISTRY_TOKEN_{NAME}`.

```yaml
# apm.yml
name: my-project
version: 1.0.0

registries:
  corp-main:
    url: https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local
  default: corp-main

dependencies:
  apm:
    - acme/code-review-prompts#^2.0.0
```

```bash
# Registry name "corp-main" -> APM_REGISTRY_TOKEN_CORP_MAIN
export APM_REGISTRY_TOKEN_CORP_MAIN=eyJ...

apm experimental enable registries
apm install
```

A `401` or `403` almost always means the token is missing or misnamed -- see [Pitfalls](#pitfalls).

### Workstation-only setup (no `registries:` block in `apm.yml`)

When every developer points at the same private registry, configure URL, token, and default locally and keep `apm.yml` free of a `registries:` block:

```bash
apm experimental enable registries
apm config set registry.corp-main.url \
  https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local
apm config set registry.corp-main.token eyJ...
apm config set registry.corp-main.default true
```

```yaml
# apm.yml -- dependencies only; no registries: block
name: my-project
version: 1.0.0

dependencies:
  apm:
    - acme/code-review-prompts#^2.0.0
```

Credentials stored in `~/.apm/config.json` are **user-scoped** and never committed to a repository. Only one registry may be default at a time; setting `registry.<name>.default true` clears any previous default.

### Authentication forms

APM reads credentials from environment variables named after the registry. `{NAME}` is the registry name uppercased, with `-` and `.` mapped to `_`.

| Env var | Auth method |
|---|---|
| `APM_REGISTRY_TOKEN_{NAME}` | `Authorization: Bearer <token>` |
| `APM_REGISTRY_USER_{NAME}` + `APM_REGISTRY_PASS_{NAME}` | `Authorization: Basic <base64(user:pass)>` |

Bearer wins when both forms are set. When neither is set, APM sends the request anonymously and surfaces a remediation hint pointing at `APM_REGISTRY_TOKEN_<NAME>` on `401` / `403`.

```bash
# Bearer (preferred for JFrog / Artifactory)
export APM_REGISTRY_TOKEN_CORP_MAIN=eyJ...

# HTTP Basic (enterprise registries that issue username/password)
export APM_REGISTRY_USER_CORP_MAIN=alice@corp.example.com
export APM_REGISTRY_PASS_CORP_MAIN=secret
```

The `APM_REGISTRY_*` prefix is distinct from `GITHUB_APM_PAT_*`, `PROXY_REGISTRY_*`, and `ARTIFACTORY_APM_TOKEN` -- there is no collision. For the broader auth model, see [Authentication](../../getting-started/authentication/).

:::caution
Never put credentials in `apm.yml` or `apm-policy.yml`. Use `APM_REGISTRY_TOKEN_<NAME>` env vars or `apm config set registry.<name>.token` instead.
:::

### Precedence chains

Token precedence (highest wins):

1. `APM_REGISTRY_TOKEN_<NAME>` / `APM_REGISTRY_USER_<NAME>` + `APM_REGISTRY_PASS_<NAME>` (env vars)
2. `registry.<name>.token` in `~/.apm/config.json`
3. Unauthenticated (APM surfaces a remediation hint on `401` / `403`)

Registry URL precedence (highest wins): `apm-policy.yml` -> project `apm.yml` -> workspace `~/.apm/apm.yml` -> `~/.apm/config.json`.

Default registry precedence (highest wins): project `apm.yml` `registries.default` -> `registry.<name>.default true` in `~/.apm/config.json`.

### `apm config` reference

```bash
# Set URL, token, default
apm config set registry.corp-main.url https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local
apm config set registry.corp-main.token eyJ...
apm config set registry.corp-main.default true

# Inspect
apm config get registry.corp-main.url
apm config get registry.corp-main.token
apm config get registry.corp-main.default

# Remove
apm config unset registry.corp-main.token
apm config unset registry.corp-main.url
apm config unset registry.corp-main.default
```

These commands are gated behind `apm experimental enable registries`. `apm config set registry.<name>.url` is also useful for workspace-level URL overrides (for example, redirecting a registry to a staging server, or reinstalling from a lockfile when the project removed its `registries:` block).

## 3. Route dependencies per registry

There are two ways to point a dependency at a registry.

### String shorthand routed through the default

When a default registry is configured -- via `registries.default` in `apm.yml` or `registry.<name>.default true` in `~/.apm/config.json` -- plain `owner/repo#<ref>` shorthand entries route through it:

```yaml
registries:
  corp-main:
    url: https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local
  default: corp-main

dependencies:
  apm:
    - acme/foo#^1.2.3        # semver range    -> corp-main
    - acme/bar#1.4.0         # exact semver   -> corp-main
    - acme/baz#~2.0.0        # tilde range    -> corp-main
    - acme/qux#stable        # non-semver label, exact-matched -> corp-main
```

Registry-routed deps must include a version selector. Semver versions and
ranges (`1.0.0`, `^1.2.3`, `~2.0`, `>=1.0 <2.0`) use range matching
against the registry catalogue. Non-semver selectors (`stable`, `latest`,
`v1.4.2`, a branch name, or any opaque string) are matched exactly against
the registry's published version list. A missing version selector (no
`#<ref>`) and a malformed range-like ref (e.g. `^1.0` without a patch
component) are both rejected during `apm install` -- the write gate rejects
them before `apm.yml` is changed.

Routing applies to every still-unrouted shorthand entry with a `#<ref>`: a valid semver range is range-matched, any other selector is exact-matched, and a malformed range-like ref is rejected. Object-form entries (`- git:`, `- path:`, `- id:`) are left alone.

### Object form -- explicit per-dep routing and virtual packages

Use the object form to pin a dep to a specific registry, or to install a **virtual package** (a single file or sub-directory inside a published package):

```yaml
dependencies:
  apm:
    # Whole package via the default registry (registry: omitted)
    - id: acme/toolkit
      version: ^2.0.0

    # Whole package routed to a specific registry
    - registry: corp-snapshots
      id: acme/toolkit
      version: ^2.0.0

    # Virtual package -- one file from inside a published package
    - registry: corp-main
      id: acme/prompt-library
      path: prompts/code-review.prompt.md
      version: 1.4.0
      alias: code-review
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Package identity at the registry, in `owner/repo` form. |
| `version` | yes | Exact version or semver range. |
| `registry` | no | Name from the merged registry map. Defaults to the effective default registry when omitted. |
| `path` | no | Sub-path to a file or directory within the published package. Omit to install the whole package. |
| `alias` | no | Local alias (controls install directory name). |

### Version selectors

Registry-routed entries must include a version selector. Semver selectors
use range matching; non-semver selectors are matched exactly against the
registry's published version list:

| Selector | Behavior |
|---|---|
| `1.0.0`, `1.4.2` | Exact semver -- resolves to exactly that version |
| `^1.0.0`, `~1.2.3`, `>=1.2.0 <2.0.0` | Semver range -- APM picks the highest matching version |
| `stable`, `latest`, `v1.4.2`, or any opaque string | Exact match -- matched literally against the registry's published version list |
| `^1.0`, `~2`, `>=1.0` (range operator, malformed) | Rejected -- looks like a semver range but is invalid (e.g. missing patch); fix the range or drop the operator |
| unset (no `#<ref>`) | Rejected -- a version selector is always required for registry-routed dependencies |

Registry-routed deps are byte-for-byte reproducible via `resolved_hash`; Git-routed deps are SHA-reproducible via `resolved_commit`.

### Default-routing precedence summary

| Entry form | Routed to |
|---|---|
| `owner/repo#<any-ref>` | Default registry |
| `- id:` object form (no `registry:`) | Default registry |
| `- registry:` object form (with `registry:`) | Named registry |
| `- git:` object form | Git (always -- explicit override) |
| `- path:` object form | Local filesystem (unchanged) |

A shorthand entry without any ref (`acme/foo`) is rejected at `apm install` time and at parse time -- a version selector is always required for registry-routed dependencies.

:::caution[Behavior change -- not a warning at install time]
There is no one-time migration prompt. Existing Git shorthand deps begin routing to the registry as soon as a default is configured. Plan the audit before enabling the default; see [Pitfalls -- default registry rerouting](#default-registry-silently-reroutes-git-shorthand) and [Migration paths](../../troubleshooting/migration/#6-default-registry-adoption-git--registry-routing).
:::

## 4. What gets recorded in the lockfile

Registry-sourced dependencies add four fields to their lockfile entry: `source: registry`, `version`, `resolved_url`, and `resolved_hash` (sha256 of the archive bytes). The lockfile is promoted to `lockfile_version: "2"` when any dep is registry-sourced OR carries git-source semver resolution fields (`constraint`, `resolved_tag`, or `resolved_at` -- issue #1488). Projects that use neither feature keep `lockfile_version: "1"` forever, even on a newer client.

```yaml
dependencies:
  - repo_url: acme/foo
    source: registry
    version: "1.4.0"
    resolved_url: https://registry.example.com/apm/corp-main/v1/packages/acme/foo/versions/1.4.0/download
    resolved_hash: "sha256:abc123..."
    depth: 1
    package_type: apm_package
    deployed_files:
      - .github/skills/foo/SKILL.md
```

`resolved_url` is the trust anchor for re-installs -- APM re-fetches from the URL stored in the lockfile, not from the registry name, and re-verifies bytes against `resolved_hash`. A hash mismatch aborts the install before extraction. See [Lockfile spec](../../reference/lockfile-spec/) for full field semantics.

## 5. Publish a package (producer summary)

```bash
# Producer -- package root with apm.yml, .apm/, and (optionally) a registries: block
apm publish --package acme/my-skill --dry-run -v
apm publish --package acme/my-skill

# Consumer -- another repo
apm install acme/internal-tools#^1.0.0
```

[`apm publish`](../../reference/cli/publish/) reads `apm.yml`, builds a **flat registry archive** (`.zip` with `apm.yml`, `.apm/`, and standard documentation files at the archive root), and uploads via `PUT /v1/packages/{owner}/{repo}/versions/{version}`. Consumers with a default registry configured install with the same `owner/repo#version` shorthand they would use for GitHub.

Registry archives use the **APM source layout** that `apm install` and the [Registry HTTP API section 6](../../reference/registry-http-api/#6-server-validation-rules-publish) expect -- not the plugin bundle wrapper from `apm pack --archive` (`{name}-{version}/plugin.json`). If you already ship marketplace plugin bundles, either repack as a flat archive or pass `--zip`.

**Auto-pack requirements:**

- `apm.yml` with `name:` and `version:`
- A `.apm/` directory with your primitives (skills, instructions, hooks, etc.)

Auto-pack writes `{name}-{version}.zip` in the project root, includes `README.md`, `CHANGELOG.md`, and `LICENSE` / `LICENCE` when present (case-insensitive, symlinks excluded), and skips macOS `._*` / `.DS_Store` sidecars.

**Custom layouts** -- build the zip yourself and pass `--zip`:

```bash
# Cross-platform zip build (Python stdlib -- no extra tools needed)
python -m zipfile -c ./build/my-skill-0.0.1.zip apm.yml .apm/

apm publish --package acme/my-skill --zip ./build/my-skill-0.0.1.zip
```

Some registries accept archives without validating `apm.yml` on upload; APM still validates on install. Prefer a valid flat layout at publish time.

```bash
# Auto-pack flat archive and publish to the only configured registry
apm publish --package acme/my-skill

# Choose a registry when multiple are configured
apm publish --package acme/my-skill --registry corp-main

# Publish a pre-built zip (skip auto-pack)
apm publish --package acme/my-skill --zip ./build/my-package-1.0.0.zip

# Preview what would be uploaded without uploading
apm publish --package acme/my-skill --dry-run
```

| Option | Description |
|---|---|
| `--registry NAME` | Registry name from the `registries:` block. Required when multiple registries are configured. |
| `--package OWNER/REPO` | Package identity to publish as (required, e.g. `acme/my-skill`). |
| `--zip PATH` | Path to a pre-built flat `.zip` archive. Skips auto-pack. |
| `--dry-run` | Preview without uploading. |
| `--verbose` / `-v` | Show detailed output. |

`apm.yml` must declare a `version:` field. Publishing the same version twice returns `409 Conflict` -- bump the version to publish again.

:::note[`apm pack` vs `apm publish`]
[`apm pack`](../../reference/cli/pack/) produces distributable **plugin bundles** (and marketplace artifacts) for Git/marketplace flows. [`apm publish`](../../reference/cli/publish/) produces **flat registry archives** for REST registries. The two commands serve different distribution surfaces.
:::

:::note[Planned]
The following are deferred to a later milestone and not yet implemented:

- **Yank** -- marking a published version unavailable.
- **Signature verification** -- cryptographic signing of registry-published packages.
:::

## 6. Enterprise policy

Org admins can mandate registry usage and block non-registry sources organization-wide via `apm-policy.yml`:

```yaml
# .github/apm-policy.yml
registry_source:
  require:
    - corp-main          # every dep must be reachable via this registry
  allow_non_registry: false   # block any dep not routed through a registry
```

With `allow_non_registry: false`, git-sourced dependencies (including shorthand `owner/repo` entries without a registry route) are blocked at install time. The policy check applies transitively -- transitive deps pulled in by registry packages are also validated. APM **fails-closed** if a listed registry has no URL in the merged registry map (from `apm.yml`, `~/.apm/apm.yml`, or `~/.apm/config.json`).

For the full governance narrative -- rollout sequencing, audit, drift, and CI gating -- see the [Governance guide](../../enterprise/governance-guide/). Field-level reference for `registry_source` lives in [Policy schema](../../reference/policy-schema/#registry_source).

## 7. Known limitations and threat model

### Guarantees

- **Byte-level reproducibility.** `resolved_hash` in `apm.lock.yaml` pins the SHA-256 of the downloaded archive. Re-installs verify bytes against the lockfile hash before writing to disk; a mismatch aborts the install.
- **Token containment.** Tokens stored in `~/.apm/config.json` are user-scoped and never committed to a repository.
- **Policy enforcement.** `registry_source` in `apm-policy.yml` allows platform teams to mandate and restrict dependency sources across the org.

### What this does not yet provide

- **Package signing.** Registry packages are not cryptographically signed. The `resolved_hash` detects corruption or tampering after download, but does not verify publisher identity.
- **SBOM generation.** APM does not produce SLSA provenance attestations or SPDX/CycloneDX bills of materials from registry packages. The lockfile (`apm.lock.yaml`) records the resolved version and hash and is suitable for internal audit, but is not a standards-format SBOM.
- **SHA-256 algorithm agility.** The hash floor is SHA-256. No upgrade path to SHA-384/512 is currently implemented.

Do not represent this feature as "supply-chain secure," "tamper-proof," or "SLSA-compliant" in compliance documentation or vendor assessments.

## Pitfalls

### Misspelled env vars look like auth failures

When no registry token is found, APM sends the request **anonymously** first and only prints credential remediation on `401` / `403`. A typo in the env var name (for example `APM_REGISTRY_TOKEN_CORP_MAI` instead of `APM_REGISTRY_TOKEN_CORP_MAIN`) is treated the same as a missing token -- you get a generic auth error, not "unknown env var."

Verify the exact variable name:

```bash
# Registry name corp-main -> APM_REGISTRY_TOKEN_CORP_MAIN
echo "$APM_REGISTRY_TOKEN_CORP_MAIN" | wc -c   # should be > 1
apm config get registry.corp-main.token        # config.json fallback
```

Use `apm config set registry.<name>.token` when debugging locally so a missing export does not masquerade as a server-side permission problem.

### Default registry silently reroutes Git shorthand

Enabling a default registry (`registries.default` or `registry.<name>.default true`) routes **every** `owner/repo#ref` shorthand to the registry -- including deps that previously installed from GitHub. There is no migration warning on `apm install`; the first signal is often a registry 404 ("no versions") instead of a git clone.

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

See [Migration paths -- default registry adoption](../../troubleshooting/migration/#6-default-registry-adoption-git--registry-routing).

### Registry names that sanitize to the same env var

Env var names derive from the registry name by uppercasing and mapping `-` and `.` to `_`. Distinct registry names can collapse to the **same** env var:

| Registry names in `apm.yml` | Shared env var |
|---|---|
| `corp-main` | `APM_REGISTRY_TOKEN_CORP_MAIN` |
| `corp.main` | `APM_REGISTRY_TOKEN_CORP_MAIN` |
| `Corp-Main` | `APM_REGISTRY_TOKEN_CORP_MAIN` |

Do not configure two different registries whose names sanitize identically -- they would share one token slot. Prefer hyphenated names (`corp-main`) and avoid dots in registry names when multiple registries coexist.

## Full example

```yaml
# apm.yml
name: my-project
version: 1.0.0

registries:
  corp-main:
    url: https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local
  default: corp-main

dependencies:
  apm:
    # String shorthand -> corp-main (semver range)
    - acme/code-review-prompts#^2.0.0

    # Object form, whole package, explicit registry
    - registry: corp-main
      id: acme/security-baseline
      version: ~1.4.0

    # Object form, virtual package
    - registry: corp-main
      id: acme/prompt-library
      path: prompts/code-review.prompt.md
      version: 1.4.0
```

```yaml
# .github/apm-policy.yml
registry_source:
  require:
    - corp-main
  allow_non_registry: false
```

```bash
# Developer workstation setup (config-only registry)
apm experimental enable registries
apm config set registry.corp-main.url https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local
apm config set registry.corp-main.token "$(cat ~/.corp-apm-token)"
apm config set registry.corp-main.default true
apm install
```

## See also

- [Manifest schema](../../reference/manifest-schema/) -- formal grammar for the `registries:` block and `- id:` object form.
- [Lockfile spec](../../reference/lockfile-spec/) -- lockfile schema and registry-specific fields.
- [Authentication](../../getting-started/authentication/) -- full token-resolution chain.
- [apm config](../../reference/cli/config/) -- full config key reference.
- [Policy schema](../../reference/policy-schema/#registry_source) -- `registry_source` field reference.
- [Governance guide](../../enterprise/governance-guide/) -- enterprise rollout, audit, and CI gating.
- [Security model](../../enterprise/security/) -- threat model and known limitations.
- [Registry HTTP API](../../reference/registry-http-api/) -- wire contract for registry servers.
