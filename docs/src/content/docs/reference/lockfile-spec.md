---
title: Lockfile specification
description: The apm.lock.yaml format - fields, lifecycle, and how install, audit, prune, and view consume it.
sidebar:
  order: 4
---

> **Normative reference:** this page documents the v0.2 working-draft lockfile format as emitted by the current CLI. The normative, ratified contract for v0.1 is defined in [OpenAPM v0.1, Section 5 (Lockfile)](/apm/specs/openapm-v01/) and published as JSON Schema at [`lockfile-v0.1.schema.json`](/apm/specs/schemas/lockfile-v0.1.schema.json).

`apm.lock.yaml` is the pinned record of every resolved dependency and every
file APM deployed into the workspace. It is the source of truth for
reproducible installs and for drift detection. Commit it.

## Purpose

This is a **Working Draft**. The lock file format has two versions in use:
`"1"` (Git-only projects) and `"2"` (projects with at least one registry-sourced
dependency). The bump is opportunistic; see [Version bumping](#version-bumping).
Registry-sourced dependencies require the experimental registries feature
(`apm experimental enable registries`) before install or replay.

The lockfile gives APM four things:

1. **Reproducibility.** `apm install --frozen` reinstalls the exact commits
   recorded here - no resolution, no network drift.
2. **Integrity.** Recorded SHA-256 hashes let `apm audit` detect tampering
   with deployed files.
3. **Cleanup.** The list of deployed files lets `apm prune` remove orphans
   when a dependency is dropped from `apm.yml`.
4. **Inspection.** `apm view --lock` and `apm audit` read the lockfile to
   answer "what is actually installed".

## Location

The lockfile lives at the project root next to `apm.yml`:

```
my-project/
|- apm.yml
|- apm.lock.yaml      <- here
|- apm_modules/
```

Always commit it. The lockfile is what makes a fresh clone install identically
on any machine.

## Top-level structure

```yaml
lockfile_version: "1"
generated_at: "2026-05-10T20:14:00+00:00"
apm_version: "0.6.4"
dependencies:
  - repo_url: https://github.com/acme-corp/security-baseline
    resolved_commit: a1b2c3d4e5f6789012345678901234567890abcd
    resolved_ref: v2.1.0
    version: "2.1.0"
    depth: 1
    package_type: apm_package
    deployed_files:
      - .github/instructions/security.instructions.md
      - .github/agents/security-auditor.agent.md

  - repo_url: https://github.com/acme-corp/common-prompts
    resolved_commit: f6e5d4c3b2a1098765432109876543210fedcba9
    resolved_ref: main
    depth: 2
    resolved_by: https://github.com/acme-corp/security-baseline
    package_type: apm_package
    deployed_files:
      - .github/instructions/common-guidelines.instructions.md

  - repo_url: https://github.com/acme-corp/security-baseline
    source: registry
    version: "2.1.0"
    resolved_url: https://registry.example.com/v1/packages/acme/security-baseline/versions/2.1.0/download
    resolved_hash: "sha256:abc123..."
    depth: 1
    package_type: apm_package
mcp_servers:
  - github
mcp_configs:
  github:
    type: stdio
    command: docker
    args: ["run", "-i", "--rm", "ghcr.io/github/github-mcp-server"]
lsp_servers:
  - pyright
lsp_configs:
  pyright:
    name: pyright
    command: pyright-langserver
    args: ["--stdio"]
    extensionToLanguage:
      ".py": python
local_deployed_files:
  - .github/skills/my-local-skill/SKILL.md
local_deployed_file_hashes:
  .github/skills/my-local-skill/SKILL.md: "a1b2c3..."
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `lockfile_version` | string | yes | Schema version. `"1"` for Git-only projects; `"2"` when any dependency has `source: "registry"`. |
| `generated_at` | ISO 8601 string | yes | UTC timestamp of the last write. Ignored by equivalence checks. |
| `apm_version` | string | no | APM CLI version that wrote the file. Diagnostic only. |
| `dependencies` | list | yes | Resolved APM packages. See [per-entry fields](#per-entry-fields). |
| `mcp_servers` | list of strings | no | Names of MCP servers declared in the manifest at install time. |
| `mcp_configs` | map | no | `server_name -> resolved config dict` baseline used to detect MCP drift. |
| `lsp_servers` | list of strings | no | Names of LSP servers declared in the manifest at install time. |
| `lsp_configs` | map | no | `server_name -> resolved config dict` baseline used to detect LSP drift. |
| `local_deployed_files` | list | no | Files this project itself contributes (sources its own primitives). See [self entry](#self-entry). |
| `local_deployed_file_hashes` | map | no | `path -> sha256` for `local_deployed_files`. |

## Per-entry fields

Each item in `dependencies` describes one resolved package.

| Field | Type | Required | Notes |
|---|---|---|---|
| `repo_url` | string | yes | Canonical repository path or URL. Entry identity is derived from `repo_url`, `host`, and virtual/local markers; see [lockfile identity keys](#lockfile-identity-keys). |
| `host` | string | no | FQDN when not inferable from `repo_url` (e.g. for registry proxies or non-GitHub hosts). |
| `host_type` | string | no | Explicit host-kind hint, currently `gitlab`, copied from object-form `type: gitlab`. |
| `port` | int | no | Non-standard SSH/HTTPS port. Validated to `1..65535` on read. |
| `registry_prefix` | string | no | URL path prefix when resolved through a registry proxy (e.g. `artifactory/github`). |
| `resolved_ref` | string | no | The user-supplied ref from `apm.yml` (`main`, `v1.2.0`, a SHA). |
| `resolved_commit` | string | no | Exact 40-char commit SHA installed. The pin. |
| `version` | string | no | Resolved package version/ref selector. For registry entries this is the exact version selected from the registry, whether semver or not. |
| `virtual_path` | string | no | Subpath inside the repo for virtual packages (monorepo subpaths). |
| `is_virtual` | bool | no | `true` when the entry is a virtual subpath package. |
| `depth` | int | no | Position in the dependency tree. `0` is the project itself, `1` is a direct dep, higher is transitive. Defaults to `1`. |
| `resolved_by` | string | no | `repo_url` of the parent that pulled this transitive dep. Absent for direct deps. |
| `package_type` | string | no | Kind of package: `apm_package`, `skill_bundle`, `claude_skill`, `hook_package`, `hybrid`, `marketplace_plugin`. Drives target placement. |
| `skill_subset` | list of strings | no | For `skill_bundle` packages: the sorted subset of skill names the manifest selected. Empty means "all". |
| `deployed_files` | list of strings | no | Project-relative paths APM wrote for this dep. Sorted. Powers `prune` and `audit`'s file-presence check. |
| `deployed_file_hashes` | map | no | `path -> sha256` for the files in `deployed_files`. Powers `audit`'s content-integrity check. Directory entries (trailing `/`) have no hash. |
| `exec_status` | string | no | Executable-trust state of this dep's executable primitives, set by the install-time gate via the shared deny-wins resolver. One of `deployed` (trusted and materialized), `gated_pending_approval` (present but parked until approved), `denied` (blocked by an org/user deny), or `absent` (declares no executables). Consumed by `audit`'s `required-executable-untrusted` signal; see [Executable approval](../cli/approve/). |
| `source` | string | no | `"local"` for path dependencies, `"registry"` for dedicated-registry resolutions. Absent for Git deps. |
| `resolved_url` | string | registry only | Fully-qualified download URL used to re-fetch registry archives. |
| `resolved_hash` | string | registry only | SHA-256 digest of the registry archive bytes, verified on every install. |
| `local_path` | string | no | Original path from `apm.yml` for local deps, relative to project root. |
| `content_hash` | string | no | SHA-256 of the local package's source tree. Lets APM detect upstream changes to a path dep. |
| `is_dev` | bool | no | `true` when the dep was declared under `devDependencies`. |
| `discovered_via` | string | no | Marketplace name that surfaced this package (provenance). |
| `marketplace_plugin_name` | string | no | Plugin name as listed in that marketplace. |
| `source_url` | string | no | Canonical marketplace source URL when the package came from a hosted `marketplace.json` catalog. |
| `source_digest` | string | no | `sha256:<hex>` digest of the hosted `marketplace.json` bytes used for resolution. |
| `is_insecure` | bool | no | `true` when the source URL was `http://`. |
| `allow_insecure` | bool | no | `true` when the manifest explicitly opted in to the insecure source. |
| `constraint` | string | git-source semver only | The original semver range from `apm.yml` (`^1.2.0`, `~1.4`). Present when `ref:` was a range; used by drift detection so a manifest range vs. a locked tag (`v1.5.3`) is not a false positive, and by lockfile replay to pin the resolved tag deterministically across installs. |
| `resolved_tag` | string | git-source semver or SHA-pin updates | The concrete annotated git tag (`v1.5.3`, `widget--v1.5.3`) that satisfied `constraint` or justified the latest full-SHA revision-pin update. |
| `resolved_at` | string | git-source semver only | RFC 3339 timestamp of the resolution. Surfaces "how stale is this pin?" in `apm why`. |
| `declared_license` | string | no | The license the package *manifest declares* (`license:` in `apm.yml`, or `license` in a `plugin.json`), recorded verbatim at resolve time and syntax-validated offline against the bundled SPDX id set. An author **claim**, not a conclusion from `LICENSE` text -- APM never reads the license file. Omitted when undeclared (absence means unknown; no sentinel is stored). Surfaced by `apm lock export`. |

Fields are emitted only when set. A minimal entry is just `repo_url` plus
`resolved_commit`.

## Lockfile identity keys

Lockfile dependency keys keep `github.com` implicit for migration stability:
existing `github.com` entries remain keyed as `owner/repo`. Local dependencies
use `local_path` directly. Virtual dependencies append `virtual_path` to the
base repo key. Entries for non-default hosts prefix the key with the lowercased
host (`host/owner/repo`), so `github.com/team/skills` and
`gitea.myorg.com/team/skills` can coexist without overwriting each other, and
host casing cannot create duplicate keys. Registry-proxy entries keep the bare
logical key because the proxy host is transport, not package identity.

## Self entry

A project that ships its own primitives (skills, agents, prompts under
`.github/`, `.claude/`, etc.) records the files it deploys to its own targets
under `local_deployed_files` and `local_deployed_file_hashes` at the top
level.

Internally, when the lockfile is loaded, APM synthesizes a virtual dependency
entry keyed by `"."` so that orphan detection, audit, and prune can iterate
all "owned" files uniformly. This synthesized entry has:

- `repo_url: <self>`
- `source: local`
- `local_path: "."`
- `depth: 0`
- `is_dev: true`
- `deployed_files` and `deployed_file_hashes` copied from the top-level
  `local_deployed_*` fields.

The synthesized entry is **not** written back to YAML - the flat
`local_deployed_*` fields remain the on-disk source of truth. Treat the self
entry as an implementation detail; do not author it by hand.

## Version bumping

The lock file uses two schema versions:

| Version | Triggered by | Adds |
|---|---|---|
| `"1"` | Default for Git-only projects. | Baseline schema. |
| `"2"` | Any dependency with `source: "registry"`. | `resolved_url`, `resolved_hash`, and the `version` field on registry entries. |

The bump is **opportunistic**: a project that never opts into a registry keeps
`lockfile_version: "1"` forever, even on a newer client. The first registry
dep added to the graph promotes the lockfile to `"2"`; if every registry dep is
later removed, the next write demotes back to `"1"`. Both versions are valid
on-disk formats; consumers MUST handle either.

For the registry workflow this enables, see the [Registries guide](../guides/registries/).

## Pack section

When a project is packed with `apm pack`, the bundled lockfile is enriched
with a top-level `pack:` block:

```yaml
pack:
  format: apm           # or "plugin"
  target: copilot       # or comma-joined list, or "all"
  packed_at: "2026-05-10T20:14:00+00:00"
  mapped_from:          # only when cross-target path remapping happened
    - .claude/skills/
  bundle_files:         # only for plugin bundles
    skills/my-skill/SKILL.md: "a1b2..."
```

The pack block is read by `apm unpack` to verify bundle integrity and to
restore correct target paths. It is stripped from project lockfiles and only
appears inside packed bundles.

`local_deployed_files` and `local_deployed_file_hashes` are stripped from
bundle lockfiles - they describe the packager's own repo, which is not
shipped.

## Lifecycle

| Command | Reads | Writes |
|---|---|---|
| `apm install` | existing lockfile (for `--frozen` and incremental reuse) | full rewrite on resolution change |
| `apm install --frozen` | required | never writes; fails on missing pin |
| `apm compile` | yes (resolution + integrity) | no |
| `apm audit` | yes | no |
| `apm prune` | yes (to identify orphans) | yes (after removing orphans) |
| `apm view --lock` | yes | no |
| `apm unpack` | bundle's pack-enriched lockfile | merges into project lockfile |

`apm install` only rewrites the file when its semantic content changes
(`generated_at` and `apm_version` are ignored when comparing). A no-op install
leaves the file untouched.

## Drift and integrity

The lockfile is what `apm audit` compares the workspace against. Each baseline
check maps to specific lockfile fields:

| Check | Backed by |
|---|---|
| `lockfile-exists` | file presence at project root |
| `ref-consistency` | `resolved_ref` per entry vs. `apm.yml` |
| `deployed-files-present` | `deployed_files` per entry (and self entry) |
| `content-integrity` | `deployed_file_hashes` (and `local_deployed_file_hashes`) |
| `skill-subset-consistency` | `skill_subset` per `skill_bundle` entry |
| `config-consistency` | `mcp_servers` and `mcp_configs` |
| `no-orphaned-packages` | `dependencies` keys vs. `apm.yml` |

Files listed in `deployed_files` without a corresponding hash entry (typically
directory markers ending in `/`) are skipped by content-integrity. Missing
files are reported by `deployed-files-present`, not by content-integrity, so
the two checks do not double-count.

Orphan detection works in two directions:

- **Orphan packages** - entries in `dependencies` that the manifest no longer
  declares. `apm prune` removes them and their `deployed_files`.
- **Orphan files** - files under managed target directories that no lockfile
  entry claims. `apm prune` removes them too.

## Versioning

`lockfile_version` is the schema version of the file format itself.

- The current versions are `"1"` and `"2"`; version `"2"` is emitted for registry-sourced or git-semver-resolved dependencies.
- APM additively extends entries within each version - new optional fields
  may appear without bumping the version. Older APM clients ignore unknown
  fields.
- Breaking changes (renames, removals, semantic shifts) require bumping
  `lockfile_version`. APM refuses to operate on a lockfile whose version it
  does not recognize, and will instruct the user to upgrade or regenerate.

A lockfile that fails to parse is treated as absent - APM logs the error and,
for non-frozen installs, proceeds to regenerate from `apm.yml`.

## Example

A small project with one remote APM package, one MCP server, and its own
local skill:

```yaml
lockfile_version: "1"
generated_at: "2026-05-10T20:14:00+00:00"
apm_version: "0.6.4"
dependencies:
  - repo_url: github.com/octocat/example-skills
    resolved_ref: v1.2.0
    resolved_commit: 7f3c9a4d2e1b8c7f0a9e6d5c4b3a2918f7e6d5c4
    version: 1.2.0
    package_type: skill_bundle
    depth: 1
    skill_subset:
      - code-review
      - test-writing
    deployed_files:
      - .github/skills/code-review/SKILL.md
      - .github/skills/test-writing/SKILL.md
    deployed_file_hashes:
      .github/skills/code-review/SKILL.md: "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
      .github/skills/test-writing/SKILL.md: "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"
mcp_servers:
  - github
mcp_configs:
  github:
    type: stdio
    command: docker
    args: ["run", "-i", "--rm", "ghcr.io/github/github-mcp-server"]
local_deployed_files:
  - .github/skills/my-local-skill/SKILL.md
local_deployed_file_hashes:
  .github/skills/my-local-skill/SKILL.md: "fcde2b2edba56bf408601fb721fe9b5c338d10ee429ea04fae5511b68fbf8fb9"
```

## See also

- [`apm install`](./cli/install/) - resolves and writes the lockfile
- [`apm audit`](./cli/audit/) - validates the workspace against the lockfile
- [`apm prune`](./cli/prune/) - removes orphan packages and files
- [`apm view`](./cli/view/) - inspect resolved state (`--lock`)
- [Baseline checks](./baseline-checks/) - the drift checks the lockfile feeds
- [Manifest schema](./manifest-schema/) - the `apm.yml` it pins
