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
- A lockfile written by a newer APM is refused, naming both versions
  and how to upgrade.

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
    Use 'apm update owner/repo' to accept new content for this package and re-pin it in the lockfile.
```

Exit code is non-zero and the partially-downloaded directory is
removed before APM exits. Source: `src/apm_cli/install/sources.py`.

The opt-in escape hatch is `apm update <pkg>` (`apm update -g <pkg>` at
user scope), which re-resolves that package and accepts its new content
into the lockfile. A transitive dependency cannot be targeted, so its
hint names the full `apm update`. Use it when you
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
recording the `resolved_commit` it deployed. Before resolving
dependencies, install reuses a package copy in `apm_modules/` only when
its marker (or, without one, its content hash) matches `apm.lock.yaml`;
a copy left by an older lock is re-downloaded first, so its outdated
`apm.yml` never decides transitive selections. `apm audit` re-checks
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
  not configure it for. See [Authentication](../authentication/).

What these checks do **not** cover, by design:

- Hidden Unicode inside primitive content (a prompt that displays as
  one thing and parses as another). Install's separate pre-deploy scan
  blocks critical findings; the `apm audit` scan described below reports
  and strips them.
- Org policy (allow-listed sources, forbidden primitives, scope
  restrictions). Policy enforcement is an enterprise concern; see
  [Security model](../../enterprise/security/)
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
diffs the result against your working tree. Five drift kinds get
reported:

| Kind | Meaning |
|---|---|
| `unintegrated` | A `.apm/` source exists, but its deployed counterpart is missing. Fix: `apm install`. |
| `modified` | A deployed file's content differs from what install would produce. Fix: revert the hand-edit, or move it into source. For edits to a remote dependency's files, the text output also suggests [`apm export-patch`](../../reference/cli/export-patch/) to export them as upstream patches before the next install overwrites them. |
| `orphaned` | A deployed file exists with no current source. Fix: `apm install` (orphan cleanup runs automatically). |
| `unrecorded` | Install replay produces the same normalized bytes as the project, but no exact or directory `deployed_files` claim covers the path. Shared merge-hook configs compare only APM-owned entries, so user hooks do not create drift; the APM-owned sidecar remains byte-for-byte checked. Fix: `apm install`, then commit the regenerated `apm.lock.yaml`. |
| `unmanaged-conflict` | A replayed file differs at a path no lockfile entry claims (a skipped user-authored file, a refused skill, or a case-only name variant). Fix: rename the local file or correct the skill/target selection; ordinary `apm install` does not repair it. |

When your project has `.apm/` content, install does not promote a sibling
`skills/<name>/SKILL.md` at the project root into a project-local skill. Audit
replays the same interpretation instead of reporting that skill as `unintegrated`.
Other [source layouts](../../producer/pack-a-bundle/#source-layout-and-install-time-discovery)
and dependency skill bundles keep their existing discovery behavior.

Bare `apm audit` keeps the replay cache-only: it does no network I/O and
does not write to your project. If the cache is missing the entries the
lockfile references, the command reports an informational "run
`apm install` first" skip instead of guessing. `apm audit --ci` is
stricter: when `apm_modules/` is absent but `apm.lock.yaml` is present, it
self-hydrates a lock-pinned scratch replay for `config-consistency` and
drift without mutating the checkout, and fails closed only if that replay
cannot be materialized.

For the full flag set, see [CLI audit](../../reference/cli/audit/).

## When something is off

| Symptom | What it means | What to do |
|---|---|---|
| `Content hash mismatch` on install | Lockfile-pinned content no longer matches what the source serves | Investigate upstream; if intended, run `apm update` |
| `cache pin mismatch` from `apm audit` | The cache holds content from a different `resolved_commit` than the lockfile records | `apm install` to repopulate the cache, then re-audit |
| `unintegrated` drift | Source committed without re-running install | `apm install`, then commit the regenerated `apm.lock.yaml` |
| `modified` drift | Hand-edit to a generated file | Revert, or move the change into the corresponding `.apm/` source; for a remote dependency's files, run `apm export-patch` to send the edit upstream |
| `unrecorded` drift | Deployed targets committed without the regenerated lockfile | `apm install`, then commit `apm.lock.yaml` in the same change |
| Hidden-Unicode finding from `apm audit` | A primitive file contains invisible characters | `apm audit --strip` to remove them, then re-audit |

None of these require disabling a check. The escape hatches
(`apm update <pkg>`, `apm audit --no-drift`) exist for narrow cases and
always require an explicit command or flag.
