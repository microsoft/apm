---
title: "Private Registries"
description: "Configure REST-based APM package registries for internal packages. Covers enabling the feature, per-registry credentials, apm.yml dependency shapes, and apm-policy.yml governance."
sidebar:
  order: 7
---

A **private registry** is a REST endpoint that implements the [Registry HTTP API](../../reference/registry-http-api/) and hosts packages your team controls. Typical deployments are Artifactory, JFrog Platform, or any custom service.

This page is the end-to-end reference for the private-registry workflow: feature flag, credentials, `apm.yml` dependency shapes, and `apm-policy.yml` governance enforcement.

For the general registry concept (public or private), see [Registries](../registries/). For the wire contract a registry server must implement, see [Registry HTTP API](../../reference/registry-http-api/).

::::caution[Experimental — dependency governance feature]
Package registries are behind an experimental flag. Nothing about your existing git-based dependencies changes when you enable it. The flag gates only the `registries:` block parsing, registry resolver, and `registry.*` config keys.

```bash
apm experimental enable registries
```
::::

---

## 1. Enable the feature

```bash
apm experimental enable registries
```

Verify:

```bash
apm experimental list
# registries    enabled
```

Revert at any time:

```bash
apm experimental reset registries
```

---

## 2. Declare registries in `apm.yml`

Add a top-level `registries:` block:

```yaml
name: my-project
version: 1.0.0

registries:
  corp-main:
    url: https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local
  corp-snapshots:
    url: https://artifactory.corp.example.com/artifactory/api/apm/corp-snapshots-local
  default: corp-main   # optional — routes unscoped deps to this registry
```

Rules:
- Registry names use lowercase letters, digits, `-`, and `.`.
- `url:` MUST start with `https://` (or `http://` for local dev).
- `token:` MUST NOT appear here — APM hard-fails if it finds one. Store credentials outside `apm.yml` (see §3).
- Unknown keys under a registry entry are rejected at parse time (typo guard).
- `default:` MUST name one of the configured entries.

---

## 3. Configure credentials

### Environment variables (CI / short-lived)

```bash
# Bearer token (preferred for JFrog / Artifactory)
export APM_REGISTRY_TOKEN_CORP_MAIN=eyJ...

# HTTP Basic (some enterprise registries)
export APM_REGISTRY_USER_CORP_MAIN=alice@corp.example.com
export APM_REGISTRY_PASS_CORP_MAIN=secret
```

The env-var name is derived from the registry name: uppercase, `-` and `.` → `_`.

| Registry name | Env var |
|---|---|
| `corp-main` | `APM_REGISTRY_TOKEN_CORP_MAIN` |
| `corp.snapshots` | `APM_REGISTRY_TOKEN_CORP_SNAPSHOTS` |

Bearer wins when both forms are set.

### `~/.apm/config.json` (developer workstations)

Use `apm config set` to store credentials locally without env vars:

```bash
# Requires: apm experimental enable registries

apm config set registry.corp-main.url \
  https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local

apm config set registry.corp-main.token eyJ...

# Inspect
apm config get registry.corp-main.url
apm config get registry.corp-main.token

# Remove
apm config unset registry.corp-main.token
apm config unset registry.corp-main.url
```

Credentials stored in `~/.apm/config.json` are **user-scoped** and never committed to a repository. Token precedence (highest wins): `APM_REGISTRY_TOKEN_<NAME>` env var → `~/.apm/config.json`.

### Precedence chain (full)

From highest to lowest:

1. `APM_REGISTRY_TOKEN_<NAME>` / `APM_REGISTRY_USER_<NAME>` + `APM_REGISTRY_PASS_<NAME>` (env vars)
2. `registry.<name>.token` in `~/.apm/config.json`
3. Unauthenticated (APM surfaces a remediation hint on 401/403)

Registry URL precedence (highest to lowest): `apm-policy.yml` → project `apm.yml` → workspace `~/.apm/apm.yml` → `~/.apm/config.json`.

---

## 4. Declare registry dependencies

### String shorthand (requires `registries.default`)

When `default:` is set, existing shorthand entries with a semver range route through the default registry automatically:

```yaml
dependencies:
  apm:
    - acme/code-review-prompts#^2.0.0    # → corp-main (default)
    - acme/security-baseline#~1.4.0      # → corp-main (default)
    - acme/git-server#main               # git (no semver range → still git)
```

### Object form — whole package

```yaml
dependencies:
  apm:
    # Explicit registry
    - registry: corp-main
      id: acme/code-review-prompts
      version: ^2.0.0

    # Default registry (registry: omitted; registries.default must be set)
    - id: acme/security-baseline
      version: ~1.4.0
```

### Object form — virtual package (sub-path)

```yaml
dependencies:
  apm:
    - registry: corp-main
      id: acme/prompt-library
      path: prompts/code-review.prompt.md
      version: 1.4.0
      alias: code-review
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Package identity at the registry: `owner/repo`. |
| `version` | yes | Semver range (`^1.0.0`, `~1.2.3`, `>=1.2.0 <2.0.0`) or exact selector. |
| `registry` | no | Name from `registries:`. Defaults to `registries.default` when omitted. |
| `path` | no | Virtual sub-path inside the package. Omit to install the whole package. |
| `alias` | no | Local alias (controls install directory name). |

### Version selectors

Registry routing requires a semver selector. Non-semver refs (`#main`, `#stable`, branch names, commit SHAs) always resolve through Git — they are never forwarded to a registry.

| Selector | Behavior |
|---|---|
| `^1.0.0`, `~1.2.3`, `>=1.0.0 <2.0.0` | Semver range — APM picks the highest matching registry version. |
| `1.4.0` | Exact semver version. |

---

## 5. Governance with `apm-policy.yml`

Platform teams can mandate registry usage and block non-registry sources organization-wide.

### Mandate registry usage

Require that specific registries be reachable. APM **fails-closed** if a listed registry has no URL in the project's `registries:` block — an unconfigured registry is treated as a policy violation, not a no-op:

```yaml
# .github/apm-policy.yml
registry_source:
  require:
    - corp-main
```

### Block non-registry sources

Refuse installation of any dependency not routed through a configured registry:

```yaml
registry_source:
  require:
    - corp-main
  allow_non_registry: false
```

With `allow_non_registry: false`, git-sourced dependencies (including shorthand `owner/repo` entries without a semver range) are blocked at install time.

### Policy fields

| Field | Default | Description |
|---|---|---|
| `require` | `[]` | Registry names that MUST be reachable. Fail-closed if missing from project `registries:`. |
| `allow_non_registry` | `true` | When `false`, blocks any dep not routed through a configured registry. |

Policy checks apply to direct and transitive dependencies.

---

## 6. Known limitations and threat model

### What this provides

- **Byte-level reproducibility.** `resolved_hash` in `apm.lock.yaml` pins the SHA-256 of the downloaded archive. Re-installs verify bytes against the lockfile hash before writing to disk.
- **Token containment.** Tokens stored in `~/.apm/config.json` are user-scoped. APM hard-fails if a `token:` key appears in any repo-tracked YAML file.
- **Policy enforcement.** `registry_source` in `apm-policy.yml` allows platform teams to mandate and restrict dependency sources across the org.

### What this does not yet provide

- **Package signing.** Registry packages are not cryptographically signed at v1. The `resolved_hash` detects corruption or tampering after download, but does not verify publisher identity.
- **SBOM generation.** APM does not produce SLSA provenance attestations or SPDX/CycloneDX bills of materials from registry packages. The lockfile (`apm.lock.yaml`) records the resolved version and hash and is suitable for internal audit, but is not a standards-format SBOM.
- **SHA-256 algorithm agility.** The hash floor is SHA-256. No upgrade path to SHA-384/512 is implemented at v1.

Do not represent this feature as "supply-chain secure," "tamper-proof," or "SLSA-compliant" in compliance documentation or vendor assessments.

---

## 7. Full example

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
    # String shorthand → corp-main (semver range triggers routing)
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
# Developer workstation setup
apm experimental enable registries
apm config set registry.corp-main.token "$(cat ~/.corp-apm-token)"
apm install
```

---

## See also

- [Registries](../registries/) — general registry concept and authentication reference.
- [Registry HTTP API](../../reference/registry-http-api/) — wire contract for registry servers.
- [apm config](../../reference/cli/config/) — full config key reference.
- [Policy schema](../../reference/policy-schema/#registry_source) — `registry_source` field reference.
- [Security model](../../enterprise/security/) — threat model and known limitations.
