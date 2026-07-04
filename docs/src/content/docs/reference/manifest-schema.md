---
title: "Manifest Schema"
description: "The apm.yml format -- top-level fields, dependencies, marketplace block, and integrator contract."
sidebar:
  order: 1
---

> **Normative reference:** this page documents the v0.3 working-draft manifest schema as implemented by the current CLI. The normative, ratified contract for v0.1 is defined in [OpenAPM v0.1, Section 4 (Manifest)](/apm/specs/openapm-v01/) and published as JSON Schema at [`manifest-v0.1.schema.json`](/apm/specs/schemas/manifest-v0.1.schema.json).

<dl>
<dt>Version</dt><dd>0.3 (Working Draft)</dd>
<dt>Date</dt><dd>2026-05-20</dd>
<dt>Editors</dt><dd>Daniel Meppiel (Microsoft)</dd>
<dt>Repository</dt><dd>https://github.com/microsoft/apm</dd>
<dt>Format</dt><dd>YAML 1.2</dd>
</dl>

## Status of This Document

This is a **Working Draft**. It may be updated, replaced, or made obsolete at any time. It is inappropriate to cite this document as other than work in progress.

This specification defines the manifest format (`apm.yml`) used by the Agent Package Manager (APM). Feedback is welcome via [GitHub Issues](https://github.com/microsoft/apm/issues).

---

## Abstract

The `apm.yml` manifest declares the full closure of agent primitive dependencies, MCP servers, scripts, compilation settings, consumer-side policy controls, and (optionally) marketplace authoring metadata for a project. It is the contract between package authors, runtimes, and integrators: any conforming resolver can consume this format to install, compile, run, and pack agentic workflows.

---

## 1. Conformance

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119).

A conforming manifest is a YAML 1.2 document that satisfies all MUST-level requirements in this specification. A conforming resolver is a program that correctly parses conforming manifests and performs dependency resolution as described herein.

---

## 2. Document Structure

A conforming manifest MUST be a YAML mapping at the top level with the following shape:

```yaml
# apm.yml
name:          <string>                  # REQUIRED
version:       <string>                  # REQUIRED
description:   <string>
author:        <string>
license:       <string>
target:        <enum | list<enum>>
type:          <enum>
scripts:       <map<string, string>>
includes:      <enum | list<string>>
registries:    <map<string, RegistryEntry> & {default?: <string>}>
dependencies:
  apm:         <list<ApmDependency>>
  mcp:         <list<McpDependency>>
  lsp:         <list<LspDependency>>
devDependencies:
  apm:         <list<ApmDependency>>
  mcp:         <list<McpDependency>>
  lsp:         <list<LspDependency>>
compilation:   <CompilationConfig>
policy:        <PolicyConfig>
marketplace:   <MarketplaceConfig>       # OPTIONAL; marketplace authoring
```

Two fields are REQUIRED at parse time: `name` and `version`. All other fields are OPTIONAL. Unknown top-level keys MUST be preserved by writers but MAY be ignored by resolvers.

The `marketplace:` block is the source for `apm pack`'s marketplace output. Repositories that do not publish a marketplace omit it entirely. See [Section 7](#7-marketplace-authoring-block).

Newly initialised projects (`apm init`) are scaffolded by the CLI; see [`apm init`](./cli/init/) for the templates.

---

## 3. Top-Level Fields

### 3.1. `name`

| | |
|---|---|
| **Type** | `string` |
| **Required** | MUST be present |
| **Description** | Package identifier. Free-form string (no pattern enforced at parse time). Convention: alphanumeric, dots, hyphens, underscores. Normalised to kebab-case in the compiled `marketplace.json` -- see [7.2](#72-block-fields). |

### 3.2. `version`

| | |
|---|---|
| **Type** | `string` |
| **Required** | MUST be present |
| **Pattern** | `^\d+\.\d+\.\d+` (semver; pre-release/build suffixes allowed) |
| **Description** | Semantic version. A value that does not match the pattern SHOULD produce a validation warning (non-blocking). |

### 3.3. `description`

| | |
|---|---|
| **Type** | `string` |
| **Required** | OPTIONAL |
| **Description** | Brief human-readable description. Inherited by the `marketplace:` block when not overridden. |

### 3.4. `author`

| | |
|---|---|
| **Type** | `string` |
| **Required** | OPTIONAL |
| **Description** | Package author or organization. |

### 3.5. `license`

| | |
|---|---|
| **Type** | `string` |
| **Required** | OPTIONAL |
| **Description** | SPDX license expression (e.g. `MIT`, `Apache-2.0`, `(MIT OR Apache-2.0)`) declaring the package license. |

The value is recorded verbatim into a consumer's lockfile as
[`declared_license`](./lockfile-spec/) at resolve time, syntax-validated
offline against the bundled SPDX id set, and surfaced by
[`apm lock export`](./cli/lock/#export-sbom-inventory). Special tokens
(`UNLICENSED`, `SEE LICENSE IN <file>`) and unrecognized strings are accepted
and recorded as a named license -- a declaration is never rejected and never
blocks packing or publishing. APM records what the manifest *declares*; it
never reads or concludes a license from `LICENSE` file text. When omitted, the
SBOM marks the component unknown -- CycloneDX omits the license entry and SPDX
writes the literal `NOASSERTION` -- and `apm pack` / `apm publish` print an
actionable nudge (the authoring path only).

### 3.6. `target`

| | |
|---|---|
| **Type** | `string` or `list<string>` |
| **Required** | OPTIONAL |
| **Default** | Auto-detect from folder presence (see below). |
| **Allowed values** | `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `windsurf`, `kiro`, `agent-skills` |

Controls which output targets are generated during compilation, installation, and packing. Accepts a single string or a YAML list. Unknown values MUST raise a parse error at load time, naming the offending token.

When `target:` is omitted, APM auto-detects targets from folder presence (`.github/`, `.claude/`, `.codex/`, `.gemini/`, `.opencode/`, `.windsurf/`, `.kiro/`). Auto-detection applies only when `target:` is unset; once set, the field is authoritative.

```yaml
# Single target
target: copilot

# Multiple targets (flow-list form)
target: [claude, copilot]

# Multiple targets (block-list form, equivalent)
target:
  - claude
  - copilot
```

When a list is specified, only those targets are compiled, installed, and packed; no output is generated for unlisted targets.

A plural alias `targets:` (YAML list only) is also accepted and takes precedence over the legacy CSV form when both are declared. Prefer `targets:` in new manifests; `target:` remains supported for backward compatibility.

| Value | Effect |
|---|---|
| `copilot` | Emits `AGENTS.md` at the project root (and per-directory files in distributed mode). |
| `claude` | Emits `CLAUDE.md` at the project root. |
| `cursor` | Emits to `.cursor/rules/`, `.cursor/agents/`, `.cursor/skills/`. |
| `opencode` | Emits to `.opencode/agents/`, `.opencode/commands/`, `.opencode/skills/`. |
| `codex` | Emits `AGENTS.md` and deploys skills to `.agents/skills/`, agents to `.codex/agents/`. |
| `gemini` | Emits `GEMINI.md` and deploys to `.gemini/commands/`, `.gemini/skills/`, `.gemini/settings.json`. |
| `windsurf` | Emits `AGENTS.md` and deploys to `.windsurf/rules/`, `.agents/skills/`, `.windsurf/workflows/`, `.windsurf/hooks.json`. |
| `kiro` | Emits `AGENTS.md` and deploys to `.kiro/steering/`, `.kiro/skills/`, `.kiro/hooks/`, `.kiro/settings/mcp.json`. |
| `agent-skills` | Deploys Agent Skills under `.agents/skills/`. |

:::tip[Deterministic committed output]
Teams that commit the files `apm compile` generates face a consistency problem:
without `target:` or `targets:` set, auto-detection decides which files to produce
based on which tool folders exist on the local machine. The committed set silently tracks
whoever last ran `apm compile`. Setting `targets:` makes the output deterministic
for every developer, CI runner, and cloud agent that relies on the checked-in
generated files without running `apm compile` locally. See
[Pin committed output with targets:](./cli/compile/#pin-committed-output-with-targets).
:::

### 3.7. `type`

| | |
|---|---|
| **Type** | `enum<string>` |
| **Required** | OPTIONAL |
| **Default** | None (behaviour driven by package content; synthesized plugin manifests use `hybrid`). |
| **Allowed values** | `instructions`, `skill`, `hybrid`, `prompts` |

Declares how the package's content is processed during install and compile. Today behaviour is driven by package content (presence of `SKILL.md`, component directories, etc.); this field is reserved for future explicit overrides.

| Value | Behaviour |
|---|---|
| `instructions` | Compiled into `AGENTS.md` only. No skill directory created. |
| `skill` | Installed as a native skill only. No `AGENTS.md` output. |
| `hybrid` | Both `AGENTS.md` compilation and skill installation. |
| `prompts` | Commands/prompts only. No instructions or skills. |

### 3.8. `scripts`

| | |
|---|---|
| **Type** | `map<string, string>` |
| **Required** | OPTIONAL |
| **Key** | Script name (free-form string). |
| **Value** | Shell command string. |

Named commands executed via `apm run <name>`. The script body MUST support `--param key=value` substitution (`{key}` placeholders in the command string are replaced before execution).

The script name `start` is the default invoked by a bare `apm run` (no name given) and SHOULD be present in publishable packages so consumers have a one-command entry point.

```yaml
scripts:
  start:  "copilot -p 'README.prompt.md'"
  review: "copilot -p 'code-review.prompt.md'"
  impl:   "copilot -p 'implement-feature.prompt.md'"
```

### 3.9. `includes`

| | |
|---|---|
| **Type** | `string` (literal `auto`) or `list<string>` |
| **Required** | OPTIONAL |
| **Default** | Undeclared (legacy implicit auto-publish; flagged by `apm audit`). |
| **Allowed values** | `auto` or a list of paths relative to the project root. |

Declares which local `.apm/` content the project consents to publish when packing or deploying. Three forms are supported:

1. **Undeclared** (field omitted). Legacy behaviour: all local `.apm/` content is published as if `auto` were set. `apm audit` emits an `includes-consent` advisory whenever local content is deployed under this form.
2. **`includes: auto`**. Explicit consent to publish all local `.apm/` content via the file scanner. No path enumeration required. Default for newly initialised projects.
3. **`includes: [<path>, ...]`**. Explicit allow-list of paths the project consents to publish. Strongest governance form; changes are reviewable in PR diffs.

```yaml
# Form 1: undeclared (legacy; audit advisory)
# includes: <omitted>

# Form 2: explicit auto-publish (default for new projects)
includes: auto

# Form 3: explicit path list (strongest governance)
# includes:
#   - .apm/instructions/
#   - .apm/skills/my-skill/
```

`includes:` is allow-list only. There is no `exclude:` form. To keep maintainer-only primitives out of shipped artifacts, author them OUTSIDE `.apm/` and reference them via a local-path devDependency. See [Dev-only Primitives](../concepts/primitives-and-targets/#dev-only-primitives).

When `policy.manifest.require_explicit_includes` is `true` (see [Policy reference](../enterprise/policy-reference/)), only form 3 passes; `auto` and undeclared are rejected at install/audit time by the `explicit-includes` check (not at YAML parse time).

### 3.10. `policy`

| | |
|---|---|
| **Type** | `map<string, string>` |
| **Required** | OPTIONAL |
| **Description** | Consumer-side controls for org policy discovery and verification. All sub-keys are optional; defaults preserve current fail-open install behaviour. |

```yaml
policy:
  fetch_failure_default: warn      # warn | block (default warn)
  hash: "sha256:<hex>"             # optional consumer-side pin on the org policy bytes
  hash_algorithm: sha256           # sha256 (default) | sha384 | sha512
```

| Sub-key | Type | Default | Allowed values | Semantic |
|---|---|---|---|---|
| `fetch_failure_default` | `string` | `warn` | `warn`, `block` | Posture when no enforceable policy is available (fetch failures or no-policy outcomes). `warn` keeps installs unblocked; `block` opts into fail-closed semantics for both `apm install` and `apm audit --ci`. |
| `hash` | `string` | unset | `<algo>:<hex-digest>` | Pin on the raw bytes of the fetched leaf org policy. Verified before YAML parsing; mismatch is always fail-closed regardless of `fetch_failure_default`. |
| `hash_algorithm` | `string` | `sha256` | `sha256`, `sha384`, `sha512` | Digest algorithm for `policy.hash`. Inferred from the `<algo>:` prefix when present. MD5 and SHA-1 are rejected at parse time. |

Full semantics (network failure matrix, hash pin verification, policy precedence) live in the [Policy reference](../enterprise/policy-reference/).

### 3.11. `registries`

::::caution[Experimental]
The `registries:` field and registry-routed APM dependency forms require `apm experimental enable registries`.
::::

| | |
|---|---|
| **Type** | `map<string, RegistryEntry>` with optional `default: <string>` key |
| **Required** | OPTIONAL |
| **Description** | Declares REST-based APM registries for the project. Strictly additive - absent or empty block leaves Git resolution unchanged unless a default registry is configured in `~/.apm/config.json`. URLs from all layers are merged at install time; see the [Registries guide](../guides/registries/#user-level-config). |

```yaml
registries:
  jf-skills:
    url: https://artifactory.example.com/artifactory/api/skills/jf-skills-local
  default: jf-skills           # OPTIONAL - name of one of the configured entries
```

| Sub-key | Type | Required | Constraint | Semantic |
|---|---|---|---|---|
| `<name>` | `RegistryEntry` | at least one when block is non-empty | Name uses lowercase letters, digits, `-`, `.` | Registered registry. |
| `<name>.url` | `string` | REQUIRED per entry | MUST start with `https://` or `http://`; no trailing slash required | Base URL the client appends `/v1/...` paths to. |
| `default` | `string` | OPTIONAL | MUST name one of the configured entries | When set, plain string-shorthand APM deps and object-form deps without an explicit `registry:` key route through this registry. Project value wins over `registry.<name>.default` in `~/.apm/config.json`. |

Unknown keys under a registry entry MUST be rejected at parse time (typo guard).

**Effective default registry:** project `registries.default` if present; otherwise the registry marked `"default": true` in `~/.apm/config.json` (via `apm config set registry.<name>.default true`). Only one default is active at a time.

For full client semantics - auth, lockfile fields, and routing rules - see the [Registries guide](../guides/registries/). For the wire contract servers implement, see the [Registry HTTP API](./registry-http-api/).

---

## 4. Dependencies

| | |
|---|---|
| **Type** | `object` |
| **Required** | OPTIONAL |
| **Known keys** | `apm`, `mcp`, `lsp` |

Contains three OPTIONAL lists: `apm` for agent primitive packages, `mcp` for MCP servers, and `lsp` for LSP servers. Each list entry is either a string shorthand or a typed object. Additional keys MAY be present for future dependency types; conforming resolvers MUST ignore unknown keys for resolution but MUST preserve them when reading and rewriting manifests.

---

### 4.1. `dependencies.apm` -- `list<ApmDependency>`

Each element MUST be one of two forms: **string** or **object**.

#### 4.1.1. String Form

Grammar (ABNF-style):

```
dependency      = url_form / shorthand_form / local_path_form
url_form        = ("https://" / "http://" / "ssh://git@" / "git@") clone-url
shorthand_form  = [host "/"] owner "/" repo ["/" virtual_path] ["#" ref]
local_path_form = ("./" / "../" / "/" / "~/" / ".\\" / "..\\" / "~\\") path
```

When a default registry is configured - via `registries.default` in `apm.yml` or `registry.<name>.default true` in `~/.apm/config.json` - plain `shorthand_form` entries with a `#<selector>` route through that registry instead of Git.

`clone-url` MAY include a `:port` segment on `https://`, `http://`, and `ssh://git@` forms (e.g. `ssh://git@host:7999/owner/repo.git`). The SCP shorthand `git@host:path` cannot carry a port - `:` is the path separator in that form. When a port is present, APM preserves it across all clone attempts: the SSH attempt uses `ssh://host:PORT/...` and the HTTPS fallback uses `https://host:PORT/...` (same port on both protocols).

| Segment | Required | Pattern | Description |
|---|---|---|---|
| `host` | OPTIONAL | FQDN (e.g. `gitlab.com`) | Git host. Defaults to `github.com`. |
| `port` | OPTIONAL | `1`-`65535` | Non-default port on `ssh://`, `https://`, `http://` clone URLs. Not expressible in SCP shorthand. |
| `owner/repo` | REQUIRED | 2+ path segments of `[a-zA-Z0-9._~-]+` on non-Azure-DevOps hosts; `[a-zA-Z0-9._\- ]+` (allows spaces, not tilde) on Azure DevOps | Repository path. GitHub uses exactly 2 segments. Non-GitHub hosts MAY use nested groups (e.g. `gitlab.com/group/sub/repo`). Tilde supports Bitbucket Data Center personal-repo segments (`/scm/~user/repo.git`) and Sourcehut `~user` paths. |
| `virtual_path` | OPTIONAL | Path segments after repo | Subdirectory or file within the repo. See Section 4.1.3. |
| `ref` | OPTIONAL | Branch, tag, or commit SHA | Git reference. Commit SHAs matched by `^[a-f0-9]{7,40}$`. Semver tags matched by `^v?\d+\.\d+\.\d+`. |

**Examples:**

```yaml
dependencies:
  apm:
    # GitHub shorthand (default host); each line shows a syntax variant
    - microsoft/apm-sample-package                # latest (lockfile pins commit SHA)
    - microsoft/apm-sample-package#v1.0.0         # pinned to tag (immutable)
    - microsoft/apm-sample-package#main           # branch ref (may change over time)

    # Non-GitHub hosts (FQDN preserved)
    - gitlab.com/acme/coding-standards
    - bitbucket.org/team/repo#main

    # Full URLs
    - https://github.com/microsoft/apm-sample-package.git
    - http://github.com/microsoft/apm-sample-package.git
    - git@github.com:microsoft/apm-sample-package.git
    - ssh://git@github.com/microsoft/apm-sample-package.git

    # Custom ports (e.g. Bitbucket Datacenter, self-hosted GitLab)
    - ssh://git@bitbucket.example.com:7999/project/repo.git
    - https://git.internal:8443/team/repo.git

    # Virtual packages
    - ComposioHQ/awesome-claude-skills/brand-guidelines   # subdirectory
    - contoso/prompts/review.prompt.md                    # single file

    # Azure DevOps
    - dev.azure.com/org/project/_git/repo

    # Local path (development only)
    - ./packages/my-shared-skills          # relative to project root
    - ../sibling-repo/my-package           # parent directory
```

#### 4.1.2. Object Form

REQUIRED when the shorthand is ambiguous (e.g. direct nested-group repos with virtual paths). NOT required for nested-group deps that route through a registry proxy (explicit `host/artifactory/<key>/...` FQDN, or bare shorthand under `PROXY_REGISTRY_URL` + `PROXY_REGISTRY_ONLY=1`): the install-time boundary probe HEAD-walks candidate splits against the proxy and locks in the first one whose archive responds. See [Registry proxy guide](../enterprise/registry-proxy/#nested-group-repos-gitlab-subgroups-behind-the-proxy).

| Field | Type | Required | Pattern / Constraint | Description |
|---|---|---|---|---|
| `git` | `string` | REQUIRED (remote) | HTTPS URL, SSH URL, or FQDN shorthand | Clone URL of the repository. Required for remote dependencies. |
| `path` | `string` | OPTIONAL / REQUIRED (local) | Relative path within the repo, or local filesystem path | When `git` is present: subdirectory or file (virtual package). When `git` is absent: local filesystem path (must start with `./`, `../`, `/`, or `~/`). |
| `ref` | `string` | OPTIONAL | Branch, tag, or commit SHA | Git reference to checkout. |
| `alias` | `string` | OPTIONAL | `^[a-zA-Z0-9._-]+$` | Local alias. |
| `targets` | `list<string>` | OPTIONAL | Subset of canonical target keys (`copilot`, `claude`, `cursor`, `kiro`, `opencode`, `gemini`, `antigravity`, `codex`, `windsurf`, `agent-skills`, `openclaw`, `hermes`, `copilot-cowork`, `copilot-app`) | Restricts which install targets receive this dependency's target-scoped primitives. Omitted = all active install targets. Effective reach = install targets INTERSECT this list. |

`targets:` on a dependency is intersected with the active install targets
(`--target`, config default, package-level `targets:`, or auto-detect).
Listing a harness the install did not select does not add it; omitting
`targets:` means the dependency reaches every active install target. An empty
list `targets: []` is rejected at parse time -- omit the key to mean
"all".

```yaml
- git: my-team/review-hooks
  targets: [copilot, claude]
```

Remote dependency (git URL plus sub-path):

```yaml
- git: https://gitlab.com/acme/repo.git
  path: instructions/security
  ref: v2.0
  alias: acme-sec
```

`ref:` accepts either a literal git ref (`main`, `v2.0`, a 40-char commit SHA) or a **semver range** (`^1.2.0`, `~1.4`, `>=2.0 <3`, `1.5.x`). When `ref:` is a semver range, APM resolves it against the remote's tags at install time, matching against `v{version}` and `{name}--v{version}` patterns (with `{version}` as a bare-tag fallback) and selecting the highest tag that satisfies the range.

The lockfile records the original `constraint`, the `resolved_tag`, the resolved `version`, the `resolved_commit`, and a `resolved_at` timestamp so subsequent installs replay the same tag deterministically -- only `apm install --update` or a manifest change re-resolves. When no remote tag satisfies the range, APM surfaces `NoMatchingTagError` with the inspected patterns.

Local path dependency (development only):

```yaml
- path: ./packages/my-shared-skills
```

When the declaring package came from a remote repo, `path:` remains scoped to
that same repo only. The resolved path must stay inside the parent's cloned repo
root; APM expands it to the same remote host/repo/ref. Absolute paths, paths
that escape the repo root, and cross-repo local paths are rejected.

Monorepo sibling reference (`git: parent`):

```yaml
# In agents/pkg-a/apm.yml inside org/monorepo
- git: parent
  path: skills/shared
```

The literal sentinel `git: parent` is valid only inside a transitively resolved package whose clone coordinates are known to the resolver. APM expands `parent` to the consumer's `host`, `repo_url`, and resolved `ref`, with `virtual_path` set from `path`. The lockfile records the **expanded** coordinates: `parent` MUST NOT appear as durable identity (`repo_url` / `source`). `path` is REQUIRED for `git: parent` and is normalised to a single relative path; absolute paths and `..` traversal are refused. `ref` and `alias` overrides are accepted; when `ref` is omitted the parent's resolved ref is inherited.

Marketplace dependency (resolved at install time):

```yaml
- name: sec-check
  marketplace: acme-plugins

- name: secrets-vault
  marketplace: acme-plugins
  version: "~2.1.0"
```

| Field | Type | Required | Pattern / Constraint | Description |
|---|---|---|---|---|
| `name` | `string` | REQUIRED | `^[a-zA-Z0-9._-]+$` | Plugin identifier within the marketplace. |
| `marketplace` | `string` | REQUIRED | `^[a-zA-Z0-9._-]+$` | Registered marketplace name. |
| `version` | `string` | OPTIONAL | Semver range or exact version (e.g. `~2.1.0`, `^2.0`, `>=1.4`, `2.1.0`) | Version constraint resolved against git tags on the marketplace repository. When omitted the marketplace entry's default ref is used. |

The `marketplace` key is mutually exclusive with `git`, `path`, `registry`, and `id`; combining them raises a parse error. Unknown keys in a marketplace entry are rejected. During dependency resolution the resolver calls `resolve_marketplace_plugin()` to look up the plugin in the marketplace's `marketplace.json` and replace the entry with a concrete git reference (owner/repo, ref, and optional virtual path).

When `version` is specified and is a semver range or bare version number (e.g. `~2.1.0`, `^2.0`, `2.1.0`), the resolver lists git tags on the marketplace repository matching the `{name}--v{version}` convention, filters to those satisfying the constraint, and resolves to the highest matching tag. If no tag satisfies an explicit semver range, resolution fails with a `NoMatchingVersionError`. A bare version with no matching tag falls back to using the value as a raw git ref. Pre-release versions (e.g. `2.0.0-beta.1`) are excluded from semver-range resolution; target them explicitly as raw git refs. When `version` is a raw git ref (e.g. `v2.0.0`, `main`, or a commit SHA), it is used as a direct ref override without tag resolution.

Resolution failures stop the install instead of silently skipping the dependency. The lockfile records the **resolved** coordinates and pinned commit, not the marketplace placeholder. Unresolved marketplace dependencies cannot compute install paths or serialize back to `apm.yml`.

Registry dependency (whole package or virtual sub-path):

```yaml
# Whole package via the default registry
- id: acme/toolkit
  version: ^2.0.0

# Whole package routed to a named registry
- registry: jf-skills              # OPTIONAL - defaults to the effective default registry
  id: acme/toolkit                 # REQUIRED - owner/repo identity at the registry
  version: ^2.0.0                  # REQUIRED - opaque version selector (semver when supported)

# Virtual package (sub-path inside a published package)
- registry: jf-skills
  id: acme/prompt-pack
  path: prompts/review.prompt.md   # OPTIONAL - omit to install the whole package
  version: 1.4.0
  alias: review                    # OPTIONAL
```

`id:` (or `registry:`) and `git:` are mutually exclusive on the same entry. `version:` MUST be a non-empty string - opaque selectors such as `stable`, `main`, or commit pins are valid; semver ranges (`^1.2.3`) are interpreted as ranges when the registry publishes semver-tagged versions. When `registry:` is omitted, a default registry MUST be configured - in project `apm.yml` or via `registry.<name>.default true` in `~/.apm/config.json`; APM hard-fails otherwise.

#### 4.1.3. Virtual Packages

A dependency MAY target a subdirectory or a file within a repository rather than the whole repo. Conforming resolvers MUST classify virtual packages using the following rules, evaluated in order:

| Kind | Detection rule | Example |
|---|---|---|
| **File** | `virtual_path` ends in `.prompt.md`, `.instructions.md`, or `.agent.md` | `owner/repo/prompts/review.prompt.md` |
| **Subdirectory** | `virtual_path` does not match any file extension above | `owner/repo/skills/security` |

Classification is by extension only, never by path segment. A path like `owner/repo/collections/security` (no extension) is a **Subdirectory**: the on-disk shape (APM package with `apm.yml`, skill bundle, or plugin) is resolved at fetch time by probing for `apm.yml` first.

> **Removed (#1094):** the legacy `.collection.yml` / `.collection.yaml` virtual-package form is no longer supported. Convert any such reference to an `apm.yml` with a `dependencies:` section, then reference the resulting subdirectory as a regular subdirectory virtual package.

#### 4.1.4. Canonical Normalisation

Conforming writers MUST normalise entries to canonical form on write. `github.com` is the default host and MUST be stripped; all other hosts MUST be preserved as FQDN.

| Input | Canonical form |
|---|---|
| `https://github.com/microsoft/apm-sample-package.git` | `microsoft/apm-sample-package` |
| `git@github.com:microsoft/apm-sample-package.git` | `microsoft/apm-sample-package` |
| `gitlab.com/acme/repo` | `gitlab.com/acme/repo` |

---

### 4.2. `dependencies.mcp` -- `list<McpDependency>`

Each element MUST be one of two forms: **string** or **object**.

#### 4.2.1. String Form

A plain registry reference: `io.github.github/github-mcp-server`.

#### 4.2.2. Object Form

| Field | Type | Required | Constraint | Description |
|---|---|---|---|---|
| `name` | `string` | REQUIRED | Non-empty | Server identifier (registry name or custom name). |
| `transport` | `enum<string>` | Conditional | `stdio`, `sse`, `http`, `streamable-http` | Transport protocol. REQUIRED when `registry: false`. Values are MCP transport names, not URL schemes; remote variants connect over HTTPS. |
| `env` | `map<string, string>` | OPTIONAL | | Environment variable overrides. Values may contain `${VAR}`, `${env:VAR}`, or `${input:<id>}` references; see Section 4.2.4. |
| `args` | `dict` or `list` | OPTIONAL | | Dict for overlay variable overrides (registry); list for positional args (self-defined). |
| `version` | `string` | OPTIONAL | | Pin to a specific server version. |
| `registry` | `bool` or `string` | OPTIONAL | Default: `true` (public registry) | `false` = self-defined (private) server. String = custom registry URL. |
| `package` | `enum<string>` | OPTIONAL | `npm`, `pypi`, `oci` | Package manager type hint. |
| `headers` | `map<string, string>` | OPTIONAL | | Custom HTTP headers for remote endpoints. Same variable syntax as `env`. |
| `tools` | `list<string>` | OPTIONAL | Default: `["*"]` | Restrict which tools are exposed. |
| `url` | `string` | Conditional | | Endpoint URL. REQUIRED when `registry: false` and `transport` is `http`, `sse`, or `streamable-http`. |
| `command` | `string` | Conditional | Single binary path; no embedded whitespace unless `args` is also present | Binary path. REQUIRED when `registry: false` and `transport` is `stdio`. |

Any additional keys not listed above are preserved as **extra passthrough fields** and round-tripped verbatim into the generated target manifests. This allows harness-specific configuration (e.g. Claude Code's `oauth` block for remote-MCP OAuth client config) to be declared in `apm.yml` and appear in the generated config without modification. A warning is emitted at parse time naming each non-standard key.

Two guardrails apply:

- **Reserved keys are rejected.** A passthrough key whose name collides with a modeled field above -- `name`, `transport`/`type`, `command`, `url`, `headers`, `env`, `args`, `tools`, `version`, `registry`, `package` (and the Codex `http_headers` alias) -- is dropped with a warning. This prevents a passthrough value from shadowing or redirecting a modeled field. Extra keys also never overwrite a value the target adapter set itself.
- **Extra keys broadcast to every target.** Passthrough keys are written uniformly into the generated config for **all** installed harnesses, not just the one that understands them. A Claude Code `oauth` block (`clientId`/`callbackPort`), for example, is emitted into every target's server entry; harnesses that do not recognise the key ignore it. Per-harness scoping is tracked as a future enhancement (see issue #1806).

> A future release may require passthrough keys to be nested under an explicit `extra:` block and stop auto-capturing bare top-level keys (fail-closed), via a deprecation path. See issue #1806.

#### 4.2.3. Validation Rules for Self-Defined Servers

When `registry` is `false`, the following constraints apply:

1. `transport` MUST be present.
2. If `transport` is `stdio`, `command` MUST be present.
3. If `transport` is `http`, `sse`, or `streamable-http`, `url` MUST be present.
4. If `transport` is `stdio`, `command` MUST be a single binary path with no embedded whitespace. APM does not split `command` on whitespace; use `args` for additional arguments. A path that legitimately contains spaces (e.g. `/opt/My App/server`) is allowed when `args` is also provided (including an explicit empty list `args: []`), signaling the author has taken responsibility for the shape.

```yaml
dependencies:
  mcp:
    # Registry reference (string)
    - io.github.github/github-mcp-server

    # Registry with overlays (object)
    - name: io.github.github/github-mcp-server
      tools: ["repos", "issues"]
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

    # Self-defined server (object, registry: false)
    - name: my-private-server
      registry: false
      transport: stdio
      command: ./bin/my-server
      args: ["--port", "3000"]
      env:
        API_KEY: ${{ secrets.KEY }}

    # Self-defined remote server with harness-specific extra keys
    - name: slack
      registry: false
      transport: http
      url: https://mcp.slack.com/mcp
      oauth:
        clientId: "<pre-registered-client-id>"
        callbackPort: 3118
```

#### 4.2.4. Variable References in `headers` and `env`

Values in `headers` and `env` may contain three placeholder syntaxes. APM resolves them per-target so secrets stay out of generated config files where possible.

| Syntax | Source | VS Code | JetBrains Copilot | Copilot CLI / Kiro | Codex / Gemini / Cursor |
|---|---|---|---|---|---|
| `${VAR}` | host environment | Translated to `${env:VAR}` (resolved at server-start by VS Code) | Translated to `${env:VAR}` | Native; passed through verbatim | Resolved at install time from env (or interactive prompt) |
| `${env:VAR}` | host environment | Native; passed through verbatim | Native; passed through verbatim | Translated to `${VAR}` | Resolved at install time from env (or interactive prompt) |
| `${input:<id>}` | user prompt | Native; VS Code prompts at runtime | Not supported; use `${VAR}` or `${env:VAR}` instead | Not supported; use `${VAR}` or `${env:VAR}` instead | Not supported; use `${VAR}` or `${env:VAR}` instead |
| `<VAR>` (legacy) | host environment | Not recognized | Translated to `${env:VAR}` | Translated to `${VAR}` | Resolved at install time (kept for back-compat) |

- **VS Code** has native `${env:VAR}` and `${input:VAR}` interpolation, so APM emits placeholders rather than baking secrets into `mcp.json`. Bare `${VAR}` is normalized to `${env:VAR}` for you.
- **JetBrains Copilot** has native `${env:VAR}` interpolation in `mcp.json`; APM normalizes `${VAR}` and legacy `<VAR>` to `${env:VAR}`.
- **Copilot CLI and Kiro** have native `${VAR}` interpolation in their MCP config files; APM normalizes `${env:VAR}` and legacy `<VAR>` to `${VAR}`.
- **Codex, Gemini, and Cursor** have no runtime interpolation, so APM resolves `${VAR}`, `${env:VAR}`, and the legacy `<VAR>` at install time using `os.environ` (or an interactive prompt when missing). Resolved values are not re-scanned, so a value containing literal `${...}` text is preserved.
- **Recommended:** Use `${VAR}` or `${env:VAR}` in all new manifests - they work on every target that supports remote MCP servers. `<VAR>` is legacy; in VS Code it would silently render as literal text in the generated config.
- **Registry-backed servers** - APM auto-generates input prompts from registry metadata only for required variables. Optional variables do not generate prompts or runtime config entries when no value is available. If a user has already edited an optional value in runtime config, reinstall preserves that value rather than overwriting it.
- **Self-defined servers** - APM detects `${input:...}` patterns in `apm.yml` and generates matching input definitions automatically.

GitHub Actions templates (`${{ ... }}`) are intentionally left untouched.

```yaml
dependencies:
  mcp:
    - name: my-server
      registry: false
      transport: http
      url: https://my-server.example.com/mcp/
      headers:
        Authorization: "Bearer ${MY_SECRET_TOKEN}"      # bare env-var
        X-Tenant: "${env:TENANT_ID}"                    # env-prefixed
        X-Project: "${input:my-server-project}"         # VS Code input prompt
```

---

### 4.3. `dependencies.lsp` -- `list<LspDependency>`

LSP (Language Server Protocol) server dependencies give supported runtimes real-time code intelligence -- diagnostics, go-to-definition, and type information. APM currently writes LSP config for Claude Code and GitHub Copilot CLI while keeping this manifest schema runtime-neutral.

Each element MUST be one of two forms: **string** or **object**.

#### 4.3.1. String Form

A plain server name reference: `gopls`. String-form entries carry only a name and are resolved from transitive packages or plugin `.lsp.json` files. They bypass strict validation (no `command` or `extensionToLanguage` required).

#### 4.3.2. Object Form

| Field | Type | Required | Constraint | Description |
|---|---|---|---|---|
| `name` | `string` | REQUIRED | `^[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}$`; no `..` segments | Server identifier. |
| `command` | `string` | REQUIRED | No `..` path segments | Binary to execute (must be on `$PATH` or a relative path). |
| `extensionToLanguage` | `map<string, string>` | REQUIRED | Non-empty dict | Maps file extensions to LSP language identifiers (e.g. `".go": "go"`). |
| `args` | `list<string>` | OPTIONAL | | Command-line arguments for the server. |
| `transport` | `enum<string>` | OPTIONAL | `stdio`, `socket` | Communication transport. Defaults to `stdio`. |
| `env` | `map<string, string>` | OPTIONAL | | Environment variables set when starting the server. |
| `initializationOptions` | `any` | OPTIONAL | | Options passed to the server during LSP initialization. |
| `settings` | `any` | OPTIONAL | | Settings passed via `workspace/didChangeConfiguration`. |
| `workspaceFolder` | `string` | OPTIONAL | | Workspace folder path for the server. |
| `startupTimeout` | `int` | OPTIONAL | | Max time (ms) to wait for server startup. |
| `shutdownTimeout` | `int` | OPTIONAL | | Max time (ms) to wait for graceful shutdown. |
| `restartOnCrash` | `bool` | OPTIONAL | | Whether to automatically restart on crash. |
| `maxRestarts` | `int` | OPTIONAL | | Maximum restart attempts before giving up. |

Both `command` and `extensionToLanguage` are REQUIRED in the object form. A missing or empty value for either is a validation error.

#### 4.3.3. Validation Rules

1. `name` MUST match the pattern above and MUST NOT contain `..` path segments.
2. `command` MUST be a string and MUST NOT contain `..` path traversal.
3. `transport`, when present, MUST be `stdio` or `socket`.
4. `extensionToLanguage` MUST be a non-empty dict mapping string keys to string values.

Manifest keys use camelCase (`extensionToLanguage`, `initializationOptions`, `workspaceFolder`, `startupTimeout`, `shutdownTimeout`, `restartOnCrash`, `maxRestarts`). Snake_case aliases are accepted on input for ergonomics but camelCase is canonical.

```yaml
dependencies:
  lsp:
    # String form
    - gopls

    # Object form
    - name: pyright
      command: pyright-langserver
      args: ["--stdio"]
      extensionToLanguage:
        ".py": python
        ".pyi": python
      transport: stdio
      env:
        PYTHONPATH: "./src"
      startupTimeout: 10000

    - name: rust-analyzer
      command: rust-analyzer
      extensionToLanguage:
        ".rs": rust
      restartOnCrash: true
      maxRestarts: 3
```

#### 4.3.4. What Gets Written

`apm install` writes LSP server configs to detected runtime targets. Claude Code uses `.lsp.json` at project scope or `~/.claude.json` at user scope. GitHub Copilot CLI uses `.github/lsp.json` at project scope or `~/.copilot/lsp-config.json` at user scope. See [Install LSP servers](../consumer/install-lsp-servers/) for output formats and lifecycle details.

---

## 5. devDependencies

| | |
|---|---|
| **Type** | `object` |
| **Required** | OPTIONAL |
| **Known keys** | `apm`, `mcp`, `lsp` |

Development-only dependencies installed locally but excluded from plugin bundles produced by [`apm pack`](./cli/pack/) (plugin format is the default). Uses the same structure as [`dependencies`](#4-dependencies).

```yaml
devDependencies:
  apm:
    - owner/test-helpers
    - owner/lint-rules#v2.0.0
```

Created automatically by [`apm plugin init`](./cli/plugin/). Use [`apm install --dev`](./cli/install/) to add packages:

```bash
apm install --dev owner/test-helpers
```

Plain `apm install` (no flag) deploys both `dependencies` and `devDependencies`. There is no `--omit=dev` flag today; the dev/prod separation kicks in at `apm pack` (plugin format, the default). The local-content scanner that builds plugin bundles operates on `.apm/` only and does not consult the devDep marker. To keep maintainer-only primitives out of shipped artifacts, author them outside `.apm/` and reference them via a local-path devDependency. See [Dev-only Primitives](../concepts/primitives-and-targets/#dev-only-primitives).

Local-path devDependency example:

```yaml
devDependencies:
  apm:
    - path: ./dev/skills/release-checklist
```

---

## 6. Compilation

The `compilation` key is OPTIONAL. It controls [`apm compile`](./cli/compile/) behaviour. All fields have sensible defaults; omitting the entire section is valid.

| Field | Type | Default | Constraint | Description |
|---|---|---|---|---|
| `target` | `enum<string>` | `all` | Same values as Section 3.6 | Output target. Defaults to `all` when set explicitly in compilation config. |
| `strategy` | `enum<string>` | `distributed` | `distributed`, `single-file` | `distributed` generates per-directory target files (e.g. `AGENTS.md`, `CLAUDE.md`). `single-file` generates one monolithic file at `output`. |
| `single_file` | `bool` | `false` | | Legacy alias. When `true`, overrides `strategy` to `single-file`. |
| `output` | `string` | `AGENTS.md` | File path | Custom output path for the compiled file. |
| `chatmode` | `string` | unset | | Chatmode filter for compilation. |
| `resolve_links` | `bool` | `true` | | Resolve relative Markdown links in primitives. |
| `source_attribution` | `bool` | `false` | | Include source-file origin comments in compiled output (opt-in). |
| `exclude` | `list<string>` or `string` | `[]` | Glob patterns | Directories to skip during compilation (e.g. `apm_modules/**`). |
| `placement` | `object` | unset | | Placement tuning. See Section 6.1. |
| `agents_md` | `object` | unset | | AGENTS.md output tuning. See Section 6.2. |

### 6.1. `compilation.placement`

| Field | Type | Default | Description |
|---|---|---|---|
| `min_instructions_per_file` | `int` | `1` | Minimum instruction count to warrant a separate `AGENTS.md` file. |

```yaml
compilation:
  target: all
  strategy: distributed
  source_attribution: true
  exclude:
    - "apm_modules/**"
    - "tmp/**"
  placement:
    min_instructions_per_file: 1
```

### 6.2. `compilation.agents_md`

Controls how `apm compile` writes the root `AGENTS.md` output file. All fields are OPTIONAL; omitting the entire sub-object keeps the default full-overwrite behaviour. Use `managed_section` mode when your root `AGENTS.md` contains hand-written content you want to preserve across recompiles. In distributed compile mode, subdirectory `AGENTS.md` files remain fully APM-owned and are overwritten on each run.

| Field | Type | Default | Constraint | Description |
|---|---|---|---|---|
| `mode` | `enum<string>` | `full` | `full`, `managed_section` | `full` overwrites the entire file on every compile. `managed_section` replaces only the root `AGENTS.md` block between `start_marker` and `end_marker`, leaving surrounding content untouched. |
| `start_marker` | `string` | `<!-- apm:start -->` | Non-empty, distinct from `end_marker` | Opening HTML comment that delimits the APM-managed block. Required in the output file when `mode: managed_section`. |
| `end_marker` | `string` | `<!-- apm:end -->` | Non-empty, distinct from `start_marker` | Closing HTML comment that delimits the APM-managed block. Required in the output file when `mode: managed_section`. |

Both markers must appear **exactly once** in the file; a missing or duplicate marker raises `ManagedSectionError` rather than silently overwriting content.

See [Managed-section mode](../producer/compile/#managed-section-mode) in the compile guide for usage and marker setup instructions.

```yaml
compilation:
  agents_md:
    mode: managed_section
    start_marker: "<!-- apm:start -->"
    end_marker: "<!-- apm:end -->"
```

---

## 7. Marketplace Authoring Block

The OPTIONAL `marketplace:` block declares the metadata `apm pack` needs to emit a Claude-Code-compatible plugin marketplace (`marketplace.json`). It is read by `apm marketplace` subcommands and ignored by everything else. Repositories that do not publish a marketplace omit it entirely.

The block was previously a standalone `marketplace.yml` file (still loadable for back-compat); the in-`apm.yml` form is canonical and is what [`apm marketplace init`](./cli/marketplace/) scaffolds.

### 7.1. Inheritance

Three keys are inherited from the top-level manifest unless explicitly overridden inside `marketplace:`:

| Key | Inherited from |
|---|---|
| `name` | top-level `name` |
| `description` | top-level `description` |
| `version` | top-level `version` |

Overrides exist for the rare case where the published marketplace identity differs from the package identity. Inherited values are omitted from the generated `marketplace.json` per the Claude-Code convention.

### 7.2. Block Fields

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | OPTIONAL (override) | inherited | Override of top-level `name`. Normalised to kebab-case (lowercase, non-alphanumeric -> hyphen) in the compiled `marketplace.json` for Copilot App compatibility; the raw value is retained internally and, for Codex, as `interface.displayName`. |
| `description` | `string` | OPTIONAL (override) | inherited | Override of top-level `description`. |
| `version` | `string` | OPTIONAL (override) | inherited | Override of top-level `version`. Validated as semver. |
| `owner` | `Owner` | REQUIRED | -- | Marketplace publisher identity. See Section 7.3. |
| `sourceBase` | `string` | OPTIONAL | unset | HTTPS git base that relative `packages[].source` values compose onto. See Section 7.5. |
| `output` | `string` | OPTIONAL | `.claude-plugin/marketplace.json` | Output path for the generated marketplace JSON. |
| `metadata` | `object` | OPTIONAL | `{}` | Free-form metadata forwarded verbatim to `marketplace.json` (e.g. `homepage`, `support`). |
| `build` | `Build` | OPTIONAL | `tagPattern: "v{version}"` | Build configuration for resolving package refs. See Section 7.4. |
| `packages` | `list<Package>` | OPTIONAL | `[]` | Packages exposed in the marketplace. See Section 7.5. |

Unknown keys inside `marketplace:` are rejected at parse time.

### 7.3. `marketplace.owner`

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `string` | REQUIRED | Owner display name (org or person). |
| `email` | `string` | OPTIONAL | Contact email. |
| `url` | `string` | OPTIONAL | Owner homepage. |

```yaml
marketplace:
  owner:
    name: contoso
    url:  https://github.com/contoso
    email: maintainers@contoso.example
```

### 7.4. `marketplace.build`

| Field | Type | Default | Description |
|---|---|---|---|
| `tagPattern` | `string` | `v{version}` | Pattern used to construct git tags for packages. MUST contain at least one of `{version}` or `{name}`. Per-package overrides live on `packages[].tag_pattern`. |

### 7.5. `marketplace.packages`

Each entry MUST be a mapping. Unknown keys are rejected.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `string` | REQUIRED | Package identifier as it appears in the marketplace. |
| `source` | `string` | REQUIRED | One of: `<owner>/<repo>` (remote on the default host), `<host.tld>/<owner>/<repo>` (remote on a non-default host such as GitHub Enterprise or self-hosted GitLab -- shorthand), `https://<host.tld>/<owner>/<repo>[.git]` (same, full URL form -- a trailing `.git` is stripped), `./<path>` (local), or a relative path when `marketplace.sourceBase` is set. Must match the source pattern; path traversal (`..`) is refused, and URL forms with userinfo (`user@host`), ports, query strings, or non-`https` schemes are rejected. |
| `subdir` | `string` | OPTIONAL | Subdirectory inside the source repo. Path-traversal-validated. Ignored for local sources. |
| `version` | `string` | Conditional | Semver range (e.g. `^1.0.0`, `~2.1.0`, `>=3.0`). Stored as a string; resolution happens at pack time. REQUIRED for remote packages unless `ref` is given; when omitted in that case, the displayed version can fall back to the package's own `apm.yml` (see note below). |
| `ref` | `string` | Conditional | Explicit git ref (SHA, tag, or branch). Overrides `version` range when both are present. REQUIRED for remote packages unless `version` is given. |
| `tag_pattern` | `string` | OPTIONAL | Per-package override of `build.tagPattern`. Same placeholder rule. |
| `include_prerelease` | `bool` | `false` | Whether semver pre-release tags are eligible for resolution. |
| `description` | `string` | OPTIONAL | Pass-through to `marketplace.json`; falls back to the package's own `apm.yml` when omitted (see note below). |
| `homepage` | `string` | OPTIONAL | Pass-through to `marketplace.json`. |
| `tags` | `list<string>` | OPTIONAL | Pass-through to `marketplace.json`. Limited to 50 tags, 100 chars each. |
| `keywords` | `list<string>` | OPTIONAL | Alias merged into `tags` (deduplicated). |
| `author` | `string` or `object` | OPTIONAL | Either a non-empty string (treated as `name`) or an object with `name` (REQUIRED), `email`, `url`. |
| `license` | `string` | OPTIONAL | Pass-through (SPDX identifier). |
| `repository` | `string` | OPTIONAL | Pass-through. |

Remote packages MUST declare at least one of `version` or `ref`. Local packages (sources beginning with `./`) skip git resolution and have no version requirement.

When `description` is omitted, or when a remote entry has no displayable `version`, `apm pack` reads the matching field from the referenced package's own `apm.yml` and uses it in the generated `marketplace.json`. Remote GitHub-class packages (`github.com`, GHES, or authenticated GHE Cloud) are fetched over HTTPS (skipped under `--offline` and for other hosts); local packages are read from disk under the project root. A `description` or display `version` set on the `packages[]` entry still wins. For remote packages, semver ranges such as `^1.0.0` are used for resolution, not emitted as the displayed version, so the package `apm.yml` version is emitted when available.

The first three `source` forms target a remote git host; the second and third name a non-default host (e.g. GitHub Enterprise, self-hosted GitLab) as either a shorthand or a full HTTPS URL with an optional `.git` suffix that is normalized away. Path traversal (`..`) in local paths, userinfo (`user@host`), ports, query strings, and non-`https` URL schemes are rejected at parse time.

When `sourceBase` is set, relative package sources compose onto that base. For example, `sourceBase: https://gitlab.corp.example.com/platform/agent-marketplace` plus `source: review` emits `https://gitlab.corp.example.com/platform/agent-marketplace/review`. This includes two-segment `owner/repo` values and deeper relative paths; only host-prefixed sources, full HTTPS URLs, and local `./` sources are overrides that ignore `sourceBase`. Without `sourceBase`, existing `owner/repo` behavior is unchanged and single-segment relative sources are rejected.

`sourceBase` works with any supported host. An Azure DevOps base such as `sourceBase: https://dev.azure.com/contoso/platform/_git` plus `source: agent-skills` emits `https://dev.azure.com/contoso/platform/_git/agent-skills`; the `dev.azure.com` host is preserved through to the consumer, which resolves it as an Azure DevOps dependency rather than rewriting it onto the default host.

A relative `source` may use arbitrary path depth. A value whose leading segments form a host-prefixed shape (`<host.tld>/<owner>/<repo>`) or a full `https://` URL is always treated as a per-entry override and ignores `sourceBase`. A value that looks like it is trying to name a host (a dotted, FQDN-like first segment) but does **not** form a valid override shape is rejected at parse time rather than silently composed onto the base -- this avoids a confused-deputy footgun. To target a different host, use an explicit host-prefixed override or a full `https://` URL instead of a relative source.

`sourceBase` must start with `https://`, use a FQDN host, include at least one path segment, and omit userinfo, ports, query strings, fragments, and a trailing `.git`. Each path segment uses letters, digits, `.`, `_`, or `-`; empty, `.` and `..` segments are refused.

Non-default hosts -- GitHub Enterprise, self-hosted GitLab, and Azure DevOps -- authenticate via the standard APM token chain -- see the [authentication guide](../getting-started/authentication/) for the per-host-class lookup order. A token resolved for the default host is never forwarded to a non-default host (an Azure DevOps `ADO_APM_PAT`, for example, is only ever offered to `dev.azure.com`).

### 7.6. Complete Marketplace Block

```yaml
marketplace:
  # name, description, version inherit from top-level apm.yml
  owner:
    name: contoso
    url:  https://github.com/contoso

  # Optional: packages can name repos relative to this git base.
  sourceBase: https://gitlab.corp.example.com/platform/agent-marketplace

  output: .claude-plugin/marketplace.json

  metadata:
    homepage: https://contoso.example/marketplace

  build:
    tagPattern: "v{version}"

  packages:
    - name: code-review
      source: code-review                    # resolves under sourceBase
      version: "^1.0.0"
      description: AI code-review skills
      tags: [review, quality]

    - name: pinned-helper
      source: contoso/pinned-helper          # also resolves under sourceBase
      ref: main                              # explicit ref overrides version
      tag_pattern: "pinned-helper-v{version}"

    - name: local-tool                       # local-path package
      source: ./packages/local-tool
      description: Vendored tool

    - name: enterprise-agents                # GHE shorthand
      source: ghe.corp.example.com/platform/agents
      version: "^0.3.0"

    - name: gitlab-helper                    # full URL form
      source: https://gitlab.corp.example.com/team/helper.git
      ref: v1.2.0
```

The legacy standalone `marketplace.yml` (top-level keys, no `marketplace:` wrapper) is still loadable but deprecated; new repositories SHOULD use the in-`apm.yml` form scaffolded by `apm marketplace init`.

---

## 8. Lockfile (`apm.lock.yaml`)

After successful dependency resolution, a conforming resolver MUST write a lockfile capturing the exact resolved state. The lockfile MUST be a YAML file named `apm.lock.yaml` at the project root and SHOULD be committed to version control.

The full lockfile schema is specified in the [Lockfile specification](./lockfile-spec/). At a minimum, every resolver MUST record `lockfile_version`, `dependencies[].repo_url`, `dependencies[].resolved_commit`, and `dependencies[].deployed_files` so subsequent installs are reproducible and `apm uninstall` can remove every placed file.

Resolver behaviour:

1. **First install** -- Resolve all dependencies, write `apm.lock.yaml`.
2. **Subsequent installs** -- Read `apm.lock.yaml`, use locked commit SHAs. A resolver SHOULD skip download when the local checkout already matches.
3. **`--update` flag** -- Re-resolve from `apm.yml`, overwrite the lockfile.

---

## 9. Integrator Contract

Any runtime adopting this format (e.g. GitHub Agentic Workflows, CI systems, IDEs) MUST implement these steps:

1. **Parse** -- Read `apm.yml` as YAML. Validate the two REQUIRED fields (`name`, `version`) and the `dependencies` object shape.
2. **Resolve `dependencies.apm`** -- For each entry, clone or fetch the git repo (respecting `ref`), locate the `.apm/` directory (or virtual path), and extract primitives.
3. **Resolve `dependencies.mcp`** -- For each entry, resolve from the MCP registry or validate self-defined transport config per Section 4.2.3.
4. **Transitive resolution** -- Resolved packages MAY contain their own `apm.yml` with further dependencies, forming a dependency tree. Resolvers MUST resolve transitively. Conflicts are merged at instruction level (by `applyTo` pattern), not file level.
5. **Write lockfile** -- Record exact commit SHAs and deployed file paths in `apm.lock.yaml` per Section 8 and the [Lockfile specification](./lockfile-spec/).

---

## Appendix A. Complete Example

```yaml
name: my-project
version: 1.0.0
description: AI-native web application
author: Contoso
license: MIT
target: [claude, copilot]
type: hybrid
includes: auto

scripts:
  start:  "copilot -p 'README.prompt.md'"
  review: "copilot -p 'code-review.prompt.md'"
  impl:   "copilot -p 'implement-feature.prompt.md'"

dependencies:
  apm:
    - microsoft/apm-sample-package#v1.0.0
    - gitlab.com/acme/coding-standards#main
    - git: https://gitlab.com/acme/repo.git
      path: instructions/security
      ref: v2.0
  mcp:
    - io.github.github/github-mcp-server
    - name: my-private-server
      registry: false
      transport: stdio
      command: ./bin/my-server
      env:
        API_KEY: ${{ secrets.KEY }}
  lsp:
    - name: pyright
      command: pyright-langserver
      args: ["--stdio"]
      extensionToLanguage:
        ".py": python
        ".pyi": python

devDependencies:
  apm:
    - owner/test-helpers

compilation:
  target: all
  strategy: distributed
  exclude:
    - "apm_modules/**"
  placement:
    min_instructions_per_file: 1

policy:
  fetch_failure_default: warn

marketplace:
  owner:
    name: contoso
    url:  https://github.com/contoso
  packages:
    - name: code-review
      source: contoso/code-review
      version: "^1.0.0"
      tags: [review, quality]
```

---

## Appendix B. Revision History

| Version | Date | Changes |
|---|---|---|
| 0.1 | 2026-03-06 | Initial Working Draft. |
| 0.2 | 2026-05-10 | Added Section 7 (Marketplace authoring block). Documented `scripts.start` as the default `apm run` entry point. Cross-links updated to reference CLI paths. ASCII-only enforcement. |
| 0.3 | 2026-05-20 | Added Section 4.3 (`dependencies.lsp`). LSP servers as a third dependency kind. Updated document structure, devDependencies known keys, and Appendix A. |
| 0.4 | 2026-06-11 | Added `marketplace.sourceBase` and Section 7.5 source composition semantics. |
