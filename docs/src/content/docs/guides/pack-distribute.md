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
- **Air-gapped environments** — no network access at deploy time
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

Creates a self-contained bundle from installed dependencies. Reads the `deployed_files` manifest in `apm.lock.yaml` as the source of truth — it does not scan the disk.

```bash
# Default: apm format, target auto-detected from apm.yml
apm pack

# Filter by target
apm pack --target copilot         # only .github/ files
apm pack --target claude          # only .claude/ files
apm pack --target all             # all targets
apm pack -t claude,copilot        # multiple targets (comma-separated)

# Bundle format
apm pack --format plugin          # valid plugin directory structure

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
| `--format` | `apm` | Bundle format (`apm` or `plugin`) |
| `-t, --target` | auto-detect | File filter: `copilot`, `claude`, `cursor`, `opencode`, `all`. `vscode` is a deprecated alias for `copilot` |
| `--archive` | off | Produce `.tar.gz` instead of directory |
| `-o, --output` | `./build` | Output directory |
| `--dry-run` | off | List files without writing |
| `--force` | off | On collision (plugin format), last writer wins |

### Target filtering

The target flag controls which deployed files are included based on path prefix:

| Target | Includes |
|--------|----------|
| `copilot` | Paths starting with `.github/` |
| `vscode` | Deprecated alias for `copilot` |
| `claude` | Paths starting with `.claude/` |
| `cursor` | Paths starting with `.cursor/` |
| `opencode` | Paths starting with `.opencode/` |
| `all` | `.github/`, `.claude/`, `.cursor/`, and `.opencode/` |

When no target is specified, APM auto-detects from the `target` field in `apm.yml`, falling back to `all`.

### Cross-target path mapping

Skills and agents are semantically identical across targets -- `.github/skills/X` and `.claude/skills/X` contain the same content. When the lockfile records files under a different target prefix than the one you are packing for, APM automatically remaps `skills/` and `agents/` paths:

```
apm pack --target claude
# .github/skills/my-plugin/SKILL.md  ->  .claude/skills/my-plugin/SKILL.md
# .github/agents/helper.md           ->  .claude/agents/helper.md
```

Only `skills/` and `agents/` are remapped. Commands, instructions, and hooks are target-specific and are never mapped.

The enriched lockfile inside the bundle uses the remapped paths, so the bundle is self-consistent. When mapping occurs, the `pack:` section includes a `mapped_from` field listing the original prefixes.

### Targeting mental model

**Choose your target when you pack. Unpack delivers exactly what was packed.**

A bundle is a deployable snapshot, not a retargetable source artifact. Target selection happens at pack time because that is when the full context is available -- which file types are remappable (skills, agents) and which are target-specific (commands, instructions, hooks).

`apm unpack` does not remap paths. If the bundle was packed for Claude, the files land under `.claude/`. If you need a different target, re-pack from source with the desired `--target` flag, or use `--target all` to include all platforms.

When unpacking, APM reads the bundle's `pack:` metadata and shows the target it was packed for. If the bundle target does not match the project's detected target, a warning is displayed:

```
$ apm unpack team-skills.tar.gz
[*] Unpacking team-skills.tar.gz -> .
[i] Bundle target: claude (1 dep(s), 3 file(s))
[!] Bundle target 'claude' differs from project target 'copilot'
[+] Unpacked 3 file(s) (verified)
```

This is informational -- the files still extract. The warning helps users understand why their tool may not see the unpacked files and suggests the correct workflow.

## Bundle structure

The bundle mirrors the directory structure that `apm install` produces. It is not an intermediate format — extract it at the project root and the files land exactly where they belong.

Output is written to `./build/<name>-<version>/` by default, where name and version come from `apm.yml`.

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

## Plugin format

`apm pack --format plugin` transforms your project into a standalone plugin directory consumable by Copilot CLI, Claude Code, or other plugin hosts. The output contains no APM-specific files — no `apm.yml`, `apm_modules/`, `.apm/`, or `apm.lock.yaml`.

Use this when you want to distribute your APM package as a standalone plugin that works without APM.

```bash
apm pack --format plugin
```

### Output mapping

The exporter remaps `.apm/` content into plugin-native paths:

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

**Excluded from plugin output:** `devDependencies` are excluded from plugin bundles — see [devDependencies](../../reference/manifest-schema/#5-devdependencies).

### plugin.json generation

The bundle includes a `plugin.json`. If one already exists in the project (at the root, `.github/plugin/`, `.claude-plugin/`, or `.cursor-plugin/`), it is used and updated with component paths reflecting the output layout. Otherwise, APM synthesizes one from `apm.yml` metadata.

### devDependencies exclusion

Dependencies listed under [`devDependencies`](../../reference/manifest-schema/#5-devdependencies) in `apm.yml` are excluded from the plugin bundle. Use [`apm install --dev`](../../reference/cli-commands/#apm-install---install-dependencies-and-deploy-local-content) to add dev deps:

```bash
apm install --dev owner/test-helpers
```

This keeps development-only packages (test helpers, lint rules) out of distributed plugins.

### Example output

```
build/my-plugin-1.0.0/
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
  plugin.json
```

## Lockfile enrichment

The bundle includes a copy of `apm.lock.yaml` enriched with a `pack:` section. The project's own `apm.lock.yaml` is never modified.

```yaml
pack:
  format: apm
  target: copilot
  packed_at: '2025-07-14T09:30:00+00:00'
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

The `pack:` section records:

- **format** — the bundle format used (`apm` or `plugin`)
- **target** — the effective target filter applied
- **packed_at** — UTC timestamp of when the bundle was created

This metadata lets consumers verify what they received and trace it back to a build.

## `apm unpack`

Extracts an APM bundle into a project directory. Accepts both `.tar.gz` archives and unpacked bundle directories.

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

Resolve once in a setup job, fan out to N consumer jobs. No APM installation in downstream jobs.

```yaml
# .github/workflows/ci.yml
jobs:
  setup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
      - run: apm pack --archive
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
      # Prompts and agents are now in place — no APM needed
```

### Agentic workflows

GitHub's agentic workflow runners operate in sandboxed environments with no network access. Pre-pack the bundle and include it as a workflow artifact so the agent has full context from the start.

### Release audit trail

Attach the bundle as a release artifact. Anyone auditing the release can inspect exactly which prompts, agents, and skills shipped with that version.

```bash
apm pack --archive -o ./release-artifacts/
gh release upload v1.2.0 ./release-artifacts/*.tar.gz
```

### Dev Containers and Codespaces

Include a pre-built bundle in the dev container image or restore it during `onCreateCommand`. New contributors get working AI context without running `apm install`.

```json
{
  "onCreateCommand": "tar xzf .devcontainer/apm-bundle.tar.gz -C ."
}
```

### Org-wide distribution

A central platform team maintains the canonical prompt library. Monthly, they run `apm install && apm pack --archive`, publish the bundle to an internal artifact registry, and downstream repos pull it during CI or onboarding.

## `apm-action` integration

The official [apm-action](https://github.com/microsoft/apm-action) supports pack and restore as first-class modes.

### Pack mode

Generate a bundle as part of a GitHub Actions workflow:

```yaml
- uses: microsoft/apm-action@v1
  with:
    pack: true
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
2. The `--target` filter matches where files were deployed. For example, if files are under `.github/` but you pack with `--target claude`, APM will remap `skills/` and `agents/` automatically. If no remappable files exist, the bundle will be empty. Try `--target all` or check `apm.lock.yaml` to see which prefixes your files use.
