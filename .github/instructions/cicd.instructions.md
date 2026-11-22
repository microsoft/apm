---
applyTo: ".github/workflows/**"
description: "CI/CD Pipeline configuration for PyInstaller binary packaging and release workflow"
---

# CI/CD Pipeline Instructions

## Workflow Architecture (Tiered + Merge Queue)
Five workflows split by trigger and tier. PRs get fast feedback; the heavy
integration suite runs only at merge time via GitHub Merge Queue
(microsoft/apm#770).

1. **`ci.yml`** - Tier 1, runs on `pull_request` AND `merge_group`
   - **Linux-only** (ubuntu-24.04). Combined `build-and-test` job: unit tests + binary build in a single runner. No secrets needed.
   - Uploads Linux x86_64 binary artifact for downstream integration testing.
   - Runs in both PR context (fast feedback for contributors) and merge_group
     context (against the tentative merge commit before queue auto-merges).
2. **`ci-integration.yml`** - Tier 2, `merge_group`; also write-access `workflow_dispatch` (rehearse on a branch) and PRs labeled `full-integration` (merge-gate.yml then requires its checks at PR time)
   - **Linux-only**. Builds binary inline, then runs smoke + integration +
     release-validation against the tentative merge commit. Hermetic
     integration is sharded by the build-release planner
     (`scripts/ci_sharding.py`, at most 16 shards); network tests run in a
     retrying lane. `tests/quality/test_ci_topology.py` requires every
     merge_group `EXPECTED_CHECKS` name in `merge-gate.yml` to come from a
     merge_group job, and `merge-queue-freshness.yml` fails daily when
     `main` moved but no merge_group run completed in 3 days.
   - Trust boundary is the write-access grant (only users with write can
     enqueue a PR). No environment approval gate.
   - Inlines the binary build instead of fetching from `ci.yml` to avoid
     cross-workflow artifact plumbing across triggers.
   - **Never add `pull_request_target` or `push` here; `pull_request` stays label-gated on every job.**
     This file holds production secrets (`GH_CLI_PAT`, `ADO_APM_PAT`).
     Required-check satisfaction at PR time is handled by `merge-gate.yml`,
     which aggregates all required signals into a single `gate` check.
3. **`merge-gate.yml`** - single-authority PR-time aggregator
   - Triggers on `pull_request` only (single trigger - dual-trigger with
     `pull_request_target` produces SUCCESS+CANCELLED check-run twins via
     `cancel-in-progress` and poisons branch protection's rollup).
   - One job named `gate`. Polls the Checks API for all entries in the
     workflow's `EXPECTED_CHECKS` env var; aggregates pass/fail into a
     single check-run.
   - Branch protection requires ONLY this one check (`gate`). Adding,
     renaming, or removing an underlying check is a `merge-gate.yml` edit,
     never a ruleset edit. Tide / bors single-authority pattern.
   - Recovery if the `pull_request` webhook is dropped: empty commit,
     `gh workflow run merge-gate.yml -f pr_number=NNN`, or close+reopen.
   - `.github/CODEOWNERS` requires Lead Maintainer review for any change
     to `.github/workflows/**`.
4. **`build-release.yml`** - `push` to main, `v*` tags, nightly schedule, `repository_dispatch` (`manual-build-release`); no `workflow_dispatch`
   - **`release-plan`** (`scripts/release_plan.py`) picks a mode: `push` (unit tests + Linux/Windows binaries), `full` (schedule/dispatch: every lane; a green full run is promotion evidence for its SHA) or, for a `v*` tag, `promoted` (SHA has a green full run on main: build, smoke, release validation, publish) or full validation in the tag run before publishing. Releasing is always "push a tag"; nothing publishes without a green full validation of the exact SHA. A tag must point at a commit on `main` or a `release/*` branch (compare API, checked before evidence); any other tag, or a compare API error, fails the plan before any build or publish job.
   - **Linux + Windows** build in `build-linux-x86`, `build-linux-arm`, `build-windows` (check name `Build & Test`). Linux integration is duration-sharded (`integration-plan` + `scripts/ci_sharding.py`, seeds in `.github/ci-durations/`); network-dependent tests run in their own retrying lane; `Integration Tests Summary` requires every lane.
   - **macOS** never runs on push. Intel is one job running `windows_compat or platform_sensitive`. arm64 is two parallel jobs on the same SHA: `unit-macos-arm` (unit tests, full runs only) and `build-and-validate-macos-arm` (build, `lifecycle_smoke or windows_compat or platform_sensitive`, release validation). `create-release` requires both (the unit job may be skipped only on a promoted tag).
   - **Windows units on push** run as three seed-balanced shards (`windows-unit-1..3`); `build-windows-push` builds only if the plan, installer and every shard succeeded.
   - Publication jobs do not receive `ADO_APM_PAT`; live ADO PAT acceptance is an explicit `ado_pat_e2e` mode in `auth-acceptance.yml`. Full 5-platform binary output (linux x86_64/arm64, darwin x86_64/arm64, windows x86_64).
5. **`ci-runtime.yml`** - nightly schedule, manual dispatch, path-filtered push
   - **Linux x86_64 only**. Live inference smoke tests (`apm run`) isolated from release pipeline.
   - Uses `GH_MODELS_PAT` for GitHub Models API access.
   - Failures do not block releases - annotated as warnings.
6. **`main-architecture-ratchets.yml`** - `push` to main only
   - Re-runs ci.yml's `test-architecture-contracts` job (check `Architecture Ratchet Contracts (main)`), so a change that reaches main without a `merge_group` run cannot leave main silently red on the ratchet contracts.
   - Never opts into provisional baselines. `tests/quality/test_ci_topology.py` pins the job to ci.yml's, so the two cannot drift.
7. **`merge-train.yml`** - `push` to `queue/t*/p*` and `queue/b*/p*` only, plus `workflow_dispatch` (#4574)
   - Proves one exact speculative SHA (main plus queued PRs) and reports ONE check, `train-gate`. The ref suffix sets the scope: `-guard` runs `scope`, `guard` and `train-gate`; `-full` also runs `tier1` (ci.yml), `tier2` (ci-integration.yml, `secrets: inherit`) `spec` (spec-conformance.yml) and `owner` (owner-surface.yml) through their `workflow_call` triggers.
   - `guard` runs the `.github/actions/lint-chain` composite (the single owner of the Lint steps, also used by ci.yml's `Lint` job), the ratchet contracts, the other `tests/quality` modules and the NOTICE check. The base is the head commit's `Queue-Base:` trailer, else the merge base with `main`.
   - `train-gate` (`if: always()`) passes only when every job is green, with the tiers skipped on `-guard`, or when every failure is an inherited node id: `scripts/merge_queue/gate.py` subtracts the base probe's `train-failures.json` and publishes the run's own (`actions: read`). A full proof is a successful run on the `-full` branch at that exact SHA (`gh run list --workflow merge-train.yml --branch <full ref> --commit <sha>`), not a `train-gate` lookup by SHA alone. Every job checks out the triggering commit only. A manual probe dispatches with `--ref <queue branch>`; the workflow takes no inputs and `scope` fails on any other ref. A `Queue-Base:` trailer must equal the merge base with main.
   - Queue refs are real branches. Train jobs restore caches (main's included) read-only: merge-train's setup-uv sets `save-cache: false`; called ci.yml and ci-integration.yml jobs can save only into the queue branch's scope, never main's.
   - `cleanup` (`if: always()`, after `train-gate`, the only job with `contents: write`) deletes every train branch; it is not part of the verdict. Every job skips the `push` a branch deletion fires (`github.event.deleted`). Each run has its own concurrency group and is never cancelled.
   - Never add `pull_request_target` or another push branch. `tests/quality/test_ci_topology.py` pins the triggers, the merge_group coverage of a full train, the gate and the lint-chain owner.
8. **`owner-surface.yml`** - `pull_request`, `merge_group` and `workflow_call` (the merge train's `owner` tier)
   - `Owner Surface Check` runs the integration slice an owner or install-path change can reach (`scripts/owner_surface.py`: owner-rule mutation cases and `test_architecture_*.py` files naming a changed file or affected owner, guard or rule, plus `test_install_*.py` for install changes), in 3 shards. No `paths:` filter, and every job runs on every event: only the slice's steps are gated on `run`, so off a pull request the slice is a fast no-op and the check passes.
   - Required: `merge-gate.yml` lists `Owner Surface Check` in every `EXPECTED_CHECKS` context, and a full merge train runs it as its `owner` tier.
9. **`main-push-gate.yml`** - `push` to main plus `workflow_dispatch` (S28)
   - Calls ci.yml (`tier1`: all Tier 1 jobs, every Lifecycle Smoke shard) and ci-integration.yml (`tier2`: every Integration Tests shard, `secrets: inherit`) through `workflow_call` on main's head, with the push's `before` SHA (else `HEAD^1`) as the lint base. Its `main-push-gate` aggregate (`if: always()`) fails the run when any tier job fails, so main is visibly red. Not a required check; runs are never cancelled.
   - No new default-branch cache saves: the setup-uv steps of ci.yml and ci-integration.yml set `save-cache` to `auto` only on pull_request and merge_group (`false` otherwise), setup-node sets `package-manager-cache: false`, and ci-integration.yml saves its APM fetch cache on no `push`. `tests/quality/test_ci_topology.py` fails if any step reachable from a main push would save a cache.

## Platform Testing Strategy
- **PR time**: Linux-only combined build-and-test in `ci.yml`. Catches logic bugs and dependency issues before merge. Windows is tested on every push to main; macOS and full integration run nightly, on dispatch and on release tags.
- **Post-merge**: Linux and Windows unit tests and binaries on every push; the full 5-platform matrix (linux x86_64/arm64, darwin x86_64/arm64, windows x86_64) runs on full runs (nightly, dispatch) and is promotion evidence for tags.
- **Rationale**: ci.yml has always been Linux-only - Windows and macOS are covered by `build-release.yml`. This keeps PR feedback fast while still catching platform issues before release.

## PyInstaller Binary Packaging
- **CRITICAL**: Uses `--onedir` mode (NOT `--onefile`) for faster CLI startup performance
- **Binary Structure**: Creates `dist/{binary_name}/apm` (nested directory containing executable + dependencies)
- **Platform Naming**: `apm-{platform}-{arch}` (e.g., `apm-darwin-arm64`, `apm-linux-x86_64`)
- **Spec File**: `build/apm.spec` handles data bundling, hidden imports, and UPX compression

## Artifact Flow Quirks
- **Upload**: Artifacts include both binary directory + test scripts for isolation testing
- **Download**: GitHub Actions creates nested structure: `{artifact_name}/dist/{binary_name}/apm`
- **Release Prep**: Extract binary from nested path using `tar -czf "${binary}.tar.gz" -C "${artifact_dir}/dist" "${binary}"`

## Critical Testing Phases
1. **Integration Tests**: Full source code access for comprehensive testing
2. **Release Validation**: ISOLATION testing - no source checkout, validates exact shipped binary experience
3. **Path Resolution**: Use symlinks and PATH manipulation for isolated binary testing

## Inference Testing (Decoupled)
- Live inference tests (`apm run`) are **isolated** in `ci-runtime.yml` - they do NOT gate releases
- `APM_RUN_INFERENCE_TESTS=1` env var enables inference in test scripts; absent = skipped
- `GH_MODELS_PAT` is only used in `ci-runtime.yml` and Tier 2 smoke-test job - NOT in integration-tests or release-validation
- Rationale: 8 inference executions x 2% failure rate = 14.9% false-negative per release; APM core UVPs require zero live inference

## Release Flow Dependencies
- **PR workflow**: Tier 1 only - ci.yml (build-and-test, Linux-only) provides fast feedback. Tier 2 does not run until enqueued.
- **Merge queue workflow**: ci.yml (Tier 1 against tentative merge ref, including `Coverage Combine (Linux)`, the global 80% floor) + ci-integration.yml (Tier 2: build + shard plan -> smoke-test -> planned integration shards + network lane -> integration-tests; release-validation in parallel). Queue auto-merges on success; ejects on failure.
- **Push/Release workflow (Linux + Windows)**: release-plan -> build-linux-x86/build-linux-arm/build-windows -> sharded integration lanes + release-validation -> create-release (pushed tag only) -> publish-pypi (gh-aw-compat runs in parallel, informational)
- **Homebrew distribution**: `microsoft/homebrew-apm` polls the latest public APM release and updates its formula with its own `GITHUB_TOKEN`; this workflow must not send a cross-repository dispatch or hold a tap credential.
- **Push/Release workflow (macOS)**: build-and-validate-macos-intel and build-and-validate-macos-arm (full and promoted runs only; marker subsets on full runs) -> create-release
- **Live ADO PAT acceptance**: manual `auth-acceptance.yml` dispatch with `ado_pat_e2e: true` -> exact `live and requires_ado_pat` marker intersection. Invalid credentials fail with production auth diagnostics but do not block publication.
- **Tag Triggers**: Only `v*.*.*` tags trigger full release pipeline
- **Artifact Retention**: 30 days for debugging failed releases
- **Cross-workflow artifacts**: ci-integration.yml builds the binary inline (no cross-workflow artifact transfer); build-release.yml jobs share artifacts within the same workflow run.

## Branch Protection & Required Checks
- **Single required check**: branch protection (`main-protection` ruleset id 9294522) requires exactly one status check context: `gate` from `merge-gate.yml`. All other PR-time signals are aggregated by that workflow's poll loop.
- **CRITICAL ruleset gotcha**: the ruleset `context` must be the literal check-run name `gate`. `Merge Gate / gate` is only how GitHub may render the workflow and job together in the UI; it is not the context value to store in the ruleset. If the ruleset stores `Merge Gate / gate`, GitHub waits forever with "Expected - Waiting for status to be reported" because no check-run with that literal name is posted.
- **How the name is derived**: GitHub matches the check by `integration_id` (`15368` = github-actions) plus the emitted check-run name. That emitted name comes from the job `name:` if one is set; otherwise it falls back to the job id. In `merge-gate.yml` the job id is `gate` and `name: gate`, so the emitted check-run name is `gate` -- that is the exact string the ruleset must require.
- **Adding a new aggregated check**: add it to `EXPECTED_CHECKS` in `merge-gate.yml`. Do not change the ruleset unless you intentionally rename the merge gate job's emitted check-run name, in which case the ruleset `context` must be updated to the new exact name.

## Trust Model
- **PR push (any contributor, including forks)**: Runs Tier 1 only. No CI secrets exposed. PR code is checked out and tested in an unprivileged context.
- **merge_group (write access required)**: Runs Tier 1 + Tier 2. Tier 2 sees secrets. The `gh-readonly-queue/main/*` ref is created by GitHub from the PR merged into main; only users with write access can trigger this by enqueueing a PR.
- **Trust boundary = write-access grant**, managed in repo Settings -> Collaborators. Write access is granted only to vetted contributors.
- **No environment approval gate** is required because the act of enqueueing IS the trust assertion. This replaces the previous `integration-tests` environment approval flow.

## Key Environment Variables
- `PYTHON_VERSION: '3.12'` - Standardized across all jobs
- `GITHUB_TOKEN` - Fallback token for compatibility (GitHub Actions built-in)
- `APM_RUN_INFERENCE_TESTS` - When `1`, enables live inference tests in validation scripts

## Performance Considerations
- **Combined build-and-test**: Eliminates ~1.5m runner re-provisioning overhead by running unit tests and binary build in the same job.
- **macOS as root nodes**: macOS jobs (including the arm64 unit job) start immediately - no dependency on Linux/Windows test completion.
- **Native uv caching**: `setup-uv` action with `enable-cache: true` replaces manual `actions/cache@v3` blocks.
- **Targeted setup-node usage**: Node.js is only installed in `ci-runtime.yml`, macOS consolidated jobs, and integration-tests/release-validation phases (for `apm runtime setup copilot` -> npm install).
- **macOS runner consolidation**: Each macOS arch has a single consolidated job (build + integration + release-validation); arm64 runs its unit tests in a parallel `unit-macos-arm` job to shorten the lane. Intel (`build-and-validate-macos-intel`) runs on every push since Intel runners are plentiful and uses marker-bounded non-live lifecycle integration coverage for promotions. ARM (`build-and-validate-macos-arm`) is gated to tag/schedule/dispatch only since ARM runners are extremely scarce (2-4h+ queue waits) and remains the full non-live corpus macOS authority. This avoids serial re-queuing of runners across multiple jobs and redundant corpus replay on Intel.
- **Unit tests skip macOS**: Python unit tests are platform-agnostic; Linux + Windows coverage is sufficient. macOS-specific validation (binary build, focused Intel integration, full non-live ARM integration, and release validation) still runs via the consolidated jobs.
- **Tier 2 runs once per merged PR**, not per WIP push, since it triggers on `merge_group` only. Saves the bulk of integration minutes that the previous per-push flow burned.
- UPX compression when available (reduces binary size ~50%)
- Python optimization level 2 in PyInstaller
- Aggressive module exclusions (tkinter, matplotlib, etc.)