---
title: Drift Detection
description: How an admin spots packages installed off-policy, lockfiles diverging from registered marketplaces, and security findings landing in dependencies across many repos.
sidebar:
  order: 5
---

Drift is what you find between two `apm install` runs that should have
been identical. This page is the operator's checklist: the categories
APM detects today, the command surface for each, and patterns for
sweeping the signal across an estate of repos.

## What "drift" means in APM

Four concrete categories. Each has a different signal and a different fix.

| Category | What it is | Detected by |
|---|---|---|
| Lockfile drift | `apm.yml` and `apm.lock.yaml` disagree, or deployed files do not match the lockfile | `apm audit --ci` (8 baseline checks) |
| Cache drift | A cached checkout's `HEAD` no longer matches `resolved_commit` in the lockfile | Built into every `apm install` cache hit |
| Ref drift (stale floats) | A locked branch tip or tag has moved upstream since the last install | `apm outdated` |
| Security-finding drift | A primitive in `apm_modules/` now contains hidden Unicode that was clean before | `apm audit` |

Marketplace-shadow drift -- the same plugin name appearing under more than
one registered marketplace -- is reported inline by `apm install` when it
happens; see [security and supply chain](../security-and-supply-chain/).

## The local commands

### `apm audit --ci`

The lockfile-consistency gate. Runs eight baseline checks in order and
exits non-zero on the first failure (or on any failure with
`--no-fail-fast`):

```
lockfile-exists -> ref-consistency -> deployed-files-present
-> no-orphaned-packages -> skill-subset-consistency
-> config-consistency -> content-integrity -> includes-consent
```

After the baseline passes, it replays the install in a scratch directory
from the cache and diffs against the working tree to surface
`unintegrated`, `modified`, and `orphaned` files. Pass `--no-drift` to
skip the replay. With `--policy <source>` it also evaluates the
discovered policy against the lockfile. Source:
`src/apm_cli/commands/audit.py`, `src/apm_cli/policy/ci_checks.py`.

### `apm audit` (default)

Scans every deployed primitive for hidden Unicode (zero-width characters,
bidi overrides, tag characters) and runs the drift replay as advisory
output. Exits 0 even when findings exist. Add `--strip` to remove hidden
characters in place; add `--file <path>` to scan a single file outside
the lockfile.

### `apm outdated`

Compares each locked dependency against its remote tip. Tag-pinned
dependencies use semver comparison; branch-pinned dependencies compare
commit SHAs. Marketplace-sourced dependencies are compared against the
marketplace entry's current ref. Source: `src/apm_cli/commands/outdated.py`.

```bash
apm outdated              # project lockfile
apm outdated --global     # ~/.apm/ user-scope lockfile
apm outdated -j 8         # 8 parallel remote checks
```

`apm outdated` does not modify anything. It is the read-only view that
tells you which floating refs have moved.

### `apm view <package>`

Prints the lockfile entry for a single package (`resolved_ref`,
`resolved_commit`, deployed files). Use it to confirm what a repo
actually has after a drift report names a dependency. Note that
`apm list` lists scripts, not packages.

## Cache integrity

`apm install` verifies cache integrity on every cache hit before reusing
a checkout. It reads `.git/HEAD` directly (not via `git rev-parse`, so a
poisoned `.git/config` cannot subvert the check) and compares the
resolved 40-character SHA against the `resolved_commit` recorded in
`apm.lock.yaml`. On mismatch, the cache entry is evicted and a fresh
fetch runs. Source: `src/apm_cli/cache/integrity.py`.

This means a stale CI runner, a teammate who manually `git checkout`-ed
inside `apm_modules/`, or a bumped pin without a re-install all surface
as either eviction-and-refetch (silent self-heal) or a hard failure
when the cache cannot be repopulated -- never as wrong content under
the right name.

## Install before audit and tamper detection

Running `apm install` before `apm audit --ci` is the correct pattern when
the goal is detecting a developer who forgot to run `apm install` after
editing `apm.yml`. The install step regenerates deployed files so the
subsequent audit can compare them against the lockfile.

That sequence has a blind spot: `apm install` overwrites every managed file
with a clean copy before the audit runs. If a deployed file was modified on
disk after the last install -- for example a hand-edit to
`.github/instructions/` -- the install step restores the original bytes.
The `content-integrity` check then compares the restored file against a
matching hash and reports no finding.

To detect post-install modification, use `setup-only: true` on the action
so it only provides the CLI without running `apm install`, then audit with
`--no-drift`:

```yaml
- uses: microsoft/apm-action@v1
  with:
    setup-only: true
- run: apm audit --ci --no-drift
```

`--no-drift` skips the install-replay (which requires a warm cache that
`setup-only` does not populate). The `content-integrity` check verifies
SHA-256 hashes of every deployed file against `deployed_file_hashes` in
`apm.lock.yaml` without needing to replay the install. Any byte-level
change to a deployed file since the last install is caught by this check.

See [Enforce in CI](../enforce-in-ci/#audit-only-ci-pattern) for the full
recipe and a comparison table of the two patterns.

## Org-wide sweeps

APM runs per repository. There is no built-in fleet console. The
operational pattern is to run the same audit in every repo and centralize
the output.

Two shapes work today:

**Scheduled CI sweep.** Add a workflow that runs nightly in every repo
and uploads the SARIF or JSON output as an artifact (or to GitHub code
scanning). The dashboard is whatever ingests the artifact.

```yaml
# .github/workflows/apm-drift.yml -- nightly per repo
name: apm drift
on:
  schedule: [{ cron: '0 6 * * *' }]
  workflow_dispatch:
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: curl -fsSL https://aka.ms/apm/install | bash
      - run: apm install
      - run: apm audit --ci --format sarif --output apm-audit.sarif
        continue-on-error: true
      - uses: github/codeql-action/upload-sarif@v3
        with: { sarif_file: apm-audit.sarif }
      - run: apm outdated > apm-outdated.txt
        if: always()
      - uses: actions/upload-artifact@v4
        with: { name: apm-drift, path: 'apm-*' }
```

**Central audit harness.** Run a single job that iterates over a list of
repos (from a GitHub org listing or a static manifest), clones each one,
and runs `apm audit --ci --format sarif` plus `apm outdated`. Aggregate
the outputs into one dashboard.

Either pattern depends only on documented `apm` flags. APM does not
phone home; the harness is yours.

## Reporting formats

`apm audit --ci` and `apm audit` both accept `--format` and `--output`.

| Format | Use |
|---|---|
| `text` (default) | Terminal review, pull-request logs |
| `json` | Custom dashboards, ticketing integrations |
| `sarif` | GitHub code scanning, Defender for Cloud, any SARIF-aware viewer |
| `markdown` | Pull-request comments, weekly status digests |

`--format json|sarif|markdown` cannot be combined with `--strip` or
`--dry-run`; those modes are interactive by design.

## Remediation

Once drift is detected, remediation routes back to two pages:

- [Enforce in CI](../enforce-in-ci/) -- wire `apm audit --ci` into branch
  protection so future drift cannot land. The same command that detects
  drift here is the one that gates merges.
- [Security and supply chain](../security-and-supply-chain/) -- scope
  tokens, lock the registry proxy, and tighten `apm-policy.yml` so the
  drift you cleaned up cannot reappear from a new source.

For the consumer-side view of what these checks protect against on a
single workstation, see [drift and secure by default](../../consumer/drift-and-secure-by-default/).
