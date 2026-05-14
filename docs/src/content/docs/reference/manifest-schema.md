---
title: "Manifest Schema"
description: "The apm.yml format -- top-level fields, dependencies, marketplace block, and integrator contract."
sidebar:
  order: 1
---

<dl>
<dt>Version</dt><dd>0.2 (Working Draft)</dd>
<dt>Date</dt><dd>2026-05-10</dd>
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
dependencies:
  apm:         <list<ApmDependency>>
  mcp:         <list<McpDependency>>
devDependencies:
  apm:         <list<ApmDependency>>
  mcp:         <list<McpDependency>>
compilation:   <CompilationConfig>
policy:        <PolicyConfig>
marketplace:   <MarketplaceConfig>       # OPTIONAL; marketplace authoring
```

Two fields are REQUIRED at parse time: `name` and `version`. All other fields are OPTIONAL. Unknown top-level keys MUST be preserved by writers but MAY be ignored by resolvers.

The `marketplace:` block is the source for `apm pack`'s marketplace output. Repositories that do not publish a marketplace omit it entirely. See [Section 7](#7-marketplace-authoring-block).

Newly initialised projects (`apm init`) are scaffolded by the CLI; see [`apm init`](../cli/init/) for the templates.

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
| **Description** | SPDX license identifier (e.g. `MIT`, `Apache-2.0`). |

### 3.6. `target`

| | |
|---|---|
| **Type** | `string` or `list<string>` |
| **Required** | OPTIONAL |
| **Default** | Auto-detect from folder presence (see below). |
| **Allowed values** | `vscode`, `agents`, `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `windsurf`, `all` |

Controls which output targets are generated during compilation, installation, and packing. Accepts a single string or a YAML list. Unknown values MUST raise a parse error at load time, naming the offending token.

When `target:` is omitted, a conforming resolver SHOULD auto-detect: `vscode` if `.github/` exists, `claude` if `.claude/` exists, `codex` if `.codex/` exists, `windsurf` if `.windsurf/` exists, `all` if multiple are present, `minimal` if none. Auto-detection applies only when `target:` is unset; once set, the field is authoritative.

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

When a list is specified, only those targets are compiled, installed, and packed; no output is generated for unlisted targets. `all` cannot be combined with other values.

| Value | Effect |
|---|---|
| `vscode` | Emits `AGENTS.md` at the project root (and per-directory files in distributed mode). |
| `agents` | Alias for `vscode`. |
| `copilot` | Alias for `vscode`. |
| `claude` | Emits `CLAUDE.md` at the project root. |
| `cursor` | Emits to `.cursor/rules/`, `.cursor/agents/`, `.cursor/skills/`. |
| `opencode` | Emits to `.opencode/agents/`, `.opencode/commands/`, `.opencode/skills/`. |
| `codex` | Emits `AGENTS.md` and deploys skills to `.agents/skills/`, agents to `.codex/agents/`. |
| `gemini` | Emits `GEMINI.md` and deploys to `.gemini/commands/`, `.gemini/skills/`, `.gemini/settings.json`. |
| `windsurf` | Emits `AGENTS.md` and deploys to `.windsurf/rules/`, `.windsurf/skills/`, `.windsurf/workflows/`, `.windsurf/hooks.json`. |
| `all` | All targets. Cannot be combined with other values in a list. |
| `minimal` | `AGENTS.md` only at project root. **Auto-detected only**: this value MUST NOT be set explicitly in manifests; it is an internal fallback when no target folder is detected. |

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

`includes:` is allow-list only. There is no `exclude:` form. To keep maintainer-only primitives out of shipped artifacts, author them OUTSIDE `.apm/` and reference them via a local-path devDependency. See [Dev-only Primitives](../../guides/dev-only-primitives/).

When `policy.manifest.require_explicit_includes` is `true` (see [Policy reference](../../enterprise/policy-reference/)), only form 3 passes; `auto` and undeclared are rejected at install/audit time by the `explicit-includes` check (not at YAML parse time).

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

Full semantics (network failure matrix, hash pin verification, policy precedence) live in the [Policy reference](../../enterprise/policy-reference/).

---

## 4. Dependencies

| | |
|---|---|
| **Type** | `object` |
| **Required** | OPTIONAL |
| **Known keys** | `apm`, `mcp` |

Contains two OPTIONAL lists: `apm` for agent primitive packages and `mcp` for MCP servers. Each list entry is either a string shorthand or a typed object. Additional keys MAY be present for future dependency types; conforming resolvers MUST ignore unknown keys for resolution but MUST preserve them when reading and rewriting manifests.

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

`clone-url` MAY include a `:port` segment on `https://`, `http://`, and `ssh://git@` forms (e.g. `ssh://git@host:7999/owner/repo.git`). The SCP shorthand `git@host:path` cannot carry a port because `:` is the path separator in that form. When a port is present, APM preserves it across all clone attempts: the SSH attempt uses `ssh://host:PORT/...` and the HTTPS fallback uses `https://host:PORT/...` (same port on both protocols).

| Segment | Required | Pattern | Description |
|---|---|---|---|
| `host` | OPTIONAL | FQDN (e.g. `gitlab.com`) | Git host. Defaults to `github.com`. |
| `port` | OPTIONAL | `1`-`65535` | Non-default port on `ssh://`, `https://`, `http://` clone URLs. Not expressible in SCP shorthand. |
| `owner/repo` | REQUIRED | 2+ path segments of `[a-zA-Z0-9._-]+` | Repository path. GitHub uses exactly 2 segments. Non-GitHub hosts MAY use nested groups (e.g. `gitlab.com/group/sub/repo`). |
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

REQUIRED when the shorthand is ambiguous (e.g. nested-group repos with virtual paths).

| Field | Type | Required | Pattern / Constraint | Description |
|---|---|---|---|---|
| `git` | `string` | REQUIRED (remote) | HTTPS URL, SSH URL, or FQDN shorthand | Clone URL of the repository. Required for remote dependencies. |
| `path` | `string` | OPTIONAL / REQUIRED (local) | Relative path within the repo, or local filesystem path | When `git` is present: subdirectory or file (virtual package). When `git` is absent: local filesystem path (must start with `./`, `../`, `/`, or `~/`). |
| `ref` | `string` | OPTIONAL | Branch, tag, or commit SHA | Git reference to checkout. |
| `alias` | `string` | OPTIONAL | `^[a-zA-Z0-9._-]+$` | Local alias. |

Remote dependency (git URL plus sub-path):

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

Monorepo sibling reference (`git: parent`):

```yaml
# In agents/pkg-a/apm.yml inside org/monorepo
- git: parent
  path: skills/shared
```

The literal sentinel `git: parent` is valid only inside a transitively resolved package whose clone coordinates are known to the resolver. APM expands `parent` to the consumer's `host`, `repo_url`, and resolved `ref`, with `virtual_path` set from `path`. The lockfile records the **expanded** coordinates: `parent` MUST NOT appear as durable identity (`repo_url` / `source`). `path` is REQUIRED for `git: parent` and is normalised to a single relative path; absolute paths and `..` traversal are refused. `ref` and `alias` overrides are accepted; when `ref` is omitted the parent's resolved ref is inherited.

#### 4.1.3. Virtual Packages

A dependency MAY target a subdirectory or a file within a repository rather than the whole repo. Conforming resolvers MUST classify virtual packages using the following rules, evaluated in order:

| Kind | Detection rule | Example |
|---|---|---|
| **File** | `virtual_path` ends in `.prompt.md`, `.instructions.md`, `.agent.md`, or `.chatmode.md` | `owner/repo/prompts/review.prompt.md` |
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

#### 4.2.4. Variable References in `headers` and `env`

Values in `headers` and `env` may contain three placeholder syntaxes. APM resolves them per-target so secrets stay out of generated config files where possible.

| Syntax | Source | VS Code | Copilot CLI / Codex |
|---|---|---|---|
| `${VAR}` | host environment | Translated to `${env:VAR}` (resolved at server-start by VS Code) | Resolved at install time from env (or interactive prompt) |
| `${env:VAR}` | host environment | Native; passed through verbatim | Resolved at install time from env (or interactive prompt) |
| `${input:<id>}` | user prompt | Native; VS Code prompts at runtime | Not supported; use `${VAR}` or `${env:VAR}` instead |
| `<VAR>` (legacy) | host environment | Not recognized | Resolved at install time (kept for back-compat) |

- **VS Code** has native `${env:VAR}` and `${input:VAR}` interpolation, so APM emits placeholders rather than baking secrets into `mcp.json`. Bare `${VAR}` is normalized to `${env:VAR}` for you.
- **Copilot CLI** has no runtime interpolation, so APM resolves `${VAR}`, `${env:VAR}`, and the legacy `<VAR>` at install time using `os.environ` (or an interactive prompt when missing). Resolved values are not re-scanned, so a value containing literal `${...}` text is preserved.
- **Codex** currently resolves only the legacy `<VAR>` placeholder at install time; `${VAR}` / `${env:VAR}` are passed through verbatim.
- **Recommended:** Use `${VAR}` or `${env:VAR}` in all new manifests. They work on every target that supports remote MCP servers. `<VAR>` is legacy and only resolved by Copilot CLI and Codex; in VS Code it would silently render as literal text.
- **Registry-backed servers**: APM auto-generates input prompts from registry metadata for `${input:...}`.
- **Self-defined servers**: APM detects `${input:...}` patterns in `apm.yml` and generates matching input definitions automatically.

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

## 5. devDependencies

| | |
|---|---|
| **Type** | `object` |
| **Required** | OPTIONAL |
| **Known keys** | `apm`, `mcp` |

Development-only dependencies installed locally but excluded from plugin bundles produced by [`apm pack`](../cli/pack/) (plugin format is the default). Uses the same structure as [`dependencies`](#4-dependencies).

```yaml
devDependencies:
  apm:
    - owner/test-helpers
    - owner/lint-rules#v2.0.0
```

Created automatically by `apm init --plugin`. Use [`apm install --dev`](../cli/install/) to add packages:

```bash
apm install --dev owner/test-helpers
```

Plain `apm install` (no flag) deploys both `dependencies` and `devDependencies`. There is no `--omit=dev` flag today; the dev/prod separation kicks in at `apm pack` (plugin format, the default). The local-content scanner that builds plugin bundles operates on `.apm/` only and does not consult the devDep marker. To keep maintainer-only primitives out of shipped artifacts, author them outside `.apm/` and reference them via a local-path devDependency. See [Dev-only Primitives](../../guides/dev-only-primitives/).

Local-path devDependency example:

```yaml
devDependencies:
  apm:
    - path: ./dev/skills/release-checklist
```

---

## 6. Compilation

The `compilation` key is OPTIONAL. It controls [`apm compile`](../cli/compile/) behaviour. All fields have sensible defaults; omitting the entire section is valid.

| Field | Type | Default | Constraint | Description |
|---|---|---|---|---|
| `target` | `enum<string>` | `all` | Same values as Section 3.6 | Output target. Defaults to `all` when set explicitly in compilation config. |
| `strategy` | `enum<string>` | `distributed` | `distributed`, `single-file` | `distributed` generates per-directory `AGENTS.md` files. `single-file` generates one monolithic file. |
| `single_file` | `bool` | `false` | | Legacy alias. When `true`, overrides `strategy` to `single-file`. |
| `output` | `string` | `AGENTS.md` | File path | Custom output path for the compiled file. |
| `chatmode` | `string` | unset | | Chatmode filter for compilation. |
| `resolve_links` | `bool` | `true` | | Resolve relative Markdown links in primitives. |
| `source_attribution` | `bool` | `true` | | Include source-file origin comments in compiled output. |
| `exclude` | `list<string>` or `string` | `[]` | Glob patterns | Directories to skip during compilation (e.g. `apm_modules/**`). |
| `placement` | `object` | unset | | Placement tuning. See Section 6.1. |

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

---

## 7. Marketplace Authoring Block

The OPTIONAL `marketplace:` block declares the metadata `apm pack` needs to emit a Claude-Code-compatible plugin marketplace (`marketplace.json`). It is read by `apm marketplace` subcommands and ignored by everything else. Repositories that do not publish a marketplace omit it entirely.

The block was previously a standalone `marketplace.yml` file (still loadable for back-compat); the in-`apm.yml` form is canonical and is what [`apm marketplace init`](../cli/marketplace/) scaffolds.

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
| `name` | `string` | OPTIONAL (override) | inherited | Override of top-level `name`. |
| `description` | `string` | OPTIONAL (override) | inherited | Override of top-level `description`. |
| `version` | `string` | OPTIONAL (override) | inherited | Override of top-level `version`. Validated as semver. |
| `owner` | `Owner` | REQUIRED | -- | Marketplace publisher identity. See Section 7.3. |
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
| `source` | `string` | REQUIRED | `<owner>/<repo>` (remote) or `./<path>` (local). Must match the source pattern; path traversal (`..`) is refused. |
| `subdir` | `string` | OPTIONAL | Subdirectory inside the source repo. Path-traversal-validated. Ignored for local sources. |
| `version` | `string` | Conditional | Semver range (e.g. `^1.0.0`, `~2.1.0`, `>=3.0`). Stored as a string; resolution happens at pack time. REQUIRED for remote packages unless `ref` is given. |
| `ref` | `string` | Conditional | Explicit git ref (SHA, tag, or branch). Overrides `version` range when both are present. REQUIRED for remote packages unless `version` is given. |
| `tag_pattern` | `string` | OPTIONAL | Per-package override of `build.tagPattern`. Same placeholder rule. |
| `include_prerelease` | `bool` | `false` | Whether semver pre-release tags are eligible for resolution. |
| `description` | `string` | OPTIONAL | Pass-through to `marketplace.json`. |
| `homepage` | `string` | OPTIONAL | Pass-through to `marketplace.json`. |
| `tags` | `list<string>` | OPTIONAL | Pass-through to `marketplace.json`. Limited to 50 tags, 100 chars each. |
| `keywords` | `list<string>` | OPTIONAL | Alias merged into `tags` (deduplicated). |
| `author` | `string` or `object` | OPTIONAL | Either a non-empty string (treated as `name`) or an object with `name` (REQUIRED), `email`, `url`. |
| `license` | `string` | OPTIONAL | Pass-through (SPDX identifier). |
| `repository` | `string` | OPTIONAL | Pass-through. |

Remote packages MUST declare at least one of `version` or `ref`. Local packages (sources beginning with `./`) skip git resolution and have no version requirement.

### 7.6. Complete Marketplace Block

```yaml
marketplace:
  # name, description, version inherit from top-level apm.yml
  owner:
    name: contoso
    url:  https://github.com/contoso

  output: .claude-plugin/marketplace.json

  metadata:
    homepage: https://contoso.example/marketplace

  build:
    tagPattern: "v{version}"

  packages:
    - name: code-review
      source: contoso/code-review
      version: "^1.0.0"
      description: AI code-review skills
      tags: [review, quality]

    - name: pinned-helper
      source: contoso/pinned-helper
      ref: main                              # explicit ref overrides version
      tag_pattern: "pinned-helper-v{version}"

    - name: local-tool                       # local-path package
      source: ./packages/local-tool
      description: Vendored tool
```

The legacy standalone `marketplace.yml` (top-level keys, no `marketplace:` wrapper) is still loadable but deprecated; new repositories SHOULD use the in-`apm.yml` form scaffolded by `apm marketplace init`.

---

## 8. Lockfile (`apm.lock.yaml`)

After successful dependency resolution, a conforming resolver MUST write a lockfile capturing the exact resolved state. The lockfile MUST be a YAML file named `apm.lock.yaml` at the project root and SHOULD be committed to version control.

The full lockfile schema is specified in the [Lockfile specification](../lockfile-spec/). At a minimum, every resolver MUST record `lockfile_version`, `dependencies[].repo_url`, `dependencies[].resolved_commit`, and `dependencies[].deployed_files` so subsequent installs are reproducible and `apm uninstall` can remove every placed file.

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
5. **Write lockfile** -- Record exact commit SHAs and deployed file paths in `apm.lock.yaml` per Section 8 and the [Lockfile specification](../lockfile-spec/).

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
| 0.2 | 2026-05-10 | Added Section 7 (Marketplace authoring block). Documented `scripts.start` as the default `apm run` entry point. Cross-links updated to `../cli/<verb>/` paths. ASCII-only enforcement. |