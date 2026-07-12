---
title: "Operating installed context"
description: "Day-to-day workflow for APM-managed projects: reproduce the lockfile in CI, see what is installed, diagnose environment problems. Maps every common operational question to the existing command."
sidebar:
  order: 7
---

After `apm install` succeeds, the day-to-day operating questions are: what
is installed, has anything drifted from the lockfile, and why is the
environment broken when something fails. APM ships a command for each
question -- this page maps the question to the command so you do not have
to remember the flag matrix.

## At a glance

| You want to... | Run | Notes |
|---|---|---|
| Reproduce the lockfile exactly (CI gate) | `apm install --frozen` | Refuses to install when `apm.lock.yaml` is missing or out of sync with `apm.yml`. Equivalent in spirit to `npm ci` / `pnpm install --frozen-lockfile`. |
| Refresh versions or refs and rewrite the lockfile | `apm update` (or `apm update --yes` for CI) | Restructures the dependency graph against latest matching versions or refs. |
| Validate lockfile integrity for CI | `apm audit --ci` | Lockfile-consistency check + on-disk integrity. Pair with `--format sarif --output audit.sarif` for GitHub Code Scanning. |
| See what is installed | `apm deps list` | Project scope. Add `--global` for `~/.apm/`. |
| Inspect the dependency tree | `apm deps tree` | Hierarchical view of direct + transitive deps. |
| Find out why a package is installed | `apm deps why <package>` | Reverse lookup -- "who pulled this in?". Add `--json` for scripts. |
| See what is outdated | `apm outdated` | Locked refs vs latest matching upstream. |
| Diagnose a broken environment | [`apm doctor`](../../reference/cli/doctor/) | Aggregated pass/fail table: Git, network, authentication, and marketplace configuration when present. |
| Inspect the cache | `apm cache info` | Disk usage and location. `apm cache clean` removes everything; `apm cache prune --days N` is incremental. |
| Inspect resolved runtimes | `apm runtime status` | Active runtime and preference order. |
| Inspect resolved targets | `apm targets` | Which harnesses APM will deploy to. Add `--json --all` to include meta-targets (e.g. `agent-skills`). |
| Show package metadata | `apm view <package>` | Versions, refs, owner, declared scripts. |

## Recommended CI block

```yaml
- run: apm install --frozen
- run: apm audit --ci --format sarif --output apm-audit.sarif
```

The `--frozen` flag is the CI-safety primitive: if the lockfile is missing
or has drifted from `apm.yml`, the install fails before producing any
artifacts. `apm audit --ci` is then the integrity gate -- it validates that
the locked content matches what was actually fetched.

## Local refresh loop

```bash
apm update            # refresh refs + rewrite the lockfile
apm install           # materialize the new lockfile
apm audit             # confirm integrity
```

Use this when you intentionally want to take newer upstream refs. The
lockfile change is the auditable record of the upgrade.

## When something is broken

The first stop for "I installed but it does not work" or "CI passes
locally but fails on the runner" is `apm doctor`. It runs a bounded set of
environment checks (git on PATH, github.com reachable, auth token
detected, and optionally marketplace config) and renders a pass/fail table with
a single non-zero exit code if a critical check fails. The GitHub CLI is one
possible authentication source, not a separate diagnostic check.

```bash
apm doctor               # quick pass/fail table
apm doctor --verbose     # plus detail per check
```

For more targeted introspection:

- `apm cache info` -- is the cache writable, how large is it
- `apm runtime status` -- is the expected runtime installed
- `apm config` -- is the configuration parseable, what is active
- `APM_DEBUG=1 apm install --dry-run -v` -- full resolution trace

## Vocabulary mapping for users coming from other ecosystems

If you reach for a verb from another package manager and APM does not
have it, the equivalent is almost always already in the table above.
Common translations:

| Other ecosystem | APM equivalent |
|---|---|
| `npm ci` | `apm install --frozen` |
| `npm audit` | `apm audit` |
| `npm why <pkg>` / `yarn why <pkg>` | `apm deps why <pkg>` |
| `pnpm install --frozen-lockfile` | `apm install --frozen` |
| `uv sync` | `apm install` (or `apm install --frozen` for the CI form) |
| `uv lock --check` | `apm audit --ci` |
| `cargo tree -i <pkg>` | `apm deps why <pkg>` |
| `brew doctor` / `flutter doctor` | `apm doctor` |
| `pip check` | `apm audit` |
