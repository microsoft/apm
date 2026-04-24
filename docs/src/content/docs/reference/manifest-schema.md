---
title: "Manifest Schema"
sidebar:
  order: 2
---

<dl>
<dt>Version</dt><dd>0.1 (Working Draft)</dd>
<dt>Date</dt><dd>2026-03-06</dd>
<dt>Editors</dt><dd>Daniel Meppiel (Microsoft)</dd>
<dt>Repository</dt><dd>https://github.com/microsoft/apm</dd>
<dt>Format</dt><dd>YAML 1.2</dd>
</dl>

## Status of This Document

This is a **Working Draft**. It may be updated, replaced, or made obsolete at any time. It is inappropriate to cite this document as other than work in progress.

This specification defines the manifest format (`apm.yml`) used by the Agent Package Manager (APM). Feedback is welcome via [GitHub Issues](https://github.com/microsoft/apm/issues).

---

## Abstract

The `apm.yml` manifest declares the full closure of agent primitive dependencies, MCP servers, scripts, and compilation settings for a project. It is the contract between package authors, runtimes, and integrators — any conforming resolver can consume this format to install, compile, and run agentic workflows.

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
target:        <enum>
type:          <enum>
scripts:       <map<string, string>>
includes:      <enum | list<string>>
dependencies:
  apm:         <list<ApmDependency>>
  mcp:         <list<McpDependency>>
devDependencies:
  apm:         <list<ApmDependency>>
  mcp:         <list<McpDependency>>
compilation:   <CompilationConfig>
policy:        <PolicyConfig>
```

---

## 3. Top-Level Fields

### 3.1. `name`

| | |
|---|---|
| **Type** | `string` |
| **Required** | MUST be present |
| **Description** | Package identifier. Free-form string (no pattern enforced at parse time). Convention: alphanumeric, dots, hyphens, underscores. |

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
| **Description** | Brief human-readable description. |

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
| **Description** | SPDX license identifier (e.g. `MIT`, `Apache-2.0`). |

### 3.6. `target`

| | |
|---|---|
| **Type** | `string \| list<string>` |
| **Required** | OPTIONAL |
| **Default** | Auto-detect: `vscode` if `.github/` exists, `claude` if `.claude/` exists, `codex` if `.codex/` exists, `all` if multiple target folders exist, `minimal` if none |
| **Allowed values** | `vscode` · `agents` · `copilot` · `claude` · `cursor` · `opencode` · `codex` · `all` |

Controls which output targets are generated during compilation and installation. Accepts a single string or a list of strings. When unset, a conforming resolver SHOULD auto-detect based on folder presence. Unknown values MUST be silently ignored (auto-detection takes over).

```yaml
# Single target
target: copilot

# Multiple targets
target: [claude, copilot]
```

When a list is specified, only those targets are compiled, installed, and packed -- no output is generated for unlisted targets. `all` cannot be combined with other values.

| Value | Effect |
|---|---|
| `vscode` | Emits `AGENTS.md` at the project root (and per-directory files in distributed mode) |
| `agents` | Alias for `vscode` |
| `copilot` | Alias for `vscode` |
| `claude` | Emits `CLAUDE.md` at the project root |
| `cursor` | Emits to `.cursor/rules/`, `.cursor/agents/`, `.cursor/skills/` |
| `opencode` | Emits to `.opencode/agents/`, `.opencode/commands/`, `.opencode/skills/` |
| `codex` | Emits `AGENTS.md` and deploys skills to `.agents/skills/`, agents to `.codex/agents/` |
| `all` | All targets. Cannot be combined with other values in a list. |
| `minimal` | AGENTS.md only at project root. **Auto-detected only** -- this value MUST NOT be set explicitly in manifests; it is an internal fallback when no target folder is detected. |

### 3.7. `type`

| | |
|---|---|
| **Type** | `enum<string>` |
| **Required** | OPTIONAL |
| **Default** | None (behaviour driven by package content; synthesized plugin manifests use `hybrid`) |
| **Allowed values** | `instructions` · `skill` · `hybrid` · `prompts` |

Declares how the package's content is processed during install and compile. Currently behaviour is driven by package content (presence of `SKILL.md`, component directories, etc.); this field is reserved for future explicit overrides.

| Value | Behaviour |
|---|---|
| `instructions` | Compiled into AGENTS.md only. No skill directory created. |
| `skill` | Installed as a native skill only. No AGENTS.md output. |
| `hybrid` | Both AGENTS.md compilation and skill installation. |
| `prompts` | Commands/prompts only. No instructions or skills. |

### 3.8. `scripts`

| | |
|---|---|
| **Type** | `map<string, string>` |
| **Required** | OPTIONAL |
| **Key pattern** | Script name (free-form string) |
| **Value** | Shell command string |
| **Description** | Named commands executed via `apm run <name>`. MUST support `--param key=value` substitution. |

### 3.9. `includes`

| | |
|---|---|
| **Type** | `string` (literal `auto`) `\| list<string>` |
| **Required** | OPTIONAL |
| **Default** | Undeclared (legacy implicit auto-publish; flagged by `apm audit`) |
| **Allowed values** | `auto` or a list of paths relative to the project root |

Declares which local `.apm/` content the project consents to publish when packing or deploying. Three forms are supported:

1. **Undeclared** -- field omitted. Legacy behaviour: all local `.apm/` content is published as if `auto` were set. `apm audit` emits an `includes-consent` advisory (the check itself passes; the message recommends declaring `includes: auto`) whenever local content is deployed under this form.
2. **`includes: auto`** -- explicit consent to publish all local `.apm/` content via the file scanner. No path enumeration required. Default for newly initialised projects.
3. **`includes: [<path>, ...]`** -- explicit allow-list of paths the project consents to publish. Strongest governance form; changes are reviewable in PR diffs.

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

When `policy.manifest.require_explicit_includes` is `true` (see [Governance guide](../../enterprise/governance-guide/)), only form 3 passes the policy check; `auto` and undeclared are rejected at install/audit time by the `explicit-includes` policy check (not at YAML parse time).

### 3.10. `policy`

| | |
|---|---|
| **Type** | `map<string, string>` |
| **Required** | OPTIONAL |
| **Description** | Consumer-side controls for org policy discovery and verification. All fields are optional; defaults preserve current fail-open install behaviour. |

```yaml
policy:
  fetch_failure_default: warn      # warn | block, default warn (#829)
  hash: "sha256:<hex>"             # optional consumer-side pin on the org policy bytes
  hash_algorithm: sha256           # sha256 (default) | sha384 | sha512
```

| Sub-key | Type | Default | Allowed values | Semantic |
|---|---|---|---|---|
| `fetch_failure_default` | `string` | `warn` | `warn`, `block` | Posture when no policy is reachable AND none is cached. `warn` keeps installs unblocked when GitHub is unreachable; `block` opts into fail-closed semantics. See [Network failure semantics](../../enterprise/policy-reference/#95-network-failure-semantics). |
| `hash` | `string` | unset | `<algo>:<hex-digest>` (e.g. `sha256:6a8c...e2f1`) | Pin on the raw bytes of the fetched leaf org policy. Verified before YAML parsing; mismatch is always fail-closed regardless of `fetch_failure_default`. See [Hash pin: `policy.hash`](../../enterprise/policy-reference/#96-hash-pin-policyhash-consumer-side-verification). |
| `hash_algorithm` | `string` | `sha256` | `sha256`, `sha384`, `sha512` | Digest algorithm for `policy.hash`. Inferred from the `<algo>:` prefix when present; this field is the explicit override. MD5 and SHA-1 are rejected at parse time. |

---

## 4. Dependencies

| | |
|---|---|
| **Type** | `object` |
| **Required** | OPTIONAL |
| **Known keys** | `apm`, `mcp` |

Contains two OPTIONAL lists: `apm` for agent primitive packages and `mcp` for MCP servers. Each list entry is either a string shorthand or a typed object. Additional keys MAY be present for future dependency types; conforming resolvers MUST ignore unknown keys for resolution but MUST preserve them when reading and rewriting manifests, to allow forward compatibility.

---

### 4.1. `dependencies.apm` — `list<ApmDependency>`

Each element MUST be one of two forms: **string** or **object**.

#### 4.1.1. String Form

Grammar (ABNF-style):

```
dependency     = url_form / shorthand_form / local_path_form
url_form       = ("https://" / "http://" / "ssh://git@" / "git@") clone-url
shorthand_form = [host "/"] owner "/" repo ["/" virtual_path] ["#" ref]
local_path_form = ("./" / "../" / "/" / "~/" / ".\\" / "..\\" / "~\\") path
```

`clone-url` MAY include a `:port` segment on `https://`, `http://`, and `ssh://git@` forms (e.g. `ssh://git@host:7999/owner/repo.git`). The SCP shorthand `git@host:path` cannot carry a port — `:` is the path separator in that form. When a port is present, APM preserves it across all clone attempts: the SSH attempt uses `ssh://host:PORT/...` and the HTTPS fallback uses `https://host:PORT/...` (same port on both protocols).

| Segment | Required | Pattern | Description |
|---|---|---|---|
| `host` | OPTIONAL | FQDN (e.g. `gitlab.com`) | Git host. Defaults to `github.com`. |
| `port` | OPTIONAL | `1`–`65535` | Non-default port on `ssh://`, `https://`, `http://` clone URLs. Not expressible in SCP shorthand. |
| `owner/repo` | REQUIRED | 2+ path segments of `[a-zA-Z0-9._-]+` | Repository path. GitHub uses exactly 2 segments (`owner/repo`). Non-GitHub hosts MAY use nested groups (e.g. `gitlab.com/group/sub/repo`). |
| `virtual_path` | OPTIONAL | Path segments after repo | Subdirectory, file, or collection within the repo. See §4.1.3. |
| `ref` | OPTIONAL | Branch, tag, or commit SHA | Git reference. Commit SHAs matched by `^[a-f0-9]{7,40}$`. Semver tags matched by `^v?\d+\.\d+\.\d+`. |

**Examples:**

```yaml
dependencies:
  apm:
    # GitHub shorthand (default host) — each line shows a syntax variant
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

REQUIRED when the shorthand is ambiguous (e.g. nested-group repos with virtual paths).

| Field | Type | Required | Pattern / Constraint | Description |
|---|---|---|---|---|
| `git` | `string` | REQUIRED (remote) | HTTPS URL, SSH URL, or FQDN shorthand | Clone URL of the repository. Required for remote dependencies. |
| `path` | `string` | OPTIONAL / REQUIRED (local) | Relative path within the repo, or local filesystem path | When `git` is present: subdirectory, file, or collection (virtual package). When `git` is absent: local filesystem path (must start with `./`, `../`, `/`, or `~/`). |
| `ref` | `string` | OPTIONAL | Branch, tag, or commit SHA | Git reference to checkout. |
| `alias` | `string` | OPTIONAL | `^[a-zA-Z0-9._-]+$` | Local alias. |

Remote dependency (git URL + sub-path):

```yaml
- git: https://gitlab.com/acme/repo.git
  path: instructions/security
  ref: v2.0
  alias: acme-sec
```

Local path dependency (development only):

```yaml
- path: ./packages/my-shared-skills
```

#### 4.1.3. Virtual Packages

A dependency MAY target a subdirectory, file, or collection within a repository rather than the whole repo. Conforming resolvers MUST classify virtual packages using the following rules, evaluated in order:

| Kind | Detection rule | Example |
|---|---|---|
| **File** | `virtual_path` ends in `.prompt.md`, `.instructions.md`, `.agent.md`, or `.chatmode.md` | `owner/repo/prompts/review.prompt.md` |
| **Collection (dir)** | `virtual_path` contains `/collections/` (no collection extension) | `owner/repo/collections/security` |
| **Collection (manifest)** | `virtual_path` contains `/collections/` and ends with `.collection.yml` or `.collection.yaml` | `owner/repo/collections/security.collection.yml` |
| **Subdirectory** | `virtual_path` does not match any file, collection, or extension rule above | `owner/repo/skills/security` |

#### 4.1.4. Canonical Normalisation

Conforming writers MUST normalise entries to canonical form on write. `github.com` is the default host and MUST be stripped; all other hosts MUST be preserved as FQDN.

| Input | Canonical form |
|---|---|
| `https://github.com/microsoft/apm-sample-package.git` | `microsoft/apm-sample-package` |
| `git@github.com:microsoft/apm-sample-package.git` | `microsoft/apm-sample-package` |
| `gitlab.com/acme/repo` | `gitlab.com/acme/repo` |

---

### 4.2. `dependencies.mcp` — `list<McpDependency>`

Each element MUST be one of two forms: **string** or **object**.

#### 4.2.1. String Form

A plain registry reference: `io.github.github/github-mcp-server`

#### 4.2.2. Object Form

| Field | Type | Required | Constraint | Description |
|---|---|---|---|---|
| `name` | `string` | REQUIRED | Non-empty | Server identifier (registry name or custom name). |
| `transport` | `enum<string>` | Conditional | `stdio` · `sse` · `http` · `streamable-http` | Transport protocol. REQUIRED when `registry: false`. Values are MCP transport names, not URL schemes: remote variants connect over HTTPS. |
| `env` | `map<string, string>` | OPTIONAL | | Environment variable overrides. Values may contain `${input:<id>}` references (VS Code only — see §4.2.4). |
| `args` | `dict` or `list` | OPTIONAL | | Dict for overlay variable overrides (registry), list for positional args (self-defined). |
| `version` | `string` | OPTIONAL | | Pin to a specific server version. |
| `registry` | `bool` or `string` | OPTIONAL | Default: `true` (public registry) | `false` = self-defined (private) server. String = custom registry URL. |
| `package` | `enum<string>` | OPTIONAL | `npm` · `pypi` · `oci` | Package manager type hint. |
| `headers` | `map<string, string>` | OPTIONAL | | Custom HTTP headers for remote endpoints. Values may contain `${input:<id>}` references (VS Code only — see §4.2.4). |
| `tools` | `list<string>` | OPTIONAL | Default: `["*"]` | Restrict which tools are exposed. |
| `url` | `string` | Conditional | | Endpoint URL. REQUIRED when `registry: false` and `transport` is `http`, `sse`, or `streamable-http`. |
| `command` | `string` | Conditional | Single binary path; no embedded whitespace unless `args` is also present | Binary path. REQUIRED when `registry: false` and `transport` is `stdio`. |

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
```

#### 4.2.4. `${input:...}` Variables

Values in `headers` and `env` may contain VS Code input variable references using the syntax `${input:<variable-id>}`. At runtime, VS Code prompts the user for each referenced input before starting the server.

- **Registry-backed servers** — APM auto-generates input prompts from registry metadata.
- **Self-defined servers** — APM detects `${input:...}` patterns in `apm.yml` and generates matching input definitions automatically.

```yaml
dependencies:
  mcp:
    - name: my-server
      registry: false
      transport: http
      url: https://my-server.example.com/mcp/
      headers:
        Authorization: "Bearer ${input:my-server-token}"
        X-Project: "${input:my-server-project}"
```

| Runtime | `${input:...}` support |
|---------|----------------------|
| VS Code | Yes — prompts user at runtime |
| Copilot CLI | No — use environment variables |
| Codex | No — use environment variables |

---

## 5. devDependencies

| | |
|---|---|
| **Type** | `object` |
| **Required** | OPTIONAL |
| **Known keys** | `apm`, `mcp` |

Development-only dependencies installed locally but excluded from plugin bundles (`apm pack --format plugin`). Uses the same structure as [`dependencies`](#4-dependencies).

```yaml
devDependencies:
  apm:
    - owner/test-helpers
    - owner/lint-rules#v2.0.0
```

Created automatically by `apm init --plugin`. Use [`apm install --dev`](../cli-commands/#apm-install---install-dependencies-and-deploy-local-content) to add packages:

```bash
apm install --dev owner/test-helpers
```

---

## 6. Compilation

The `compilation` key is OPTIONAL. It controls `apm compile` behaviour. All fields have sensible defaults; omitting the entire section is valid.

| Field | Type | Default | Constraint | Description |
|---|---|---|---|---|
| `target` | `enum<string>` | `all` | `vscode` · `agents` · `claude` · `codex` · `all` | Output target (same values as §3.6). Defaults to `all` when set explicitly in compilation config. |
| `strategy` | `enum<string>` | `distributed` | `distributed` · `single-file` | `distributed` generates per-directory AGENTS.md files. `single-file` generates one monolithic file. |
| `single_file` | `bool` | `false` | | Legacy alias. When `true`, overrides `strategy` to `single-file`. |
| `output` | `string` | `AGENTS.md` | File path | Custom output path for the compiled file. |
| `chatmode` | `string` | — | | Chatmode filter for compilation. |
| `resolve_links` | `bool` | `true` | | Resolve relative Markdown links in primitives. |
| `source_attribution` | `bool` | `true` | | Include source-file origin comments in compiled output. |
| `exclude` | `list<string>` or `string` | `[]` | Glob patterns | Directories to skip during compilation (e.g. `apm_modules/**`). |
| `placement` | `object` | — | | Placement tuning. See §6.1. |

### 6.1. `compilation.placement`

| Field | Type | Default | Description |
|---|---|---|---|
| `min_instructions_per_file` | `int` | `1` | Minimum instruction count to warrant a separate AGENTS.md file. |

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

---

## 7. Lockfile (`apm.lock.yaml`)

After successful dependency resolution, a conforming resolver MUST write a lockfile capturing the exact resolved state. The lockfile MUST be a YAML file named `apm.lock.yaml` at the project root. It SHOULD be committed to version control.

### 7.1. Structure

```yaml
lockfile_version: "1"
generated_at:     <ISO 8601 timestamp>
apm_version:      <string>
dependencies:                              # YAML list (not a map)
  - repo_url:        <string>              # Resolved clone URL
    host:            <string>              # Git host (OPTIONAL, e.g. "gitlab.com")
    port:            <int>                 # Non-default git port (OPTIONAL, 1-65535; omitted when default)
    resolved_commit: <string>              # Full commit SHA
    resolved_ref:    <string>              # Branch/tag that was resolved
    version:         <string>              # Package version from its apm.yml
    virtual_path:    <string>              # Virtual package path (if applicable)
    is_virtual:      <bool>                # True for virtual (file/subdirectory) packages
    depth:           <int>                 # 1 = direct, 2+ = transitive
    resolved_by:     <string>              # Parent dependency (transitive only)
    package_type:    <string>              # Package type (e.g. "apm_package", "marketplace_plugin")
    content_hash:    <string>              # SHA-256 of package file tree (e.g. "sha256:a1b2c3...")
    is_dev:          <bool>                # True for devDependencies
    deployed_files:  <list<string>>        # Workspace-relative paths of installed files
mcp_servers:       <list<string>>          # MCP dependency references managed by APM (OPTIONAL, e.g. "io.github.github/github-mcp-server")
```

### 7.2. Resolver Behaviour

1. **First install** — Resolve all dependencies, write `apm.lock.yaml`.
2. **Subsequent installs** — Read `apm.lock.yaml`, use locked commit SHAs. A resolver SHOULD skip download if local checkout already matches.
3. **`--update` flag** — Re-resolve from `apm.yml`, overwrite lockfile.

---

## 8. Integrator Contract

Any runtime adopting this format (e.g. GitHub Agentic Workflows, CI systems, IDEs) MUST implement these steps:

1. **Parse** — Read `apm.yml` as YAML. Validate the two REQUIRED fields (`name`, `version`) and the `dependencies` object shape.
2. **Resolve `dependencies.apm`** — For each entry, clone/fetch the git repo (respecting `ref`), locate the `.apm/` directory (or virtual path), and extract primitives.
3. **Resolve `dependencies.mcp`** — For each entry, resolve from the MCP registry or validate self-defined transport config per §4.2.3.
4. **Transitive resolution** — Resolved packages MAY contain their own `apm.yml` with further dependencies, forming a dependency tree. Resolvers MUST resolve transitively. Conflicts are merged at instruction level (by `applyTo` pattern), not file level.
5. **Write lockfile** — Record exact commit SHAs and deployed file paths in `apm.lock.yaml` per §7.

---

## Appendix A. Complete Example

```yaml
name: my-project
version: 1.0.0
description: AI-native web application
author: Contoso
license: MIT
target: all
type: hybrid              # instructions | skill | hybrid | prompts

scripts:
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
```

---

## Appendix B. Revision History

| Version | Date | Changes |
|---|---|---|
| 0.1 | 2026-03-06 | Initial Working Draft. |
