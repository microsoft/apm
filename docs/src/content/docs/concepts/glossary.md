---
title: Glossary
description: Every overloaded APM term, resolved in one paragraph. Skim alphabetically.
sidebar:
  order: 99
---

Every overloaded APM term, resolved in one paragraph. Skim alphabetically.
"What it is NOT" lines disambiguate the most common collisions.

### apm.lock.yaml

The lockfile APM writes after a successful resolve. Pins exact commit SHAs
and per-file content hashes so every `apm install` from the same lockfile
produces byte-identical output. Lives at the project root next to `apm.yml`.

NOT the manifest. The manifest declares what you want; the lockfile records
what you got.

Source: `src/apm_cli/deps/lockfile.py`.

### apm.yml

The package manifest. A YAML file at the package root that declares
`name`, `version`, `dependencies`, `scripts`, `target`, and metadata. Both
the unit of authoring and the unit of consumption -- a directory becomes
an APM package the moment it has an `apm.yml`.

NOT the lockfile, and NOT a `plugin.json`. The manifest is human-edited;
the lockfile is generated; `plugin.json` is the local-bundle descriptor.

Source: `src/apm_cli/models/apm_package.py`.

### audit

The `apm audit` command. Scans installed primitives for hidden Unicode
that could embed invisible instructions, and (with `--ci`) re-derives
content from the lockfile to detect drift before it ships. Emits SARIF,
JSON, or rendered output. Standalone scanning of arbitrary files is
available via `--file`.

NOT the same thing as install-time scanning. Install-time scanning is
automatic and blocks critical findings; `audit` is the explicit reporting
and remediation surface.

See: [Security](/apm/enterprise/security/).
Source: `src/apm_cli/commands/audit.py`.

### bundle

A local-install artifact produced by `apm pack`. Either a directory or a
`.tar.gz` containing `plugin.json` at the root and (in current versions)
an embedded `apm.lock.yaml` with per-file SHA-256 hashes. Installed via
`apm install <path-or-tarball>`.

NOT a package source repository. A bundle is the packed, hash-verified
output of one; you ship bundles, you author packages.

Source: `src/apm_cli/bundle/local_bundle.py`.

### compile

The `apm compile` command. Takes resolved primitives in `apm_modules/`
and writes them into each declared harness location (`.github/`,
`.claude/`, `.cursor/`, etc.) using the format that harness expects.
Runs automatically as the final phase of `apm install`.

NOT a build step that produces an artifact. Compile only deploys to
local harness directories.

See: [Compilation guide](/apm/producer/compile/).
Source: `src/apm_cli/commands/compile/`.

### dev-only primitive

A dependency listed under `devDependencies:` in `apm.yml` (mirroring `package.json`). Installed locally
for authoring and testing but excluded from the bundle that `apm pack`
ships. The lockfile records the `is_dev` flag per package.

NOT a separate primitive type. Any package or primitive can be marked
dev-only; it is a visibility flag, not a category.

Source: `src/apm_cli/deps/lockfile.py`,
`src/apm_cli/deps/installed_package.py`.

### GitHub APM PAT

The personal access token APM reads to authenticate against GitHub
when resolving private packages. Resolution order:
`GITHUB_APM_PAT_<ORG>` (per-org), then `GITHUB_APM_PAT`, then
`GITHUB_TOKEN`, then `GH_TOKEN`. Public packages need no token.

NOT a separate token type. It is a standard GitHub PAT; the
`GITHUB_APM_PAT` name exists so APM-scoped tokens do not collide with
other tooling.

See: [Authentication](/apm/consumer/authentication/).
Source: `src/apm_cli/core/auth.py`.

### harness

The agent runtime that executes primitives: GitHub Copilot (CLI + IDE),
Claude Code, Cursor, Codex, Gemini, OpenCode, Windsurf. Each harness has
its own primitive directory layout and file format.

NOT the same as a target. The target is the `apm.yml` field that selects
which harnesses to compile for; the harness is the runtime itself.

Source: `src/apm_cli/integration/targets.py` (see `KNOWN_TARGETS`).

### hook

A primitive type whose contents run at a defined lifecycle event in the
host harness (for example `PreToolUse`). Supported on every harness
except OpenCode -- see the matrix in
[primitives and targets](/apm/concepts/primitives-and-targets/).

NOT an APM CLI lifecycle event. Hooks fire inside the agent runtime,
not inside `apm install` or `apm compile`.

Source: `src/apm_cli/integration/hook_integrator.py`.

### install

The `apm install` command. Resolves dependencies declared in `apm.yml`,
downloads them into `apm_modules/`, runs the policy gate, scans for
hidden Unicode, writes `apm.lock.yaml`, and compiles primitives into
each declared harness directory.

All major verbs match npm semantics: `apm install` deploys, `apm update`
refreshes dependencies, `apm install --frozen` is the lockfile-only CI
install (mirrors `npm ci`). The CLI binary itself updates via
`apm self-update`, not `apm update`.

Source: `src/apm_cli/commands/install.py`.

### lockfile

See [apm.lock.yaml](#apmlockyaml).

### manifest

See [apm.yml](#apmyml).

### marketplace

A curated index of packages, hosted as a Git repository with a
`marketplace.json` at its root. Lists packages by handle and points each
one at a Git source. Authors publish their packages to a marketplace so
consumers can discover and install them by short name.

NOT a registry. A marketplace is human-curated discovery; the registry
is the resolution backend that APM actually downloads from.

See: [Marketplaces guide](/apm/consumer/private-and-org-packages/).
Source: `src/apm_cli/commands/marketplace/`.

### MCP server

A Model Context Protocol server declared as a dependency under
`mcp:` in `apm.yml`. APM resolves MCP servers transitively, applies
the same policy gate, and writes the runtime config into each harness
that supports MCP.

NOT an APM primitive type. MCP servers are external processes; APM
declares and gates them but does not ship their code.

Source: `src/apm_cli/install/mcp/`,
`src/apm_cli/integration/mcp_integrator.py`.

### package

The APM unit of distribution: a directory whose root contains an
`apm.yml`. Packages declare primitives, dependencies, scripts, and
targets. A repository may contain one package at the root or several in
subdirectories.

NOT a plugin (see below) and NOT a bundle. A package is the source-form
unit; the bundle is its packed form; `plugin.json` is a separate
descriptor format.

Source: `src/apm_cli/models/apm_package.py`.

### plugin

A local-install artifact whose root contains a `plugin.json` (Claude
Code / Copilot CLI plugin format). APM detects plugins on
`apm install <path>`, treats them as packages by synthesising an
`apm.yml` from the `plugin.json`, then installs them through the
standard pipeline.

NOT a different thing from a package at runtime. Plugin format is the
input shape; once detected, APM handles plugins exactly like packages.

See: [Plugins guide](/apm/producer/author-primitives/).
Source: `src/apm_cli/bundle/local_bundle.py`,
`src/apm_cli/commands/install.py`.

### policy

The `apm-policy.yml` file plus the install-time enforcement gate that
reads it. Lets a security team allow-list sources, scopes, and primitive
kinds. Tightens-only across enterprise -> org -> repo. Runs before any
file is written to disk, including for transitive MCP servers.

NOT the same as `audit`. Policy enforces at install time; audit reports
after the fact.

See: [Governance guide](/apm/enterprise/governance-guide/).
Source: `src/apm_cli/policy/`.

### primitive

The atomic unit APM ships. The supported kinds are: instructions,
skills, prompts, agents, hooks, commands, plugins, and MCP servers.
Each kind has its own integrator that knows how to deploy it into each
harness.

NOT every file in a package. Only files matching the primitive layout
under recognised directories (`agents/`, `skills/`, `prompts/`,
`instructions/`, `hooks/`, `commands/`) are deployed.

See: [Primitives and targets](/apm/concepts/primitives-and-targets/).
Source: `src/apm_cli/integration/`.

### registry

The resolution backend APM downloads packages from. In the current
implementation this is GitHub (or any Git host reachable over HTTPS or
SSH); enterprise customers can pin to GHES, ADO, or GitLab.

NOT a marketplace. The registry is where bytes come from; a marketplace
is a curated index that points at registry locations.

Source: `src/apm_cli/core/auth.py`,
`src/apm_cli/utils/github_host.py`.

### script

An entry under `scripts:` in `apm.yml`, mapped to a shell command.
Invoked with `apm run <name>`. Used for the post-install workflow that
launches an agent against the compiled primitives (for example
`apm run start`).

NOT a primitive. Scripts are project-level commands; they do not deploy
into harness directories.

Source: `src/apm_cli/models/apm_package.py`,
`src/apm_cli/commands/run.py`.

### target

The `target:` field in `apm.yml`. Names which harnesses the package
compiles for (`copilot`, `claude`, `cursor`, `codex`, `gemini`,
`opencode`, `windsurf`, or `all`). Drives which integrator runs and
which directories receive output during `apm compile`.

NOT the harness itself. Target is the declaration; the harness is the
runtime that consumes the compiled output.

See: [Primitives and targets](/apm/concepts/primitives-and-targets/).
Source: `src/apm_cli/integration/targets.py`,
`src/apm_cli/core/target_detection.py`.

### transitive dependency

A dependency that another dependency pulls in. APM resolves the full
transitive closure (packages and MCP servers), applies the policy gate
to every node, and records each one in the lockfile. Insecure transitive
deps trigger an explicit error unless allow-listed.

NOT silent. Every transitive node is policy-gated and lockfile-recorded;
nothing slips in below the manifest layer.

Source: `src/apm_cli/install/context.py`,
`src/apm_cli/install/insecure_policy.py`.

### trust prompt

The install-time consent step before APM writes a new MCP server config
to disk. Required because an MCP server pulled in transitively by a deep
dependency can introduce a new outbound integration the user did not
explicitly request. Today this is enforced via `--trust-transitive-mcp`
opt-in plus `apm-policy.yml` allow-listing; an interactive prompt is on
the Promise 2 roadmap.

Source: `src/apm_cli/install/mcp/`,
`src/apm_cli/install/insecure_policy.py`.

### self-update

The `apm self-update` command. Downloads the latest release of the
`apm` CLI from the official installer URL and replaces the binary in
place. Supports `--check` to report availability without installing.
Disabled in package-manager distributions (for example, Homebrew),
which print a distributor-defined upgrade message instead.

NOT a dependency refresh. `self-update` only touches the CLI binary;
your project's `apm.yml`, lockfile, and `apm_modules/` are untouched.
For dependency refresh, see [update](#update).

Source: `src/apm_cli/commands/self_update.py`.

### update

The `apm update` command. Re-resolves every dependency in `apm.yml` to
its latest matching Git ref, prints a structured plan
(added / updated / removed / unchanged), and prompts for consent before
rewriting `apm.lock.yaml`. Defaults to **No** on the prompt; declining
exits cleanly with no writes. `--yes` skips the prompt for CI;
`--dry-run` prints the plan without prompting or writing.

Mirrors `npm update`. To pin to the existing lockfile in CI without
refreshing, use `apm install --frozen` (mirrors `npm ci`). To upgrade
the CLI binary itself, see [self-update](#self-update).

Source: `src/apm_cli/commands/update.py`.
