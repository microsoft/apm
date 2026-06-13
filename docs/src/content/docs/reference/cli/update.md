---
title: apm update
description: Re-resolve dependencies in apm.yml against the latest matching Git refs, with a plan and consent gate before writing.
sidebar:
  order: 4
---

Refresh the dependencies declared in `apm.yml` to the latest matching Git refs, after showing you the plan and asking for consent.

## Synopsis

```bash
apm update [OPTIONS] [PACKAGES...]
```

## Description

`apm update` re-resolves every dependency in your project's `apm.yml` against the newest Git ref allowed by its constraint, prints a structured plan -- **added**, **updated**, **removed**, **unchanged** -- and prompts before touching anything. Full-SHA revision pins are refreshed by resolving the latest annotated semver tag from the authoritative upstream, then rewriting the SHA in `apm.yml` with a tag annotation after you accept the plan (for example `# v2.0.0`). Decline the prompt and APM exits cleanly: no manifest rewrite, no lockfile write, no filesystem changes.

Pass one or more `PACKAGES` to refresh only those dependencies, or `-g/--global` to refresh the user-scope dependencies under `~/.apm/` instead of the current project. With these flags `apm update` is a strict superset of the deprecated [`apm deps update`](../deps/#apm-deps-update).

This is the dependency-refresh command. To upgrade the APM CLI binary itself, see [`apm self-update`](../self-update/).

:::note[Consent gate]
The interactive prompt defaults to **No**. In non-interactive contexts (CI, piped stdin) you must pass `--yes` to proceed; otherwise `apm update` aborts without modifying the manifest, lockfile, or workspace.
:::

For a read-only install that pins to whatever is already in `apm.lock.yaml` -- the right command for CI -- use [`apm install --frozen`](../install/).

## Arguments

| Argument | Description |
| --- | --- |
| `PACKAGES...` | Optional. One or more dependency names to refresh (short name like `compliance-rules` or canonical `owner/repo`). Omit to refresh everything. Unknown names exit non-zero with the available list. |

## Options

| Flag | Default | Description |
| --- | --- | --- |
| `--yes`, `-y` | off | Skip the interactive prompt and accept the plan. Required for non-interactive use. |
| `--dry-run` | off | Compute and print the plan without prompting and without writing the manifest, lockfile, or filesystem. |
| `--verbose`, `-v` | off | Show per-dependency resolution detail (old ref, new ref, source) and full error context. |
| `--global`, `-g` | off | Refresh user-scope dependencies under `~/.apm/` instead of the current project (mirrors `apm install -g`). |
| `--force` | off | Overwrite locally-authored files on collision. |
| `--parallel-downloads N` | `4` | Max concurrent package downloads. `0` disables parallelism. |
| `--target TARGET`, `-t TARGET` | auto-detect | Agent harness(es) to update for. Accepts a single value (`claude`, `copilot`, `cursor`, `windsurf`, `kiro`, `codex`, `opencode`, `gemini`) or comma-separated list (`--target claude,cursor`). Overrides `apm.yml targets:` and auto-detection. |

## Examples

Preview what would change, without prompting or writing:

```bash
apm update --dry-run
```

Interactively review and accept the plan:

```bash
apm update
# prints plan, prompts: Apply these changes? [y/N]
```

Accept non-interactively (CI, scripts):

```bash
apm update --yes
```

Refresh only specific packages:

```bash
apm update org/pkg-a org/pkg-b
```

Refresh user-scope dependencies installed with `apm install -g`:

```bash
apm update -g
```

Decline the prompt -- nothing is written:

```bash
apm update
# Apply these changes? [y/N] n
# No changes applied.
```

## Behavior

- **Re-resolve every dep.** Each entry in `apm.yml` is resolved against its remote source for the newest ref allowed by the constraint (branch tip, latest matching tag, etc.). Full-SHA revision pins move only to the commit behind the latest annotated semver tag; branch refs and lightweight tags are refused. Local-path deps are skipped.
- **Structured plan.** Output is grouped into four sections:
  - **added** -- present in the new resolution but not in the previous lockfile.
  - **updated** -- ref or version moved.
  - **removed** -- previously locked, no longer required by `apm.yml`.
  - **unchanged** -- already at the latest matching ref.
- **Consent gate.** The prompt defaults to **No**. Without `--yes`, declining (or running in a non-interactive context) aborts with a clean exit; the manifest, lockfile, and workspace are untouched.
- **No partial consent.** A single prompt covers both revision-pin manifest rewrites and the normal update plan; declining leaves everything unchanged.
- **`--dry-run` skips the prompt.** It computes and prints the plan, including revision-pin SHA/tag rewrites, but never writes and never asks.

## Back-compat: `apm update` used to be the self-updater

In earlier releases, `apm update` self-updated the **APM CLI binary**. That behavior moved to [`apm self-update`](../self-update/) and `apm update` was repurposed as the dependency updater described above.

For one release after the rename, running `apm update` from a directory **without an `apm.yml`** prints a deprecation banner and forwards to `apm self-update` so existing muscle memory and scripts keep working. This shim is removed in the next minor release -- update your scripts to call `apm self-update` directly.

## Related

- [`apm install --frozen`](../install/) -- read-only install pinned to `apm.lock.yaml`; fails on drift. Use this in CI.
- [`apm self-update`](../self-update/) -- upgrade the APM CLI binary itself.
- [`apm outdated`](../outdated/) -- report dependencies with newer refs available, without changing anything.
- [Manage dependencies (consumer guide)](../../../consumer/manage-dependencies/) -- task-oriented walkthrough.
- [Update and refresh](../../../consumer/update-and-refresh/) -- when to use `update`, `install --frozen`, and `self-update`.
