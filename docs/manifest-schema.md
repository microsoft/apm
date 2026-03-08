# APM Manifest Format Specification

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
dependencies:
  apm:         <list<ApmDependency>>
  mcp:         <list<McpDependency>>
compilation:   <CompilationConfig>
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
| **Type** | `enum<string>` |
| **Required** | OPTIONAL |
| **Default** | Auto-detect: `vscode` if `.github/` exists, `claude` if `.claude/` exists, `all` if both, `minimal` if neither |
| **Allowed values** | `vscode` · `agents` · `claude` · `all` |

Controls which output targets are generated during compilation. When unset, a conforming resolver SHOULD auto-detect based on `.github/` and `.claude/` folder presence. Unknown values MUST be silently ignored (auto-detection takes over).

| Value | Effect |
|---|---|
| `vscode` | Emits `AGENTS.md` at the project root (and per-directory files in distributed mode) |
| `agents` | Alias for `vscode` |
| `claude` | Emits `CLAUDE.md` at the project root |
| `all` | Both `vscode` and `claude` targets |
| `minimal` | AGENTS.md only at project root. **Auto-detected only** — this value MUST NOT be set explicitly in manifests; it is an internal fallback when no `.github/` or `.claude/` folder is detected. |

### 3.7. `type`

| | |
|---|---|
| **Type** | `enum<string>` |
| **Required** | OPTIONAL |
| **Default** | None (unset — behaviour depends on package content) |
| **Allowed values** | `instructions` · `skill` · `hybrid` · `prompts` |

Declares how the package's content is processed during install and compile:

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
dependency     = url_form / shorthand_form
url_form       = ("https://" / "http://" / "ssh://git@" / "git@") clone-url
shorthand_form = [host "/"] owner "/" repo ["/" virtual_path] ["#" ref] ["@" alias]
```

| Segment | Required | Pattern | Description |
|---|---|---|---|
| `host` | OPTIONAL | FQDN (e.g. `gitlab.com`) | Git host. Defaults to `github.com`. |
| `owner/repo` | REQUIRED | 2+ path segments of `[a-zA-Z0-9._-]+` | Repository path. GitHub uses exactly 2 segments (`owner/repo`). Non-GitHub hosts MAY use nested groups (e.g. `gitlab.com/group/sub/repo`). |
| `virtual_path` | OPTIONAL | Path segments after repo | Subdirectory, file, or collection within the repo. See §4.1.3. |
| `ref` | OPTIONAL | Branch, tag, or commit SHA | Git reference. Commit SHAs matched by `^[a-f0-9]{7,40}$`. Semver tags matched by `^v?\d+\.\d+\.\d+`. |
| `alias` | OPTIONAL | `^[a-zA-Z0-9._-]+$` | Local alias for the dependency. Appears after `#ref` in the string. |

**Examples:**

```yaml
dependencies:
  apm:
    # GitHub shorthand (default host)
    - microsoft/apm-sample-package
    - microsoft/apm-sample-package#v1.0.0
    - microsoft/apm-sample-package@standards

    # Non-GitHub hosts (FQDN preserved)
    - gitlab.com/acme/coding-standards
    - bitbucket.org/team/repo#main

    # Full URLs
    - https://github.com/microsoft/apm-sample-package.git
    - http://github.com/microsoft/apm-sample-package.git
    - git@github.com:microsoft/apm-sample-package.git
    - ssh://git@github.com/microsoft/apm-sample-package.git

    # Virtual packages
    - ComposioHQ/awesome-claude-skills/brand-guidelines   # subdirectory
    - contoso/prompts/review.prompt.md                    # single file

    # Azure DevOps
    - dev.azure.com/org/project/_git/repo
```

#### 4.1.2. Object Form

REQUIRED when the shorthand is ambiguous (e.g. nested-group repos with virtual paths).

| Field | Type | Required | Pattern / Constraint | Description |
|---|---|---|---|---|
| `git` | `string` | REQUIRED | HTTPS URL, SSH URL, or FQDN shorthand | Clone URL of the repository. |
| `path` | `string` | OPTIONAL | Relative path within the repo | Subdirectory, file, or collection (virtual package). |
| `ref` | `string` | OPTIONAL | Branch, tag, or commit SHA | Git reference to checkout. |
| `alias` | `string` | OPTIONAL | `^[a-zA-Z0-9._-]+$` | Local alias. |

```yaml
- git: https://gitlab.com/acme/repo.git
  path: instructions/security
  ref: v2.0
  alias: acme-sec
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
| `transport` | `enum<string>` | Conditional | `stdio` · `sse` · `http` · `streamable-http` | Transport protocol. REQUIRED when `registry: false`. |
| `env` | `map<string, string>` | OPTIONAL | | Environment variable overrides. |
| `args` | `dict` or `list` | OPTIONAL | | Dict for overlay variable overrides (registry), list for positional args (self-defined). |
| `version` | `string` | OPTIONAL | | Pin to a specific server version. |
| `registry` | `bool` or `string` | OPTIONAL | Default: `true` (public registry) | `false` = self-defined (private) server. String = custom registry URL. |
| `package` | `enum<string>` | OPTIONAL | `npm` · `pypi` · `oci` | Package manager type hint. |
| `headers` | `map<string, string>` | OPTIONAL | | Custom HTTP headers for remote endpoints. |
| `tools` | `list<string>` | OPTIONAL | Default: `["*"]` | Restrict which tools are exposed. |
| `url` | `string` | Conditional | | Endpoint URL. REQUIRED when `registry: false` and `transport` is `http`, `sse`, or `streamable-http`. |
| `command` | `string` | Conditional | | Binary path. REQUIRED when `registry: false` and `transport` is `stdio`. |

#### 4.2.3. Validation Rules for Self-Defined Servers

When `registry` is `false`, the following constraints apply:

1. `transport` MUST be present.
2. If `transport` is `stdio`, `command` MUST be present.
3. If `transport` is `http`, `sse`, or `streamable-http`, `url` MUST be present.

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

---

## 5. Compilation

The `compilation` key is OPTIONAL. It controls `apm compile` behaviour. All fields have sensible defaults; omitting the entire section is valid.

| Field | Type | Default | Constraint | Description |
|---|---|---|---|---|
| `target` | `enum<string>` | `all` | `vscode` · `agents` · `claude` · `all` | Output target (same values as §3.6). Defaults to `all` when set explicitly in compilation config. |
| `strategy` | `enum<string>` | `distributed` | `distributed` · `single-file` | `distributed` generates per-directory AGENTS.md files. `single-file` generates one monolithic file. |
| `single_file` | `bool` | `false` | | Legacy alias. When `true`, overrides `strategy` to `single-file`. |
| `output` | `string` | `AGENTS.md` | File path | Custom output path for the compiled file. |
| `chatmode` | `string` | — | | Chatmode filter for compilation. |
| `resolve_links` | `bool` | `true` | | Resolve relative Markdown links in primitives. |
| `source_attribution` | `bool` | `true` | | Include source-file origin comments in compiled output. |
| `exclude` | `list<string>` or `string` | `[]` | Glob patterns | Directories to skip during compilation (e.g. `apm_modules/**`). |
| `placement` | `object` | — | | Placement tuning. See §5.1. |

### 5.1. `compilation.placement`

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

## 6. Lockfile (`apm.lock`)

After successful dependency resolution, a conforming resolver MUST write a lockfile capturing the exact resolved state. The lockfile MUST be a YAML file named `apm.lock` at the project root. It SHOULD be committed to version control.

### 6.1. Structure

```yaml
lockfile_version: "1"
generated_at:     <ISO 8601 timestamp>
apm_version:      <string>
dependencies:                              # YAML list (not a map)
  - repo_url:        <string>              # Resolved clone URL
    host:            <string>              # Git host (OPTIONAL, e.g. "gitlab.com")
    resolved_commit: <string>              # Full commit SHA
    resolved_ref:    <string>              # Branch/tag that was resolved
    version:         <string>              # Package version from its apm.yml
    virtual_path:    <string>              # Virtual package path (if applicable)
    is_virtual:      <bool>                # True for virtual (file/subdirectory) packages
    depth:           <int>                 # 1 = direct, 2+ = transitive
    resolved_by:     <string>              # Parent dependency (transitive only)
    deployed_files:  <list<string>>        # Workspace-relative paths of installed files
mcp_servers:       <list<string>>          # Short names of APM-managed MCP servers (OPTIONAL)
```

### 6.2. Resolver Behaviour

1. **First install** — Resolve all dependencies, write `apm.lock`.
2. **Subsequent installs** — Read `apm.lock`, use locked commit SHAs. A resolver SHOULD skip download if local checkout already matches.
3. **`--update` flag** — Re-resolve from `apm.yml`, overwrite lockfile.

---

## 7. Integrator Contract

Any runtime adopting this format (e.g. GitHub Agentic Workflows, CI systems, IDEs) MUST implement these steps:

1. **Parse** — Read `apm.yml` as YAML. Validate the two REQUIRED fields (`name`, `version`) and the `dependencies` object shape.
2. **Resolve `dependencies.apm`** — For each entry, clone/fetch the git repo (respecting `ref`), locate the `.apm/` directory (or virtual path), and extract primitives.
3. **Resolve `dependencies.mcp`** — For each entry, resolve from the MCP registry or validate self-defined transport config per §4.2.3.
4. **Transitive resolution** — Resolved packages MAY contain their own `apm.yml` with further dependencies, forming a dependency tree. Resolvers MUST resolve transitively. Conflicts are merged at instruction level (by `applyTo` pattern), not file level.
5. **Write lockfile** — Record exact commit SHAs and deployed file paths in `apm.lock` per §6.

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
    - microsoft/apm-sample-package
    - gitlab.com/acme/coding-standards
    - git: https://gitlab.com/acme/repo.git
      path: instructions/security
      ref: v2.0
      alias: acme-sec
  mcp:
    - io.github.github/github-mcp-server
    - name: my-private-server
      registry: false
      transport: stdio
      command: ./bin/my-server
      env:
        API_KEY: ${{ secrets.KEY }}

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
