---
title: "Authoring a marketplace"
description: Create and maintain an APM marketplace that stays in sync with Anthropic's marketplace.json standard.
sidebar:
  order: 6
---

This guide is for **marketplace maintainers** -- the people who curate a set of plugin packages for their team or organisation. If you are a consumer installing plugins from an existing marketplace, see the [Marketplaces guide](../marketplaces/) instead.

APM gives you a two-file authoring model:

- `marketplace.yml` -- source of truth, hand-edited, expressive (version ranges, tag patterns, prereleases).
- `marketplace.json` -- compiled artefact, byte-for-byte compliant with Anthropic's `marketplace.json` standard, consumed by Claude Code, Copilot CLI, and APM itself.

Both files are committed to git. `marketplace.yml` is edited; `marketplace.json` is regenerated with `apm marketplace build`.

## Anthropic compliance

`marketplace.json` produced by `apm marketplace build` conforms to [Anthropic's marketplace.json specification](https://docs.claude.com/en/docs/claude-code/plugin-marketplaces). The compiler follows three rules:

1. **`plugins:` is emitted verbatim.** APM does not rename, reorder, or decorate plugin entries. The Anthropic-defined key name (`plugins`) is used as-is.
2. **`metadata:` is an opaque pass-through.** Whatever you put under `metadata:` in `marketplace.yml` is copied byte-for-byte into `marketplace.json`, preserving key casing (for example, `pluginRoot` stays `pluginRoot`). This means extensions to Anthropic's schema (new metadata fields) usually do not need an APM code change.
3. **APM-only fields are stripped at compile time.** The `build:` block, per-package `version` ranges, `tagPattern` overrides, and `includePrerelease` flags live only in `marketplace.yml`. They never leak into `marketplace.json`.

APM does not emit a `versions[]` array. Each compiled plugin has exactly one resolved `source.ref` -- the latest commit SHA (or explicit ref) that satisfies the declared range at build time. Consumers pin to that single resolved ref.

:::caution[Experimental Feature]
Marketplace authoring commands are behind an experimental flag. Enable it once before following this guide:

```bash
apm experimental enable marketplace-authoring
```

See [Experimental Flags](../../reference/experimental/) for details.
:::

## Quickstart

```bash
# 1. Scaffold a marketplace.yml
apm marketplace init

# 2. Edit marketplace.yml -- add your packages, owner, metadata
$EDITOR marketplace.yml

# 3. Compile to marketplace.json
apm marketplace build

# 4. Commit BOTH files
git add marketplace.yml marketplace.json
git commit -m "Initial marketplace"
git push
```

Consumers now register your repository with `apm marketplace add <owner>/<repo>` and install packages from it.

## The marketplace.yml schema

Full example:

```yaml
name: my-marketplace
description: Curated plugins for the acme-org engineering team
version: 1.2.0

owner:
  name: acme-org
  url: https://github.com/acme-org
  email: maintainers@acme-org.example

# APM-only: stripped from marketplace.json at compile time.
build:
  tagPattern: "v{version}"

# Pass-through: copied verbatim into marketplace.json.
metadata:
  homepage: https://example.com/plugins
  pluginRoot: ./plugins

packages:
  - name: example-package
    description: Example package consumers will see
    source: acme-org/example-package
    version: "^1.0.0"

  - name: monorepo-tool
    description: Package that lives in a subdirectory
    source: acme-org/monorepo
    subdir: tools/monorepo-tool
    version: "~2.3.0"
    tagPattern: "monorepo-tool-v{version}"

  - name: pinned-package
    description: Pinned to an explicit ref
    source: acme-org/pinned-package
    ref: 3f2a9b1c
```

### Top-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Marketplace identifier. |
| `description` | yes | One-line summary shown to consumers. |
| `version` | yes | Semver of the marketplace itself. Bump on release. |
| `owner` | yes | Mapping with `name` (required), optional `url`, `email`. |
| `output` | no | Output path for the compiled file. Defaults to `marketplace.json`. |
| `build` | no | APM-only build options. See below. |
| `metadata` | no | Opaque pass-through copied into `marketplace.json`. |
| `packages` | no | List of package entries. |

### The `build` block (APM-only)

| Field | Default | Description |
|-------|---------|-------------|
| `tagPattern` | `v{version}` | Marketplace-wide default for resolving `{version}` to a git tag. Accepts `{version}` and `{name}` placeholders. |

Stripped from `marketplace.json` at compile time.

### Package entries

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Plugin name consumers will install. Unique within the marketplace. |
| `source` | yes | `<owner>/<repo>` shape, e.g. `acme-org/example-package`. Resolves to a git remote. |
| `description` | no | Pass-through to `marketplace.json`. |
| `tags` | no | Pass-through list of strings. |
| `version` | conditional | Semver range (see below). Either `version` or `ref` must be set. |
| `ref` | conditional | Explicit SHA, tag, or branch. Takes precedence over `version`. |
| `subdir` | no | Subdirectory within the repo. Validated against path traversal. |
| `tag_pattern` | no | Per-package override of `build.tagPattern`. |
| `include_prerelease` | no | Include semver pre-release tags in range resolution. Defaults to `false`. |

Unknown keys at any level raise a schema error rather than being silently ignored.

### `.gitignore`

Both `marketplace.yml` and `marketplace.json` must be tracked. `apm marketplace init` warns if your `.gitignore` would exclude `marketplace.json`. If you use a generic `*.json` rule, add an explicit unignore:

```gitignore
# .gitignore
*.json
!marketplace.json
```

## Version ranges

APM uses npm-compatible semver ranges. The most common forms:

| Range | Matches |
|-------|---------|
| `1.2.3` | Exact version. |
| `^1.2.3` | Compatible: `>=1.2.3 <2.0.0`. |
| `~1.2.3` | Patch-level: `>=1.2.3 <1.3.0`. |
| `>=1.2.0` | Everything from 1.2.0 upwards. |
| `<2.0.0` | Everything below 2.0.0. |
| `1.x` or `1.*` | Any 1.y.z. |
| `>=1.2.0 <2.0.0` | AND-combination. |

Pre-release tags (for example `1.2.0-beta.1`) are excluded by default. Set `include_prerelease: true` on the entry, or pass `--include-prerelease` to the build command, to include them.

Pin to a non-semver ref when you need exact reproducibility across a range the upstream does not tag cleanly:

```yaml
packages:
  - name: pinned-package
    source: acme-org/pinned-package
    ref: 3f2a9b1cdeadbeef   # SHA, tag, or branch -- overrides version ranges
```

`ref` takes precedence over `version`. If both are set, `version` is ignored.

## Managing plugins

Three subcommands let you manage `marketplace.yml` entries without hand-editing YAML.

### Adding a package

```bash
apm marketplace package add microsoft/apm-sample-package \
  --version ">=1.0.0" \
  --description "Sample package"
```

`package add` takes a `<owner>/<repo>` source, derives the package name from the repo, and appends an entry to `packages:`. Pass `--name` to override the derived name, `--subdir` for monorepo paths, `--tag-pattern` for non-default tag layouts, or `--tags` to attach metadata tags. By default the command verifies the source is reachable via `git ls-remote`; pass `--no-verify` to skip that check.

`--version` and `--ref` are mutually exclusive -- use `--ref` to pin an exact SHA, tag, or branch instead of a semver range.

### Updating a package

```bash
apm marketplace package set apm-sample-package --version ">=2.0.0"
```

`package set` takes the package name (not the source) and updates the specified fields in place. Any option accepted by `package add` (except `--name`) can be passed to `package set`.

### Removing a package

```bash
apm marketplace package remove apm-sample-package --yes
```

`package remove` drops the named entry from `packages:`. Without `--yes` the command prompts for confirmation.

## The build flow

`apm marketplace build` reads `marketplace.yml`, runs `git ls-remote` against each package source, picks the best-matching ref for each entry, and writes `marketplace.json` atomically (temp file plus rename).

```
apm marketplace build
```

| Flag | Description |
|------|-------------|
| `--dry-run` | Resolve and print the result table, but do not write `marketplace.json`. |
| `--offline` | Use only cached refs; fail entries that need a fresh `git ls-remote`. |
| `--include-prerelease` | Allow pre-release tags to satisfy every range (overrides per-entry flag). |
| `-v`, `--verbose` | Include per-entry resolution detail. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Build succeeded; `marketplace.json` written (or previewed). |
| `1` | Build error -- network failure, ref not found, no tag matches the range, etc. |
| `2` | Schema error in `marketplace.yml`. |

### What the compiler does

1. Parses and validates `marketplace.yml`. Unknown keys or invalid semver is a schema error (exit 2).
2. For each package: runs `git ls-remote`, enumerates tags and branches, filters by the entry's tag pattern, resolves the version range, picks the highest match.
3. Walks `metadata:` unchanged into the output.
4. Emits `plugins:` with the Anthropic key name; each entry carries the resolved `source` (with `ref` and SHA) plus any pass-through fields (`description`, `tags`).
5. Writes the file atomically.

## Checking and troubleshooting

Two commands cover diagnosis.

### `apm marketplace check`

Validates the yml schema and verifies every entry is resolvable. Use it in CI before publishing.

```bash
apm marketplace check
apm marketplace check --offline    # schema + cached refs only
```

Exit code is non-zero when any entry is unreachable, a ref does not exist, or no tag satisfies a range.

### `apm marketplace doctor`

Checks the environment -- git version, network reachability of common hosts, `gh` CLI presence, git authentication, and whether `marketplace.yml` is present and parses.

```bash
apm marketplace doctor
```

Run it first when `build` or `publish` fails in an unfamiliar environment.

### Common errors

| Symptom | Cause | Fix |
|---------|-------|-----|
| `'packages[0].source' must match '<owner>/<repo>' shape` | `source` is a full URL or contains a path. | Use `owner/repo` and put path under `subdir:`. |
| `No tag matching '^1.0.0'` | No published tags satisfy the range under your tag pattern. | Loosen the range, check `tagPattern`, or pin with `ref:`. |
| `Ref 'main' not found` | Branch or tag does not exist upstream. | Verify with `git ls-remote <url>`. |
| `Pre-release tags skipped` | Latest published tag is a pre-release. | Set `include_prerelease: true` on the entry or pass `--include-prerelease`. |
| `No cached refs (offline)` | First-ever `--offline` build. | Run once online to populate the cache, then retry offline. |
| `git ls-remote` auth failure | Private source without credentials. | Ensure your git credentials (SSH agent or `gh auth login`) can reach the source repo. |

### GitHub Enterprise Server

`apm marketplace build` respects the `GITHUB_HOST` environment variable. Set it before building to resolve packages from a GHES instance:

```bash
export GITHUB_HOST=github.company.com
apm marketplace build
```

Token resolution and metadata fetch use the same host, so existing auth configuration (see [Authentication](../../getting-started/authentication/)) works automatically. `git ls-remote` calls are authenticated with the resolved token, so private GHES repos work without a separate git credential helper. `type: url` sources accept Git-style repository URLs as input, including HTTPS and SSH forms, but APM resolves auth and metadata against `GITHUB_HOST`. In practice, the URL host is ignored unless it matches `GITHUB_HOST`, so do not rely on `type: url` for true cross-host resolution.

## Discovering upgrades

`apm marketplace outdated` compares the currently resolved version of each package (as captured in `marketplace.json`) against the latest tag available in the source repo.

```bash
apm marketplace outdated
apm marketplace outdated --include-prerelease
apm marketplace outdated --offline
```

Output columns: package, current version, declared range, latest in range, latest overall. Packages whose "latest overall" exceeds "latest in range" need a **manual range bump** (for example, widening `^1.0.0` to `^2.0.0`) before a new build will pick them up. This is intentional -- major-version bumps are a maintainer decision.

Packages pinned with `ref:` show `--` in the range columns; `outdated` cannot reason about them.

## Publishing to consumers

`apm marketplace publish` drives the compiled `marketplace.json` out to consumer repositories and opens pull requests on their behalf. It is the end-to-end flow for "I just built a new marketplace version; roll it out."

You need:

1. A built `marketplace.json` on the current branch (run `apm marketplace build` first).
2. A `consumer-targets.yml` file listing the repos to update.
3. The [`gh` CLI](https://cli.github.com/) authenticated against GitHub (unless you use `--no-pr`).

### The targets file

```yaml
# consumer-targets.yml
targets:
  - repo: acme-org/service-a
    branch: main
  - repo: acme-org/service-b
    branch: develop
    path_in_repo: apm/apm.yml        # optional; defaults to apm.yml
  - repo: acme-org/service-c
    branch: main
```

`repo` and `branch` are required; `path_in_repo` defaults to `apm.yml`. Paths are validated for traversal.

### First run -- preview

Always dry-run first:

```bash
apm marketplace publish --dry-run --yes
```

This clones each target, computes what would change in its lockfile references, and prints a plan. Nothing is pushed.

### Real run

```bash
apm marketplace publish
```

Output shows per-target status: updated, unchanged, failed. PR URLs are printed for each target that had changes.

### Useful flags

| Flag | Purpose |
|------|---------|
| `--targets PATH` | Use a custom targets file (default `./consumer-targets.yml`). |
| `--dry-run` | Preview; no push, no PR. |
| `--no-pr` | Push the branch to each target but skip PR creation (useful when `gh` is unavailable or you use another PR workflow). |
| `--draft` | Open PRs as drafts. |
| `--allow-downgrade` | Allow pushing a lower version than the target currently references. Off by default to prevent accidental regressions. |
| `--allow-ref-change` | Allow switching ref types (for example, branch to SHA). Off by default. |
| `--parallel N` | Maximum concurrent targets. Default `4`. |
| `--yes`, `-y` | Skip interactive confirmation (required for non-interactive CI). |
| `-v`, `--verbose` | Per-target detail. |

### State file

Publish runs append to `.apm/publish-state.json`, which records the history of runs (timestamps, targets, outcomes, PR URLs). This lets later invocations detect already-open PRs and avoid opening duplicates. The file is safe to commit or to gitignore -- it is advisory, not authoritative.

## Recipes

### Custom tag pattern

Projects that prefix tags with a package name (common in monorepos) need a per-entry pattern:

```yaml
packages:
  - name: ui-components
    source: acme-org/frontend-monorepo
    subdir: packages/ui-components
    version: "^3.0.0"
    tag_pattern: "ui-components-v{version}"
```

The `{name}` placeholder resolves to the package entry's `name`, so you can also write `tag_pattern: "{name}-v{version}"` and reuse a single `build.tagPattern`.

### Pre-release tags are being skipped

Set `include_prerelease: true` on the package entry, or pass `--include-prerelease` to `build` and `outdated` for the whole marketplace:

```yaml
packages:
  - name: example-package
    source: acme-org/example-package
    version: ">=1.0.0-0"
    include_prerelease: true
```

Note the `-0` pre-release suffix on the range -- it makes the lower bound inclusive of pre-releases.

### PR body is wrong -- how do I re-run safely?

Close the incorrect PR, fix `marketplace.yml` or the targets file, rebuild, and re-run `apm marketplace publish`. The command is idempotent on identical inputs: if the target branch already carries the expected change, the target is reported as "unchanged". If you need to force a fresh PR on a target that currently has a different ref than expected, pass `--allow-ref-change`.

### Can I use a non-GitHub host?

Not in the first release. `apm marketplace publish` uses the `gh` CLI and assumes GitHub for PR creation. You can still `build` and `check` against any git remote that speaks `git ls-remote` over HTTPS or SSH; only the `publish` step is GitHub-specific. For non-GitHub consumers, run `publish --no-pr` and drive the PR creation through your own tooling.

## Related reading

- [Marketplaces guide](../marketplaces/) -- consumer-side: registering and installing from a marketplace.
- [CLI command reference](../../reference/cli-commands/) -- authoritative options for every `apm marketplace` subcommand.
- [Plugins guide](../plugins/) -- what a plugin is and how consumers install one.
