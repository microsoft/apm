---
title: Drift and secure by default
description: What protects you when you run apm install, and what to do when something is off.
---

You run `apm install`. APM fetches packages, writes files into your
project, and wires them into every supported harness. This page covers
the checks that run on every install, what triggers a failure, and the
one command -- `apm audit` -- that lets you re-verify on demand.

The short version: your dependency graph is explicit, your lockfile is
pinned, fetched content is hash-verified, and every install replay is
reproducible from the cache. You do not opt in. You opt out, and only
through named flags.

## What runs on every `apm install`

Three checks run automatically, in this order, before any file lands in
your harness directories.

### 1. Lockfile consistency

`apm.yml` declares what you want. `apm.lock.yaml` records what was
resolved last time. On install, APM reconciles the two:

- A dependency in `apm.yml` with no lockfile entry is resolved fresh
  and pinned.
- A dependency removed from `apm.yml` but still in the lockfile is
  pruned (orphan cleanup).
- A dependency whose `apm.yml` ref changed (e.g. you bumped a tag) is
  re-resolved; the new `resolved_commit` is written to the lockfile.

The lockfile is regenerated from the resolution result on every
install, so the on-disk lockfile cannot drift away from the manifest
silently. If you commit `apm.yml` without committing the regenerated
`apm.lock.yaml`, the next teammate's `apm install` updates it and
reports the change.

### 2. Content hash verification

When APM fetches a package fresh from the network, it computes a
SHA-256 over the package file tree and compares it to the
`content_hash` recorded in `apm.lock.yaml`. On mismatch, the install
aborts:

```
[x] Content hash mismatch for owner/repo: expected sha256:abc..., got sha256:def...
    The downloaded content differs from the lockfile record.
    This may indicate a supply-chain attack.
    Use 'apm install --update' to accept new content and update the
    lockfile.
```

Exit code is non-zero and the partially-downloaded directory is
removed before APM exits. Source: `src/apm_cli/install/sources.py`.

The opt-in escape hatch is `apm install --update`, which re-resolves
refs and accepts new content into the lockfile. Use it when you
intentionally bumped a dependency upstream and expect the hash to
change.

### 3. Cache-hit integrity

A warm cache is fast but dangerous: if anything mutated the cached
checkout (a poisoned shared CI runner, a stale teammate workspace, a
manual `git checkout` inside `apm_modules/`), reusing it would deploy
wrong content under the right name.

On every cache hit, APM reads the checkout's `.git/HEAD` and verifies
it matches the `resolved_commit` from the lockfile. On mismatch, the
cache entry is evicted and a fresh fetch runs. The check reads the
HEAD ref file directly rather than spawning `git rev-parse`, so a
poisoned `.git/config` cannot subvert it. Source:
`src/apm_cli/cache/integrity.py`.

Install also drops a small `.apm-pin` marker at each package root
recording the `resolved_commit` it deployed. `apm audit` re-checks
this marker before replaying the install, so a teammate who bumped
`apm.lock.yaml` without re-running `apm install` gets a clear "run
`apm install` first" error rather than misleading drift findings.

## What "secure by default" buys you

These checks rule out a specific set of failure modes without any
configuration:

- **Silent supply-chain swap.** Hash verification on fresh downloads
  catches a publisher (or a man-in-the-middle) replacing the content
  behind a pinned ref.
- **Stale cache reuse.** HEAD-vs-lockfile verification on cache hits
  catches the shared-runner and bumped-pin cases.
- **Forgotten reinstall.** The cache-pin marker plus `apm audit`
  detect the "I edited `apm.lock.yaml` but did not reinstall" gap.
- **Cross-host token leak.** Auth tokens are scoped per host:
  `GITHUB_APM_PAT` is sent only to GitHub hosts, `ADO_APM_PAT` only
  to Azure DevOps. APM never forwards a credential to a host you did
  not configure it for. See `../authentication/`.

What these checks do **not** cover, by design:

- Hidden Unicode inside primitive content (a prompt that displays as
  one thing and parses as another). That is the `apm audit` scan
  described below.
- Org policy (allow-listed sources, forbidden primitives, scope
  restrictions). Policy enforcement is an enterprise concern; see
  [Security and supply chain](../../enterprise/security-and-supply-chain/)
  and [Drift detection](../../enterprise/drift-detection/).

## On-demand: `apm audit`

`apm audit` is the explicit re-verification command. The two consumer
use cases:

```bash
apm audit
```

Scans installed packages for hidden Unicode characters that can
hijack agent behavior (zero-width characters, bidi overrides, tag
characters). Default output is text; add `--format sarif` to wire
into GitHub code-scanning or `--format json` for tooling.

```bash
apm audit --strip
```

Removes hidden characters from scanned files in place. Combine with
`--dry-run` to preview the changes first.

`apm audit` also runs **install-replay drift detection** by default:
it replays your install into a scratch tmpdir from the cache and
diffs the result against your working tree. Three drift kinds get
reported:

| Kind | Meaning |
|---|---|
| `unintegrated` | A `.apm/` source exists, but its deployed counterpart is missing. Fix: `apm install`. |
| `modified` | A deployed file's content differs from what install would produce. Fix: revert the hand-edit, or move it into source. |
| `orphaned` | A deployed file exists with no current source. Fix: `apm install` (orphan cleanup runs automatically). |

The replay is cache-only. It does no network I/O and does not write
to your project. If the cache is missing the entries the lockfile
references, the audit fails fast with a "run `apm install` first"
message rather than guessing.

For the full flag set, see `../../reference/cli/install/`.

## When something is off

| Symptom | What it means | What to do |
|---|---|---|
| `Content hash mismatch` on install | Lockfile-pinned content no longer matches what the source serves | Investigate upstream; if intended, run `apm install --update` |
| `cache pin mismatch` from `apm audit` | The cache holds content from a different `resolved_commit` than the lockfile records | `apm install` to repopulate the cache, then re-audit |
| `unintegrated` drift | Source committed without re-running install | `apm install`, then commit the regenerated `apm.lock.yaml` |
| `modified` drift | Hand-edit to a generated file | Revert, or move the change into the corresponding `.apm/` source |
| Hidden-Unicode finding from `apm audit` | A primitive file contains invisible characters | `apm audit --strip` to remove them, then re-audit |

None of these require disabling a check. The escape hatches
(`--update`, `--no-drift`) exist for narrow cases and always require
an explicit flag.
