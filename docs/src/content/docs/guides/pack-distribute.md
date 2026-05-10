---
title: "Pack & Distribute"
description: "Bundle resolved dependencies for offline distribution, CI pipelines, and air-gapped environments."
sidebar:
  order: 6
---

Bundle your resolved APM dependencies into a portable artifact that can be distributed, cached, and consumed without APM, Python, or network access.

## Why bundles?

Every CI job that runs `apm install` pays the same tax: install APM, authenticate against GitHub, clone N repositories, compile prompts. Multiply that across a matrix of jobs, nightly builds, and staging environments and the cost adds up fast.

A bundle removes all of that. You resolve once, pack the output, and distribute the artifact. Consumers extract it and get the exact files that `apm install` would have produced — no toolchain required.

Common motivations:

- **CI cost reduction** — resolve once, fan out to many jobs
- **Air-gapped environments** — no network access at deploy time (for environments where CI *can* reach an internal proxy, see [Registry Proxy & Air-gapped](../../enterprise/registry-proxy/) -- bundles are the offline-delivery story; the proxy is the online-routing story)
- **Reproducibility** — the bundle is a snapshot of exactly what was resolved
- **Faster onboarding** — new contributors get pre-built context without running install
- **Audit trail** — attach the bundle to a release for traceability

## The pipeline

The pack/distribute workflow fits between install and consumption:

```
apm install  ->  apm pack  ->  upload artifact  ->  download  ->  apm unpack (or tar xzf)
```

The left side (install, pack) runs where APM is available. The right side (download, unpack) runs anywhere — a CI job, a dev container, a colleague's laptop. The bundle is the boundary.

## `apm pack`

Creates a self-contained bundle from installed dependencies. Reads the `deployed_files` manifest in `apm.lock.yaml` as the source of truth -- it does not scan the disk.

```bash
# Default: target-agnostic plugin bundle that installs into any consumer
apm pack

# Legacy APM bundle layout (consumed by microsoft/apm-action restore)
apm pack --format apm

# Produce a .tar.gz archive
apm pack --archive

# Custom output directory (default: ./build)
apm pack -o ./dist/

# Preview without writing
apm pack --dry-run
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--format` | `plugin` | Bundle format. `plugin` emits a Claude Code plugin directory with `plugin.json`. `apm` emits the legacy APM bundle layout. |
| `-t, --target` | (deprecated) | Deprecated. Emits a warning; the value is recorded in `pack.target` as diagnostic metadata only and is ignored by `apm install` target resolution. Bundles are target-agnostic; the consumer's project decides where files land at install time. |
| `--archive` | off | Produce `.tar.gz` instead of directory |
| `-o, --output` | `./build` | Output directory |
| `--dry-run` | off | List files without writing |
| `--force` | off | On collision (plugin format), last writer wins |

### Plugin layout normalization

`apm pack` (default `--format plugin`) emits an Anthropic plugin directory regardless of which targets installed the source files. Skills and agents are semantically identical across targets, so APM normalizes paths into the plugin convention:

```
.github/skills/my-plugin/SKILL.md  ->  skills/my-plugin/SKILL.md
.claude/agents/helper.md           ->  agents/helper.md
```

Commands, instructions, and hooks are also rehomed under the plugin's top-level convention dirs. The bundle is self-consistent and target-agnostic; the consumer's project drives where files land at install time.

### Targeting mental model

**Bundles are target-agnostic. The consumer's project decides where the files land.**

A bundle ships in Anthropic plugin layout (`agents/`, `skills/`, `commands/`, `instructions/`, `hooks/`) as a transport convention -- not a target binding. When a consumer runs `apm install <bundle>`, APM resolves the consumer's target from their project context (same precedence as registry installs: `--target` flag, then `apm.yml`, then directory detection) and routes the bundle's primitives through the integrators for that target.

Concretely: the same `team-skills.tgz` installed into a Copilot project lands under `.github/`; installed into a Claude project, lands under `.claude/`; installed into an OpenCode project, lands under `.opencode/` with instructions staged for `apm compile`.

`--target` on `apm pack` is **deprecated**. The field is informational and never overrides consumer-side target resolution; an advisory warning may still print at install time if the bundle's recorded `pack.target` differs from the resolved install target.

Compile-only targets (OpenCode, Codex, Gemini) receive instructions under `apm_modules/<slug>/.apm/instructions/` so [`apm compile`](../../guides/compilation/) merges them into `AGENTS.md` / `GEMINI.md` on the next compile.

```
$ apm install team-skills.tgz
[>] Installing local bundle from team-skills.tgz
[*] Installed 3 file(s) from local bundle
[!] Bundle staged 1 instruction(s) for compile (target: opencode). Run 'apm compile' to merge them into AGENTS.md / GEMINI.md / equivalent.
```

## Plugin format vs APM format

`apm pack` produces one of two output shapes. The default is the plugin format.

| Aspect | Plugin format (default) | APM format (`--format apm`) |
|---|---|---|
| Output layout | Claude Code plugin directory with `plugin.json` at the root and convention dirs (`agents/`, `skills/`, `commands/`, `instructions/`, `hooks/`) | Mirrors `apm install` deploy paths (`.github/`, `.claude/`, `.cursor/`, `.opencode/`) plus an enriched `apm.lock.yaml` |
| `plugin.json` | Synthesized (or updated from existing) and validates against the [official Claude Code plugin manifest schema](https://json.schemastore.org/claude-code-plugin.json) | Not emitted |
| `apm.lock.yaml` inside output | Enriched copy with a `pack:` metadata section (when the project has a lockfile) | Enriched copy with a `pack:` metadata section |
| Drop-in for | Any Claude Code plugin consumer (Copilot CLI, Claude Code, Cursor, ...) | `microsoft/apm-action`'s restore mode and bundle-aware tooling |
| `devDependencies` | Excluded | Included (full install layout) |

Pick `--format apm` when a downstream consumer expects the enriched lockfile and the install-shape directory tree -- in particular `microsoft/apm-action@v1` with `bundle:` (its restore mode reads the bundle's `apm.lock.yaml`). The action exposes `--format apm` end-to-end so existing pack/restore workflows continue unchanged. Otherwise leave the default in place.

## Without APM: what you give up

A plugin bundle works two ways: with APM, or without it. Both are supported. Pick the one that matches the consumer.

| Concern | With APM (`apm install`) | Without APM (host's native plugin loader) |
|---|---|---|
| Dependency declaration | `apm.yml` | None - copy the bundle directly |
| Version locking | `apm.lock.yaml` pins exact commits | None - whatever bytes you copied |
| Transitive dependencies | Resolved automatically | Not resolved - bundle whatever the author shipped |
| Governance hooks | `apm install` runs policy + security scans | Trust the source |
| Security scanning | Built-in: install / compile / unpack block critical findings; `apm audit` for reports | None at install time |
| Cross-runtime deploy | One install, all detected runtimes | One bundle per host, manually placed |
| Reproducibility | Same `apm.lock.yaml` -> identical bytes everywhere | Copy-and-pray |

The parallel: `apm install <skill>` is to `npx skills add <skill>` what `npm install` is to `npx`. Both work. The first is reproducible and governed; the second is convenient.

### Where the bundle goes without APM

`apm pack` writes a directory shaped like a standard plugin. The consumer side depends on the host:

- **Claude Code** loads plugins from `~/.claude/plugins/<name>/` (or via a Claude marketplace entry and `/plugin install`). Convention dirs (`agents/`, `skills/`, `commands/`, `instructions/`, `hooks/`) are picked up automatically.
- **Other Claude-plugin-compatible hosts** follow their own install steps. The bundle conforms to the [official Claude Code plugin manifest schema](https://json.schemastore.org/claude-code-plugin.json); consult your host's plugin documentation for the install path.
- **Archive output (`apm pack --archive`)** must be extracted first (`tar xzf <name>-<version>.tar.gz`), then copied into the host's plugin directory.

If your consumer runs APM, none of this applies - declare the package in `apm.yml`, run `apm install`, and APM handles discovery, deployment, locking, and scanning.

## Bundle structure (plugin format, default)

`apm pack` writes to `./build/<name>-<version>/` by default. Convention directories (`agents/`, `skills/`, `commands/`, `instructions/`, `hooks/`) are auto-discovered by Claude Code, so the synthesized `plugin.json` does NOT emit `agents`/`skills`/`commands`/`instructions` keys for them. Per the [official schema](https://json.schemastore.org/claude-code-plugin.json), those array entries are reserved for `./*.md` paths to *additional* files outside the convention directories.

### Single plugin per repo

```
build/my-plugin-1.0.0/
  plugin.json                              # schema-conformant, synthesized from apm.yml
  agents/
    architect.agent.md
  skills/
    security-scan/
      SKILL.md
  commands/
    review.md
  instructions/
    coding-standards.instructions.md
  hooks.json
```

`.apm/` source content is remapped into plugin-native paths:

| APM source | Plugin output |
|---|---|
| `.apm/agents/*.agent.md` | `agents/*.agent.md` |
| `.apm/skills/*/SKILL.md` | `skills/*/SKILL.md` |
| `.apm/prompts/*.prompt.md` | `commands/*.md` |
| `.apm/prompts/*.md` | `commands/*.md` |
| `.apm/instructions/*.instructions.md` | `instructions/*.instructions.md` |
| `.apm/hooks/*.json` | `hooks.json` (merged) |
| `.apm/commands/*.md` | `commands/*.md` |

Prompt files are renamed: `review.prompt.md` becomes `review.md` in `commands/`.

### `plugin.json` generation

If a `plugin.json` already exists in the project (root, `.github/plugin/`, `.claude-plugin/`, or `.cursor-plugin/`), it is reused. Stale `agents`/`skills`/`commands`/`instructions` keys that point at the convention directories are stripped so the output validates against the schema. Otherwise APM synthesizes one from `apm.yml` metadata.

### Multi-plugin repo (with `marketplace:` block)

When `apm.yml` declares a `marketplace:` block, `apm pack` ALSO emits `.claude-plugin/marketplace.json` aggregating each declared package as a marketplace entry. Curators have two options:

- **Per-plugin `plugin.json` files**: run `apm pack` per subdirectory (each subdirectory has its own `apm.yml`) to produce a schema-conformant `plugin.json` for every plugin.
- **Marketplace pass-through**: with `strict: false` on entries, the marketplace entry's pass-through fields (`description`, `version`, `author`, ...) stand in for the plugin manifest -- consumers read them directly from `marketplace.json`.

See [Authoring a marketplace](./marketplace-authoring/) for the full schema and build flow.

### `devDependencies` exclusion

Dependencies listed under [`devDependencies`](../../reference/manifest-schema/#5-devdependencies) in `apm.yml` are excluded from the plugin bundle. Use [`apm install --dev`](../../reference/cli-commands/#apm-install---install-dependencies-and-deploy-local-content) to add dev deps:

```bash
apm install --dev owner/test-helpers
```

This keeps third-party development-only packages (test helpers, lint rules) out of distributed plugins.

**Caveat for primitives you author yourself:** the dev/prod split is enforced via the lockfile's `is_dev` marker for resolved dependencies. The local-content scanner that ships your own `.apm/` content does NOT consult that marker -- it bundles everything under `.apm/`. To keep maintainer-only primitives (release-checklist skills, internal debugging agents) out of plugin bundles, author them OUTSIDE `.apm/` (e.g. under `dev/`) and reference them via a local-path devDependency. See [Dev-only Primitives](./dev-only-primitives/).

## Bundle structure (APM format, `--format apm`)

`apm pack --format apm` mirrors the directory structure that `apm install` produces. It is not an intermediate format -- extract it at the project root and the files land exactly where they belong. Use this format when a consumer (e.g. `microsoft/apm-action@v1` restore mode) needs the enriched lockfile alongside the deployed files.

### VS Code / Copilot target

```
build/my-project-1.0.0/
  .github/
    prompts/
      design-review.prompt.md
      code-quality.prompt.md
    agents/
      architect.md
    skills/
      security-scan/
        skill.md
  apm.lock.yaml                         # enriched copy (see below)
```

### Claude target

```
build/my-project-1.0.0/
  .claude/
    commands/
      review.md
      debug.md
    skills/
      code-analysis/
        skill.md
  apm.lock.yaml
```

### All targets

```
build/my-project-1.0.0/
  .github/
    prompts/
      ...
    agents/
      ...
  .claude/
    commands/
      ...
  .cursor/
    rules/
      ...
    agents/
      ...
  .opencode/
    agents/
      ...
    commands/
      ...
  apm.lock.yaml
```

The bundle is self-describing: its `apm.lock.yaml` lists every file it contains and the dependency graph that produced them.

## Lockfile enrichment

Both formats embed an enriched `apm.lock.yaml` in the bundle when the project has a lockfile. The project's own `apm.lock.yaml` is never modified; the embedded copy carries an additional `pack:` section so consumers verify integrity at install time without re-running the upstream pack.

```yaml
pack:
  format: apm
  packed_at: '2025-07-14T09:30:00+00:00'
  bundle_files:
    .github/prompts/design-review.prompt.md: a1b2c3...
    .github/agents/architect.md: d4e5f6...
lockfile_version: '1'
generated_at: '2025-07-14T09:28:00+00:00'
apm_version: '0.5.0'
dependencies:
  - repo_url: microsoft/apm-sample-package
    host: github.com
    resolved_commit: a1b2c3d4
    resolved_ref: main
    version: 1.0.0
    depth: 1
    package_type: apm
    deployed_files:
      - .github/prompts/design-review.prompt.md
      - .github/agents/architect.md
```

The `pack:` section records the bundle `format`, the per-file `bundle_files` SHA-256 manifest, and a `packed_at` UTC timestamp.

## `apm unpack`

:::note
For APM consumers, prefer `apm install <bundle>` over `apm unpack`. `apm install` deploys both formats target-agnostically, persists provenance to the project lockfile (`local_deployed_files`), and works with directory or `.tar.gz` inputs. `apm unpack` is retained for the legacy APM-format restore-without-APM workflow consumed by `microsoft/apm-action@v1`.
:::

Extracts an APM bundle (produced with `--format apm`) into a project directory. Accepts both `.tar.gz` archives and unpacked bundle directories. Plugin-format output is consumed directly by Claude Code and other plugin hosts and does not need `apm unpack`.

```bash
# Extract and verify
apm unpack ./build/my-project-1.0.0.tar.gz

# Extract to a specific directory
apm unpack ./build/my-project-1.0.0.tar.gz -o ./

# Skip integrity check
apm unpack --skip-verify ./build/my-project-1.0.0.tar.gz

# Preview without writing
apm unpack ./build/my-project-1.0.0.tar.gz --dry-run
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `-o, --output` | `.` (current dir) | Target project directory |
| `--skip-verify` | off | Skip completeness check against lockfile |
| `--dry-run` | off | List files without writing |
| `--force` | off | Deploy despite critical hidden-character findings |

### Behavior

- **Additive-only**: `unpack` writes files listed in the bundle's lockfile. It never deletes existing files in the target directory.
- **Overwrite on conflict**: if a file already exists at the target path, the bundle file wins.
- **Verification**: by default, `unpack` checks that every path in the bundle's `deployed_files` manifest exists in the bundle before extracting. Pass `--skip-verify` to skip this check for partial bundles.
- **Lockfile not copied**: the bundle's enriched `apm.lock.yaml` is metadata for verification only — it is not written to the output directory.

## Consumption scenarios

### CI: cross-job artifact sharing

Resolve once in a setup job, fan out to N consumer jobs. No APM installation in downstream jobs. Use `--format apm` so the bundle preserves the `apm install` directory layout that `tar xzf` restores in place.

```yaml
# .github/workflows/ci.yml
jobs:
  setup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
      - run: apm pack --format apm --archive
      - uses: actions/upload-artifact@v4
        with:
          name: apm-bundle
          path: build/*.tar.gz

  test:
    needs: setup
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: apm-bundle
          path: ./bundle
      - run: tar xzf ./bundle/*.tar.gz -C .
      # Prompts and agents are now in place -- no APM needed
```

### Agentic workflows

GitHub's agentic workflow runners operate in sandboxed environments with no network access. Pre-pack the bundle (`--format apm --archive`) and include it as a workflow artifact so the agent has full context from the start.

### Release audit trail

Attach the bundle as a release artifact. Anyone auditing the release can inspect exactly which prompts, agents, and skills shipped with that version.

```bash
apm pack --format apm --archive -o ./release-artifacts/
gh release upload v1.2.0 ./release-artifacts/*.tar.gz
```

### Dev Containers and Codespaces

Include a pre-built APM bundle in the dev container image or restore it during `onCreateCommand`. New contributors get working AI context without running `apm install`.

```json
{
  "onCreateCommand": "tar xzf .devcontainer/apm-bundle.tar.gz -C ."
}
```

### Org-wide distribution

A central platform team maintains the canonical prompt library. Monthly, they run `apm install && apm pack --format apm --archive`, publish the bundle to an internal artifact registry, and downstream repos pull it during CI or onboarding.

## `apm-action` integration

The official [apm-action](https://github.com/microsoft/apm-action) supports pack and restore as first-class modes. The action's restore mode consumes the legacy APM bundle layout, so its pack mode emits `--format apm` by default.

### Pack mode

Generate an APM-format bundle as part of a GitHub Actions workflow:

```yaml
- uses: microsoft/apm-action@v1
  with:
    pack: true        # produces --format apm bundle for restore-mode consumers
```

### Restore mode

Consume a bundle without installing APM. The action extracts the archive directly:

```yaml
- uses: microsoft/apm-action@v1
  with:
    bundle: ./path/to/bundle.tar.gz
```

No APM binary, no Python runtime, no network calls. The action handles extraction and verification internally.

## Prerequisites

`apm pack` requires two things:

1. **`apm.lock.yaml`** — the resolved lockfile produced by `apm install`. Pack reads the `deployed_files` manifest from this file to know what to include.
2. **Installed files on disk** — the actual files referenced in `deployed_files` must exist at their expected paths. Pack verifies this and fails with a clear error if files are missing.
3. **No local path dependencies** — `apm pack` rejects packages that depend on local filesystem paths (`./path` or `/absolute/path`). Replace local dependencies with remote references before packing.

The typical sequence is:

```bash
apm install     # resolve dependencies and deploy files
apm pack        # bundle the deployed files
```

Pack reads from the lockfile, not from a disk scan. If a file exists on disk but is not listed in `apm.lock.yaml`, it will not be included. If a file is listed in `apm.lock.yaml` but missing from disk, pack will fail and prompt you to re-run `apm install`.

## Troubleshooting

### "apm.lock.yaml not found"

Pack requires a lockfile. Run `apm install` first to resolve dependencies and generate `apm.lock.yaml`.

### "deployed files are missing on disk"

The lockfile references files that do not exist. This usually means dependencies were installed but the files were deleted. Run `apm install` to restore them.

### "bundle verification failed"

During unpack, verification found files listed in the bundle's lockfile that are missing from the bundle itself. The bundle may have been created from a partial install or corrupted during transfer. Re-pack from a clean install, or pass `--skip-verify` if you know the bundle is intentionally partial.

### Empty bundle

If `apm pack` produces zero files, check:

1. Your dependencies have `deployed_files` entries in `apm.lock.yaml`. This can happen if `apm install` completed but no integration files were deployed (e.g., the package has no prompts or agents for the active target).
2. The bundle is built from the `deployed_files` in `apm.lock.yaml` directly. Cross-target remapping for the convention dirs (`skills/`, `agents/`, `commands/`, `instructions/`, `hooks/`) runs automatically. If `apm.lock.yaml` shows zero deployed files, run `apm install` first; if files exist there but the bundle is empty, file an issue.
