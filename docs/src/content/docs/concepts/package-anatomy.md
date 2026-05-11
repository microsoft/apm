---
title: Package anatomy
description: The file layout of an APM package, field by field.
sidebar:
  order: 6
---

An APM package is a directory with two things: an `apm.yml` manifest and a
`.apm/` source tree. Everything else -- the lockfile, compiled output, MCP
configs, runtime-specific folders -- is generated, optional, or both.

This page walks the file tree top-down so you can recognize every piece on
sight.

## The minimal package

Three lines on disk is enough:

```
my-pkg/
+-- apm.yml
+-- .apm/
    +-- skills/hello/SKILL.md
```

```yaml
# apm.yml
name: my-pkg
version: 1.0.0
```

`name` and `version` are the only required fields.
`apm install` will validate the manifest, generate `apm.lock.yaml`, and
deploy `hello` to whatever harnesses you target.

## Full file tree

A mature package looks closer to this. One line per file; deeper pages own
the detail.

```
my-pkg/
+-- apm.yml                       # The manifest. Required. See below.
+-- apm.lock.yaml                 # Resolved versions + content hashes. Generated.
+-- apm_modules/                  # Installed dependencies. Generated. Gitignore.
+-- .apm/                         # Source primitives you author.
|   +-- instructions/             # Always-on rules attached to file globs.
|   +-- skills/                   # Multi-file capabilities (SKILL.md + assets).
|   +-- prompts/                  # Reusable prompt templates.
|   +-- agents/                   # Named agents (model + system prompt + tools).
|   +-- chatmodes/                # Chat-mode configurations.
|   +-- context/                  # Shared context fragments.
|   +-- hooks/                    # Lifecycle hooks (pre/post events).
+-- .github/                      # Compiled output for Copilot. Generated.
|   +-- instructions/
|   +-- agents/
|   +-- copilot-instructions.md
+-- .claude/                      # Compiled output for Claude Code. Generated.
+-- .cursor/                      # Compiled output for Cursor. Generated.
+-- .codex/                       # Compiled output for Codex. Generated.
+-- AGENTS.md                     # Cross-tool spec read by OpenCode, Gemini, Codex.
+-- apm-policy.yml                # Optional org/repo policy. See enterprise docs.
+-- scripts/                      # Optional helper scripts you author.
+-- tests/                        # Optional tests for your primitives.
```

Anything under `apm_modules/`, `.github/`, `.claude/`, `.cursor/`, or
`.codex/` is build output. Edit the source under `.apm/` and re-run
`apm install` -- never edit the deployed copy.

`apm init` only writes `apm.yml`. The rest appears as you author primitives
or run `apm install`.

For why `.apm/` exists at all (instead of writing straight into `.github/`),
see [Primitives and targets](/apm/concepts/primitives-and-targets/).

## Anatomy of `apm.yml`

A realistic example, every field annotated:

```yaml
# Required identity
name: my-pkg                       # Package name. Required.
version: 1.0.0                     # SemVer string. Required.

# Optional metadata
description: Code review skills for Python services
author: Jane Doe
license: MIT

# Optional content type: one of instructions, skill, hybrid, prompts.
# Constrains what `.apm/` may contain. Useful for single-purpose packages.
type: skill

# Optional target list. Pins which harnesses this package compiles to.
# Accepts a string ("copilot,claude") or a YAML list. Omit to target all.
target:
  - copilot
  - claude

# Optional. "auto" auto-publishes every primitive under .apm/, or list
# explicit repo paths to publish a subset.
includes: auto

# Optional. Runtime dependencies, grouped by kind.
dependencies:
  apm:
    - microsoft/apm-sample-package#v1.0.0   # Pinned to a tag
    - github/awesome-copilot/skills/review-and-refactor   # Single primitive
  mcp:
    - microsoft/azure-devops-mcp            # MCP server dependency

# Optional. Same shape as `dependencies`, but excluded from the shipped
# artifact. Use for dev-only tooling and tests.
devDependencies:
  apm:
    - my-org/internal-test-skills

# Optional. Named scripts you can run with `apm run <name>`.
scripts:
  start: copilot -p hello.prompt.md
  codex: codex --skip-git-repo-check hello.prompt.md
```

### Field reference

| Field            | Required | Notes                                                       |
|------------------|----------|-------------------------------------------------------------|
| `name`           | yes      | Package name.                                               |
| `version`        | yes      | SemVer string.                                              |
| `description`    | no       |                                                             |
| `author`         | no       |                                                             |
| `license`        | no       | SPDX identifier recommended.                                |
| `type`           | no       | `instructions`, `skill`, `hybrid`, or `prompts`.            |
| `target`         | no       | String or list of harness slugs.                            |
| `includes`       | no       | `"auto"` or list of repo paths.                             |
| `dependencies`   | no       | Mapping with `apm:` and/or `mcp:` keys.                     |
| `devDependencies`| no       | Same shape as `dependencies`. Excluded from `apm pack`.     |
| `scripts`        | no       | Mapping of name to shell command. Run via `apm run <name>`. |

:::note[Coming from npm?]
The shape mirrors `package.json` on purpose: `name`, `version`,
`dependencies`, `devDependencies`, `scripts`. The verbs match too:
`apm install` deploys, `apm update` refreshes dependencies, and
`apm install --frozen` is the lockfile-only CI install (mirrors
`npm ci`). The CLI binary itself updates via `apm self-update`.
:::

## Anatomy of `apm.lock.yaml`

The lockfile pins every resolved dependency to an exact commit and content
hash so two clones of the repo install byte-identical primitives. Generated
by `apm install`; commit it.

```yaml
lockfile_version: '1'
generated_at: '2026-04-21T21:45:34.516938+00:00'
apm_version: 0.10.0

dependencies:
  - repo_url: https://github.com/microsoft/apm-sample-package
    resolved_commit: a1b2c3d4e5f6...           # Exact SHA installed
    resolved_ref: v1.0.0                       # Tag/branch the SHA came from
    version: 1.0.0                             # SemVer if available
    depth: 1                                   # 1 = direct, 2+ = transitive
    package_type: APM_PACKAGE
    content_hash: sha256:9f...                 # Hash of the package file tree
    deployed_files:                            # What this dep wrote to disk
      - .github/skills/review/SKILL.md
    deployed_file_hashes:
      .github/skills/review/SKILL.md: sha256:c4...

  # A single-primitive (virtual) import looks like this:
  - repo_url: https://github.com/github/awesome-copilot
    virtual_path: skills/review-and-refactor
    is_virtual: true
    resolved_commit: 7e8f9a...
    depth: 1

mcp_servers:
  - microsoft/azure-devops-mcp

# The package's own local content. Same hashing logic as deps; lets
# `apm audit` detect hand-edits to deployed files.
local_deployed_files:
  - .github/instructions/python.instructions.md
local_deployed_file_hashes:
  .github/instructions/python.instructions.md: sha256:45...
```

### Field reference

Top-level fields:

| Field                          | Notes                                          |
|--------------------------------|------------------------------------------------|
| `lockfile_version`             | Schema version of the lockfile.                |
| `generated_at`                 | ISO timestamp of last write.                   |
| `apm_version`                  | CLI version that generated the file.           |
| `dependencies`                 | List of `LockedDependency` entries.            |
| `mcp_servers`                  | Resolved MCP server identifiers.               |
| `mcp_configs`                  | Per-harness MCP configuration blobs.           |
| `local_deployed_files`         | Files this package wrote to deployed dirs.     |
| `local_deployed_file_hashes`   | SHA-256 of each local-deployed file.           |

Per-dependency fields:

| Field                  | Notes                                          |
|------------------------|------------------------------------------------|
| `repo_url`             | Canonical clone URL.                           |
| `host`, `port`         | For non-github.com or non-standard ports.      |
| `registry_prefix`      | Artifactory-style prefix.                      |
| `resolved_commit`      | Full SHA. The thing that makes installs reproducible. |
| `resolved_ref`         | Original tag/branch the SHA was resolved from. |
| `version`              | SemVer, if the dep is versioned.               |
| `virtual_path`         | Subpath for single-primitive imports.          |
| `is_virtual`           | True for primitive-form deps.                  |
| `depth`                | 1 = direct dependency; >1 = transitive.        |
| `package_type`         | `APM_PACKAGE`, `CLAUDE_SKILL`, `HYBRID`.       |
| `deployed_files`       | Files this dep wrote to your tree.             |
| `deployed_file_hashes` | SHA-256 of each deployed file.                 |
| `source`, `local_path` | Set for `local_path:` deps.                    |
| `content_hash`         | SHA-256 of the package file tree.              |
| `is_dev`               | True for `devDependencies` entries.            |

`apm audit` rehashes everything in `deployed_file_hashes` and
`local_deployed_file_hashes` to detect hand-edits before they ship.

## The `.apm/` directory

`.apm/` is the source root. APM does not look elsewhere for primitives. Each
subdirectory holds one primitive type; file naming conventions are
documented per type.

- **`instructions/`** -- Always-on rules attached to file globs (e.g. "for
  every `*.py`, follow PEP 8"). One Markdown file per rule. Compiled into
  `.github/instructions/`, `.cursor/rules/`, and the equivalent for other
  harnesses.
- **`skills/<name>/SKILL.md`** -- Multi-file capabilities. The `SKILL.md`
  is the entry point; sibling files (templates, scripts, references) ship
  alongside it. Loaded on demand by harnesses that support skills.
- **`prompts/`** -- Reusable prompt templates, one `.prompt.md` per prompt.
  Invocable via `apm run <script>` or directly by the harness CLI.
- **`agents/`** -- Named agent definitions: model choice, system prompt,
  tool whitelist. One `.agent.md` per agent.
- **`chatmodes/`** -- Chat-mode configurations for harnesses that expose
  modes (e.g. Copilot Chat).
- **`context/`** -- Shared context fragments that other primitives can
  reference. Not loaded standalone.
- **`hooks/`** -- Lifecycle hooks fired on pre/post install, compile, or
  run events.

For what each primitive type can reach inside which harness, see
[Primitives and targets](/apm/concepts/primitives-and-targets/).
