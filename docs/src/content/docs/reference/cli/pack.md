---
title: apm pack
description: Pack distributable artifacts (plugin bundle, APM bundle, or marketplace artifacts) from your APM project.
sidebar:
  order: 17
---

## Synopsis

```bash
apm pack [OPTIONS]
```

## Description

`apm pack` produces distributable artifacts from the current APM project. It reads `apm.yml` to decide what to emit:

- `dependencies:` block present -> a bundle (directory or `.tar.gz`).
- `marketplace:` block present -> selected marketplace artifacts.
- Both blocks present -> bundle plus selected marketplace artifacts in a single run.

The bundle is built from `apm.lock.yaml`. An enriched copy of the lockfile (per-file SHA-256 in `bundle_files`, plus `pack:` metadata) is embedded inside the bundle so `apm install <bundle>` can verify integrity at install time.

Bundles are target-agnostic. The consumer's project decides where files land at install time -- the bundle carries no harness binding. Flags whose scope does not match the detected outputs are silent no-ops, not errors, so the same `apm pack` invocation works in CI across projects that produce only a bundle, only a marketplace, or both.

## Options

| Flag | Default | Description |
|---|---|---|
| `--format plugin\|apm` | `plugin` | Bundle format. `plugin` emits a Claude Code plugin directory with `plugin.json` and plugin-native subdirs (`agents/`, `skills/`, `commands/`, `instructions/`, `hooks/`). `apm` emits the legacy APM bundle layout, kept for tooling that still consumes it (e.g. `microsoft/apm-action@v1` restore mode). |
| `--archive` | off | Produce a `.tar.gz` archive instead of a directory. Bundle only. |
| `-o`, `--output PATH` | `./build` | Bundle output directory. Does not affect the `marketplace.json` path. |
| `--force` | off | On collision in `plugin` format, last writer wins instead of first. Bundle only. |
| `--dry-run` | off | Print what would be packed without writing anything. |
| `--verbose`, `-v` | off | Show per-file paths and detailed packer output. |
| `--offline` | off | Marketplace: resolve version ranges from cached refs only; skip `git ls-remote`. |
| `--include-prerelease` | off | Marketplace: allow pre-release tags to satisfy version ranges. |
| `-m`, `--marketplace FORMATS` | all configured | Comma-separated list of marketplace formats to build. Sentinels: `all` (every configured format), `none` (skip marketplace entirely). |
| `--marketplace-path FORMAT=PATH` | manifest default | Override the output path for a specific format. Repeatable. Example: `--marketplace-path codex=./dist/codex.json`. |
| `--json` | off | Emit machine-readable JSON to stdout. All logs move to stderr. Shape: `{ok, dry_run, warnings, errors, marketplace: {outputs: [...]}}`. |
| `--marketplace-output PATH` | _(hidden)_ | **Deprecated.** Translates to `--marketplace-path claude=PATH` with a stderr warning. Will be removed in v0.15 (see #1318). |
| `--legacy-skill-paths` | off | Bundle skills under per-client paths (e.g. `.cursor/skills/`) instead of the converged `.agents/skills/`. Compatibility flag. |
| `--target`, `-t VALUE` | auto-detect | **Deprecated.** Recorded as informational `pack.target` metadata only; ignored by `apm install`. Will be removed in a future release. |

## Examples

### Bundle only

```bash
apm pack                              # plugin format (default), ./build/
apm pack --archive                    # plugin bundle as .tar.gz
apm pack --format apm -o ./dist       # legacy APM bundle layout
```

### Marketplace only

```bash
apm pack
apm pack --offline --dry-run

# Build only Claude format, output as JSON for CI:
apm pack --marketplace=claude --json

# Override codex output path:
apm pack --marketplace-path codex=./dist/codex-marketplace.json

# Build all formats, preview paths:
apm pack --marketplace=all --json | jq -r '.marketplace.outputs[].path'
```

### Both artifacts in one run

```bash
apm pack
apm pack --archive --offline
```

### Configure marketplace output paths

```yaml
marketplace:
  outputs:
    claude: {}
    codex:
      path: ./build/codex-marketplace.json
```

### Preview without writing

```bash
apm pack --dry-run
apm pack --archive --dry-run -v
```

## Output format

### Plugin bundle (`--format plugin`, default)

A Claude Code plugin directory under `--output`. Contains:

- `plugin.json` -- schema-conformant manifest. Convention-dir keys are stripped because Claude Code auto-discovers them.
- Plugin-native subdirs populated from your `.apm/` content and from installed dependencies: `agents/`, `skills/`, `commands/`, `instructions/`, `hooks/`.
- A merged `hooks.json` when multiple sources contribute hooks.
- `apm.lock.yaml` -- enriched copy with `pack:` metadata and a `bundle_files` map of per-file SHA-256 digests, used by `apm install` for install-time integrity verification.
- `devDependencies` are excluded.

### APM bundle (`--format apm`)

The legacy APM layout under `--output`. Files are copied preserving their install-time directory structure. The bundle's `apm.lock.yaml` carries the same `pack:` metadata and `bundle_files` digests. The project's own `apm.lock.yaml` is never modified.

Example enriched lockfile fragment:

```yaml
pack:
  format: apm
  packed_at: '2026-03-09T12:00:00+00:00'
  bundle_files:
    .github/agents/architect.md: a1b2c3...
lockfile_version: '1'
generated_at: ...
dependencies:
  - repo_url: owner/repo
```

### Marketplace artifacts

`.claude-plugin/marketplace.json` by default, plus any additional artifact selected by `marketplace.outputs` such as `.agents/plugins/marketplace.json` for Codex. Each remote plugin's version range is resolved against `git ls-remote`; local-path entries pass through verbatim. Files are written atomically, and parent directories are created if absent.

Configure marketplace artifact paths in `apm.yml`: `marketplace.claude.output` controls the Claude/Anthropic artifact, and `marketplace.codex.output` controls the Codex artifact. `--marketplace-output PATH` remains as a legacy Claude-only compatibility override; prefer manifest config for new projects and CI.

## Behavior

- **Lockfile-driven.** The bundle enumerates `deployed_files` from `apm.lock.yaml`. Run `apm install` first if the lockfile is stale or missing.
- **Hidden-character scan.** Source files are scanned before bundling. Findings are reported as warnings only -- packing is non-blocking. Consumers are protected at install time, where critical findings block.
- **Empty bundle warning.** If no files match (e.g. nothing was installed yet), `apm pack` emits a warning and exits `0` with an empty bundle. Verbose mode prints a hint to run `apm install` first.
- **Share line.** On success, `apm pack` prints `Share with: apm install <bundle-path>` so the produced bundle is immediately copy-pasteable.
- **Marketplace fallback.** With no `marketplace:` block in `apm.yml`, a legacy `marketplace.yml` file is read with a deprecation warning. Both files present is a hard error.
- **Marketplace outputs.** Configure via `marketplace.outputs` map (keyed by format). Claude is included by default. The legacy list form (`outputs: [claude]`) still parses with a deprecation warning. Use `--marketplace=` to filter which formats are built in a given invocation.
- **JSON mode.** `--json` makes `apm pack` machine-friendly: stdout is a single JSON object, all human-readable logs move to stderr. Combine with `--marketplace=` for selective CI matrix builds.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. Requested artifacts written (or, with `--dry-run`, planned). |
| `1` | Build or runtime error: network failure, ref not found, no tag matches a marketplace range, lockfile read error, or unhandled packer exception. |
| `2` | `apm.yml` schema validation error. |

## Related

- [`apm unpack`](../unpack/) -- inverse, deprecated; prefer `apm install <bundle>`.
- [`apm install`](../install/) -- consumer side; installs a packed bundle directory or `.tar.gz`.
- [Pack a bundle (producer guide)](../../../producer/pack-a-bundle/) -- task-oriented walkthrough.
- [Publish to a marketplace](../../../producer/publish-to-a-marketplace/) -- end-to-end marketplace flow.
- [Lockfile spec](../../lockfile-spec/) -- `pack:` metadata and `bundle_files` schema.
