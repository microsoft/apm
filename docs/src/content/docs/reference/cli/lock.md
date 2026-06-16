---
title: apm lock
description: Resolve all dependencies and write apm.lock.yaml without deploying any files to agent targets.
sidebar:
  order: 5
---

Resolve all dependencies declared in `apm.yml` and write `apm.lock.yaml` with pinned commit SHAs -- without copying any files to agent targets.

## Synopsis

```bash
apm lock [OPTIONS]
apm lock export [OPTIONS]
```

## Description

`apm lock` runs the full resolver and downloader so every dependency SHA is pinned, then writes `apm.lock.yaml`. It skips the targets, cleanup, post-deps-local, and audit phases. The integrate phase still runs but deploys nothing because the target set is empty in lockfile-only mode -- no files are copied to `.github/`, `.agents/`, or any other harness directory.

Use `apm lock` to:

- **Bootstrap a lockfile** before the first `apm install` run in a new project or CI environment.
- **Refresh the lockfile** after editing `apm.yml` without triggering a full deployment, so you can review the new lockfile before applying it.
- **Verify that `apm.yml` resolves cleanly** (useful in PR checks).

This mirrors the ergonomics of `cargo generate-lockfile` and `pnpm lock`.

## Options

| Flag | Default | Description |
| --- | --- | --- |
| `--verbose`, `-v` | off | Show per-dependency resolution details. |
| `--global`, `-g` | off | Operate on `~/.apm/apm.yml` instead of the current project (mirrors `apm install -g`). |
| `--update` | off | Re-resolve deps to their latest matching SHAs before writing the lockfile (like `apm install --update`). |
| `--no-policy` | off | Skip policy enforcement during resolution. |
| `--target TARGET`, `-t TARGET` | none | Agent target for policy enforcement during resolution. No files are deployed regardless of this value. Accepts a single target (`claude`, `copilot`, etc.) or comma-separated list. |
| `--parallel-downloads N` | `4` | Max concurrent package downloads. `0` disables parallelism. |

## Examples

Resolve from `apm.yml` and write the lockfile:

```bash
apm lock
```

Re-resolve to the latest SHAs and update the lockfile:

```bash
apm lock --update
```

Resolve and write the lockfile for user-scope dependencies:

```bash
apm lock -g
```

Show resolution details while writing the lockfile:

```bash
apm lock --verbose
```

## Behavior

- **Resolve and download.** Every dependency in `apm.yml` is resolved and, if not already cached, downloaded. Fresh downloads pin the commit SHA and compute a content hash.
- **Write `apm.lock.yaml`.** The lockfile records every pinned ref, resolved commit, and content hash. `deployed_files` entries are empty because no files are deployed.
- **No files deployed.** The targets, cleanup, post-deps-local, and audit phases are skipped. The integrate phase runs but deploys nothing because the target set is empty. Running `apm lock` is safe to run before you are ready to install.
- **Idempotent.** If the lockfile already matches the resolution result, it is overwritten with the same content.

## Export (SBOM inventory)

`apm lock export` serializes the existing lockfile into an SBOM document. It is an **inventory** export, not a security attestation: it reads `apm.lock.yaml` only and never re-resolves, re-hashes, or touches the network or filesystem.

```bash
apm lock export [OPTIONS]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--format FORMAT`, `-f FORMAT` | `cyclonedx` | SBOM output format: `cyclonedx` (1.5) or `spdx` (2.3). |
| `--output FILE`, `-o FILE` | stdout | Write the SBOM to a file instead of stdout. |
| `--global`, `-g` | off | Read the user-scope (`~/.apm/`) lockfile instead of the current project. |
| `--timestamp TS` | auto | Pin the SBOM timestamp (ISO 8601, e.g. `2024-06-01T00:00:00+00:00`) for reproducible output. Defaults to `SOURCE_DATE_EPOCH`, then the lockfile's `generated_at`. |

Component identity is a Package URL (`pkg:github/<owner>/<repo>@<commit>` for git deps, `pkg:oci/<name>@<digest>` for registry deps, `pkg:generic/<name>@<content_hash>` for local primitives), and the declared license is passed through verbatim (or `NOASSERTION` when undeclared). Output is deterministic -- components sorted by purl with a pinned timestamp -- so two runs are byte-identical. Credentials in recorded URLs are scrubbed. The diagnostic line routes to stderr, so `apm lock export | jq` stays clean. See [Inventory export (SBOM)](../../enterprise/security-and-supply-chain/#inventory-export-sbom) for the full model.

Export a CycloneDX SBOM to a file:

```bash
apm lock export --format cyclonedx -o sbom.json
```

Stream an SPDX SBOM to a tool:

```bash
apm lock export --format spdx | jq '.packages | length'
```

## CI integration

Add `apm lock` to your CI workflow to keep the lockfile in sync with `apm.yml`:

```yaml
- name: Refresh APM lockfile
  run: apm lock
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

- name: Commit updated lockfile
  run: |
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git add apm.lock.yaml
    git diff --cached --quiet || git commit -m "chore: update apm lockfile"
```

To verify the lockfile is up to date in a PR check (and fail if it drifts), use [`apm install --frozen`](../install/) instead.

## Related

- [`apm install`](../install/) -- install dependencies and deploy files to agent targets.
- [`apm install --frozen`](../install/) -- reproduce the lockfile exactly; fails on drift. Use this in CI.
- [`apm update`](../update/) -- re-resolve, show a plan, prompt for consent, then install.
- [`apm outdated`](../outdated/) -- report which dependencies have newer refs available.
- [Inventory export (SBOM)](../../enterprise/security-and-supply-chain/#inventory-export-sbom) -- the SBOM/declared-license model behind `apm lock export`.
