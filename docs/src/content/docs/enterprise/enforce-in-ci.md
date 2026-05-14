---
title: Enforce in CI
description: Wire apm audit into your CI so policy and integrity gates run on every pull request, even when a developer bypassed the local checks.
sidebar:
  order: 4
---

`apm install` already runs the policy gate, the security scan, and drift
detection on every developer machine. CI re-runs the same gates on the
pull request itself. That is defence in depth: a developer can pass
`--no-policy`, `--force`, or `APM_POLICY_DISABLE=1` locally; CI cannot.

This page is the recipe set. For the full schema and the rollout
playbook, see [Governance overview](../governance-overview/) and
[apm-policy getting started](../apm-policy-getting-started/).

## The gate

```bash
apm audit --ci
```

One command. It runs the eight baseline lockfile checks
(`lockfile-exists`, `ref-consistency`, `deployed-files-present`,
`no-orphaned-packages`, `skill-subset-consistency`, `config-consistency`,
`content-integrity`, `includes-consent`), the install-replay drift
check, and -- if an `apm-policy.yml` is discovered -- the org policy
checks. Exit code is `0` clean, `1` on any violation.

Useful flags:

- `--policy <source>` -- explicit policy ref
  (`org`, a path, a URL, or `<owner>/<repo>`). Without it, APM
  auto-discovers from your project's git remote, mirroring `apm install`.
- `--no-policy` -- skip policy discovery (baseline + drift only).
- `--no-cache` -- force a fresh policy fetch. Recommended in CI so a
  cached file does not mask a same-day policy update.
- `--no-fail-fast` -- run every check even after one fails. Useful for
  reports; default is stop at first failure.
- `--no-drift` -- skip the install-replay. Reduces coverage; only use
  when CI minutes are the bottleneck.
- `-f json` / `-f sarif` -- structured output. Markdown is not
  supported in `--ci` mode.
- `-o <path>` -- write the report to a file. The format is inferred
  from the extension (`.sarif`, `.json`).

## Recipe: minimal GitHub Actions gate

This is the smallest job that fails a PR on any APM violation.

```yaml
# .github/workflows/apm-audit.yml
name: APM audit
on:
  pull_request:
    paths:
      - 'apm.yml'
      - 'apm.lock.yaml'
      - '.apm/**'
      - '.github/**'
      - '.claude/**'
      - '.cursor/**'

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
      - run: apm audit --ci --no-cache
        env:
          GITHUB_APM_PAT: ${{ secrets.APM_PAT }}
```

`microsoft/apm-action@v1` runs `apm install` by default, so by the time
`apm audit --ci` runs, the lockfile and deployed files are present.
Make this job a required status check via
[GitHub Rulesets](../github-rulesets/) and a violating PR cannot merge.

## Audit-only CI pattern

The default `microsoft/apm-action@v1` runs `apm install` before any
subsequent steps. That is the right default for most workflows: it ensures
the lockfile and deployed files are present before the audit reads them.

However, `apm install` overwrites every managed file with a fresh copy
before `apm audit --ci` runs. If a managed file was modified on disk after
the last install -- its bytes changed without updating the lockfile hash --
the install step silently restores the clean copy. The `content-integrity`
check then compares the freshly restored file against a hash that matches,
and the tampering goes undetected.

To detect post-install file modification, run the action in setup-only mode
so it only adds the CLI to `PATH` without touching deployed files:

```yaml
jobs:
  audit:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
        with:
          setup-only: true     # CLI only; does not run apm install
      - run: apm audit --ci --no-drift
        env:
          GITHUB_APM_PAT: ${{ secrets.APM_PAT }}
```

`setup-only: true` leaves every deployed file exactly as checked out.
`--no-drift` skips the install-replay check because no warm cache exists;
the `content-integrity` check still verifies that every deployed file's
SHA-256 hash matches the `deployed_file_hashes` recorded in `apm.lock.yaml`.
Any file whose bytes were changed after the last install fails this check.

The two patterns serve different goals:

| Pattern | Use when |
|---|---|
| Full install then audit | Catching developers who skipped `apm install` after editing `apm.yml`; ensuring deployed files are present on a fresh runner |
| Audit-only (`setup-only: true`) | Detecting modification of deployed files after install; committed files and lockfile are the ground truth |

Both patterns enforce policy and the eight baseline lockfile checks. The
difference is only in whether content-integrity can see tampered bytes.

## Recipe: SARIF for GitHub Code Scanning

Emit SARIF and upload it so each violation appears inline on the PR
diff and in the repository's Security tab.

```yaml
jobs:
  audit:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      security-events: write
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
      - name: Audit
        run: apm audit --ci --no-cache -o apm-audit.sarif
        env:
          GITHUB_APM_PAT: ${{ secrets.APM_PAT }}
      - name: Upload SARIF
        if: always()
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: apm-audit.sarif
          category: apm-audit
```

`if: always()` matters: SARIF must upload even when the audit step
exited `1`, otherwise the failing run produces no Code Scanning entry.

## Recipe: scheduled drift sweep

Pull requests catch drift on the changed branch. A nightly job catches
drift on `main` -- hand-edits, missing `apm install` runs after a
manual lockfile bump, or a stale deployed file that no PR touched.

```yaml
on:
  schedule:
    - cron: '0 6 * * *'  # 06:00 UTC daily
  workflow_dispatch:

jobs:
  drift-sweep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
      - run: apm audit --ci --no-fail-fast -o drift.sarif
      - if: always()
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: drift.sarif
          category: apm-drift-sweep
```

`--no-fail-fast` lets the sweep report every finding rather than the
first one. See [drift detection](../drift-detection/) for what the
replay actually checks and how to debug a finding locally.

## When the gate blocks a PR

The fix path depends on which check failed.

- **`lockfile-exists` / `ref-consistency` / `deployed-files-present`.**
  The author skipped `apm install` after editing `apm.yml`. They run
  `apm install`, commit `apm.lock.yaml` and the integrated files, and
  push.
- **`content-integrity` or a hidden-Unicode finding.** A primitive was
  hand-edited. The author runs `apm audit --strip` to clean it (or
  reverts the edit), then `apm install` to refresh the lockfile.
- **Drift replay.** A deployed file no longer matches what an install
  would produce. `apm install` is the fix.
- **Policy violation.** The author either picks an allowed alternative,
  or opens a change request against `<org>/.github/apm-policy.yml`.
  `--no-policy` does not work here -- CI ignores the local bypass flag.

For genuine, time-boxed exceptions, two waiver shapes exist today:

1. Amend `apm-policy.yml` (allow-list the package, raise `max_depth`,
   etc.) through the same review process as any other policy change.
2. Lower `enforcement` from `block` to `warn` for that policy scope.
   Findings still appear in the SARIF report; they no longer fail the
   job. Treat this as a temporary state and track its removal.

There is no per-PR override flag and there will not be one. Bypass
must be visible in the policy file's history.

## Next steps

- [drift detection](../drift-detection/) -- what the replay actually
  catches and how to read its output.
- [security and supply chain](../security-and-supply-chain/) -- the
  built-in install-time scan that complements the CI gate.
- [github rulesets](../github-rulesets/) -- make the audit job a
  required status check across an org.
- [APM in CI/CD](../../integrations/ci-cd/) -- deeper patterns for
  Azure Pipelines, GitLab, Jenkins, air-gapped runners, and bundle
  caching across jobs.
