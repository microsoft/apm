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

For the producer walkthrough and compatibility table, see
[Pack a bundle](../../../producer/pack-a-bundle/) and
[Agent Plugins v1 migration](../../../getting-started/agent-plugins-v1-migration/).
For the install-time trust boundary, see
[Security Model](../../../enterprise/security/#local-bundle-install-trust-model)
and [`apm audit`](../audit/).

- `dependencies:` mapping present -> a bundle (directory by default, or archive with `--archive`; see `--archive-format`). An explicit empty mapping (`dependencies: {}`) produces a bundle of the package's local content; an omitted or null `dependencies:` value does not.
- `marketplace:` block present -> selected marketplace artifacts.
- `target:` (or `targets:`) containing `copilot` -> a Copilot manifest. Claude sidecar generation runs only with `--claude-plugin`.
- Both blocks present -> bundle plus selected marketplace artifacts in a single run.

The bundle is built from `apm.lock.yaml`. An enriched copy of the lockfile (per-file SHA-256 in `bundle_files`, plus `pack:` metadata) is embedded inside the bundle so `apm install <bundle>` can verify integrity at install time.

Bundles are target-agnostic. The consumer's project decides where files land at install time -- the bundle carries no harness binding. Flags whose scope does not match the detected outputs are silent no-ops, not errors, so the same `apm pack` invocation works in CI across projects that produce only a bundle, only a marketplace, or both.

## Options

| Flag | Default | Description |
|---|---|---|
| `--plugin` | off | Explicitly request Agent Plugin v1 output. |
| `--claude-plugin` | off | Explicitly select the Claude-compatible output, which is also the current no-flag default. |
| `--format plugin\|agent-plugin\|claude-plugin\|apm` | `claude-plugin` | `plugin` and `agent-plugin` emit Agent Plugin v1. `claude-plugin` selects the Claude-compatible layout. `apm` emits the legacy APM bundle. |
| `--archive` | off | Produce a `.zip` archive instead of a directory (previous default: `.tar.gz`; use `--archive-format tar.gz` for legacy CI pipelines). Bundle only. |
| `--archive-format zip\|tar.gz` | `zip` | Archive format when `--archive` is set. `zip` is natively extractable on Windows and matches the format expected by Claude Code and plugin hosts. `tar.gz` is typically smaller for text-heavy bundles and preserves the previous default for pipelines that depend on it. |
| `-o`, `--output PATH` | `./build` | Bundle output directory. Does not affect the `marketplace.json` path. |
| `--force` | off | Allow overwriting on collision. In `plugin` bundle format, last writer wins instead of first; for generated `plugin.json` manifests, overwrites an existing file instead of preserving it. |
| `--dry-run` | off | Print what would be packed without writing anything. |
| `--verbose`, `-v` | off | Show per-file paths and detailed packer output. |
| `--offline` | off | Marketplace: resolve version ranges from cached refs only; skip `git ls-remote`. |
| `--include-prerelease` | off | Marketplace: allow pre-release tags to satisfy version ranges. |
| `-m`, `--marketplace FORMATS` | all configured | Comma-separated list of marketplace formats to build. Sentinels: `all` (every configured format), `none` (skip marketplace entirely). |
| `--marketplace-path FORMAT=PATH` | manifest default | Override the output path for a specific format. Repeatable. Example: `--marketplace-path codex=./dist/codex.json`. |
| `--json` | off | Emit machine-readable JSON to stdout. All logs move to stderr. Shape: `{ok, dry_run, warnings, errors, marketplace: {outputs: [...]}}`. |
| `--legacy-skill-paths` | off | Bundle skills under per-client paths (e.g. `.cursor/skills/`) instead of the converged `.agents/skills/`. Compatibility flag. |
| `--check-versions` | off | Release gate: verify per-package versions agree with the configured `marketplace.versioning.strategy` (`lockstep`, `tag_pattern`, or `per_package`). Exits `3` on misalignment. Composes with `--check-clean` and `--dry-run`. |
| `--check-clean` | off | Release gate: regenerate every configured marketplace output to a temp representation and diff against the same effective path used by `apm pack`, including `--marketplace-path` overrides. Exits `4` for drift. Combine with `--dry-run` to compare without normal pack output generation. |
| `--target`, `-t VALUE` | auto-detect | **Deprecated.** Recorded as informational `pack.target` metadata only; ignored by `apm install`. Will be removed in a future release. |

:::caution[Migrating automation from `.tar.gz`?]
`apm pack --archive` now produces `.zip`. If your CI release, checksum, or
upload step still matches `build/*.tar.gz`, add `--archive-format tar.gz` or
update the downstream glob to `.zip`.
:::

## Examples

### Bundle only

```bash
apm pack                              # plugin format (default), ./build/
apm pack --archive                    # plugin bundle as .zip (default)
apm pack --archive --archive-format tar.gz  # legacy CI: produce .tar.gz instead
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

### Agent Plugin bundle (`--plugin` or `--format plugin`)

`--plugin` and `--format plugin` emit an Agent Plugin v1 bundle. Its exact root schema selects canonical whole-plugin admission. APM currently blocks deployment because no selected target has a machine-verifiable, non-deprecated native lifecycle; it does not fall back to legacy primitive projection. The no-flag default remains the Claude-compatible bundle.

Contents (typical):

- `plugin.json` — Agent Plugins v1 manifest (synthesised from `apm.yml` when not authored). Note: `apm.yml` remains the authoring source; `plugin.json` is the produced artifact the host consumes.
- Converged content:
  - `skills/` — top-level skill bundles (one directory per named skill).
  - `com.microsoft.apm/agents/`, `com.microsoft.apm/commands/`, `com.microsoft.apm/instructions/`, `com.microsoft.apm/hooks/`, `com.microsoft.apm/extensions/` — the namespaced payload that holds primitive categories in the Agent Plugin namespace.
  - `com.microsoft.apm/lsp.json` when LSP servers are present.
- Optional root `mcp.json` / `.mcp.json` — producer-declared MCP metadata. Packed as bundle metadata and routed into each target's native MCP config at install time; the bundle's `.mcp.json` is not deployed verbatim.
- `apm.lock.yaml` at the bundle root — an enriched lockfile with `pack:` metadata and a `bundle_files` map of per-file SHA-256 digests used by `apm install` for install-time integrity verification.
- Optional docs copied from the project root when present: `README.md`, `LICENSE`, `CHANGELOG.md`.

Source and dependency rules:

- Local primitives are sourced from `.apm/` when present. Without `.apm/`, APM falls back to root convention directories such as `agents/`, `skills/`, `commands/`, and `hooks/`, but `.apm/` is preferred and root sources are skipped with an actionable warning when both exist. An explicit `includes:` list is exhaustive; missing or unpackable listed paths fail the pack instead of falling back.
- Installed dependencies are included only from lockfile-attested `deployed_files`; the `apm_modules` cache is never packed. Each attested file is verified against its recorded SHA-256 (`deployed_file_hashes`). If a dependency declares a `skills:` subset, only the named skills are included. A dependency with cached primitives but no `deployed_files` causes `apm pack` to fail and instruct you to run `apm install`.

Compatibility flags and migration notes:

- `--claude-plugin` preserves the historical Claude-compatible exporter and layout (the legacy Claude plugin manifest and directory shapes). Use this flag to remain fully compatible with Claude Code hosts that expect the older layout.
- `--plugin` and `--format plugin` map to the Agent Plugin v1 output (the new canonical format).
- `--format apm` emits the legacy APM bundle layout for tooling that still requires it.

Notes:

- `apm.yml` is the source-of-truth for authoring. `plugin.json` is an emitted artifact and may be regenerated by `apm pack` from `apm.yml`.
- The embedded `apm.lock.yaml` is the authoritative provenance and integrity record for the bundle content.
- The packer performs Agent Plugins schema validation and integrity checks; packing fails on missing provenance, unstated deployed files, or other conditions the exporter cannot satisfy, with actionable instructions to fix the source and retry.
- `devDependencies` are excluded.

### APM bundle (`--format apm`)

The legacy APM layout under `--output`. Files are copied preserving their install-time directory structure. Installed dependencies are packed exclusively from lockfile-attested `deployed_files`, and each file is verified against its `deployed_file_hashes` SHA-256 before it is copied (the same integrity gate the `plugin` format applies) -- a file whose bytes no longer match its recorded hash fails the pack with `... does not match the hash recorded in apm.lock.yaml`. Files with no recorded hash (older lockfiles) pack without verification. The bundle's `apm.lock.yaml` carries the same `pack:` metadata and `bundle_files` digests. The project's own `apm.lock.yaml` is never modified.

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

Configure marketplace artifact paths in `apm.yml` with the `marketplace.outputs` map, keyed by format. Use `--marketplace-path FORMAT=PATH` to override per-format output paths at pack time.

### Plugin manifests

By default, an Agent Plugin build emits only the target-driven Copilot sidecar. To generate the historical Claude sidecar and layout, run `apm pack --claude-plugin`. Use `targets: [claude, copilot]` together with `apm pack --claude-plugin` to emit both sidecars.

| Ecosystem | Output path |
|---|---|
| `claude` | `.claude-plugin/plugin.json` |
| `copilot` | `.github/plugin/plugin.json` |

Add one line to `apm.yml` and pack:

```yaml
# apm.yml
name: my-plugin
version: 1.0.0
target: claude
```

```bash
apm pack --claude-plugin   # writes .claude-plugin/plugin.json
```

Use `targets: [claude, copilot]` with `apm pack --claude-plugin` to emit both sidecars. A default Agent Plugin build emits only the Copilot sidecar.

`target:` and `targets:` are mutually exclusive: declaring both is a build error (exit `1`). An empty `targets:` list or an unrecognised ecosystem token is likewise rejected before any artifact is written.

The manifest is synthesised from `apm.yml` identity fields (`name`, `version`, `description`, `author`, `license`). Per-ecosystem differences:

- **Claude:** includes `mcpServers` sourced from `.mcp.json` when that file declares servers that survive credential stripping.
- **Copilot:** omits `mcpServers`.

#### Credential stripping (Claude `mcpServers`)

`.mcp.json` routinely embeds secrets that an MCP host injects at startup, so they are removed before the manifest is written -- a committed `plugin.json` never leaks them. Stripping is recursive and applies at any nesting depth:

- Credential-bearing keys are dropped: `env`/`environment`/`headers`/`authorization` blocks, plus any key whose name contains `token`, `secret`, `password`, `credential`, `apikey`, or `key`.
- Secret-shaped values are redacted even when the key name is innocuous: `user:pass@host` URL userinfo, inline `--token=...` flags, space-separated `--token value` pairs, shell `ENV=secret` prefixes, `Bearer`/`Basic` auth headers, and bare provider tokens (GitHub, OpenAI, Slack, AWS, Google, GitLab, npm, PyPI, HuggingFace, Stripe, SendGrid, Supabase, Databricks, and other recognised provider token prefixes) passed as positional `args`.

A warning lists everything dropped or redacted, led by the consequence (secrets withheld from commit).

#### Overwrite and dry-run

If a `plugin.json` already exists at the target path it is **preserved**: `apm pack` warns and skips the write. Re-run with `--force` to overwrite it (the same flag that governs bundle collisions). The `--dry-run` flag prevents any writes -- the manifest content is computed but not persisted.

:::note[Planned]
The generated manifest is intentionally minimal. Enrichment fields (`homepage`, `repository`, `keywords`, `author.url`) are planned for a follow-up release ([#1621](https://github.com/microsoft/apm/issues/1621)).
:::

Plugin manifest generation runs after BUNDLE and MARKETPLACE phases so the generated file is never accidentally included in the bundle export.

## Behavior

- **Lockfile-attested dependencies.** Dependency content is packed exclusively from lockfile `deployed_files` and verified against `deployed_file_hashes`; the `apm_modules` cache is never packed. If a dependency has cached primitives but no `deployed_files`, `apm pack` errors and tells you to run `apm install`.
- **Hidden-character scan.** Source files are scanned before bundling. Findings are reported as warnings only -- packing is non-blocking. Consumers are protected at install time, where critical findings block.
- **Empty bundle warning.** If no package files match after dependency resolution, `apm pack` emits a warning and exits `0` with an empty bundle. Missing dependency content is an error, not an empty bundle.
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
| `3` | `--check-versions` failed: per-package versions disagree with the configured marketplace versioning strategy. |
| `4` | `--check-clean` failed: marketplace working tree is dirty (regenerated output differs from on-disk file). |

## Related

- [`apm unpack`](../unpack/) -- inverse, deprecated; prefer `apm install <bundle>`.
- [`apm install`](../install/) -- consumer side; installs a packed bundle directory, `.zip`, or `.tar.gz`.
- [Pack a bundle (producer guide)](../../../producer/pack-a-bundle/) -- task-oriented walkthrough.
- [Agent Plugins v1 migration](../../../getting-started/agent-plugins-v1-migration/) -- compatibility table and migration guidance.
- [Publish to a marketplace](../../../producer/publish-to-a-marketplace/) -- end-to-end marketplace flow.
- [Lockfile spec](../../lockfile-spec/) -- `pack:` metadata and `bundle_files` schema.
