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
2. **`ci-integration.yml`** - Tier 2, `merge_group` trigger only
   - Builds the Linux binary once, inline, then runs smoke, duration-sharded
     hermetic integration (`scripts/ci_sharding.py`, shard count planned
     while the binary builds), a retriable network lane, release validation
     and the full Windows unit suite in three shards (`Windows Unit Tests`),
     all against the tentative merge commit.
   - Trust boundary is the write-access grant (only users with write can
     enqueue a PR). No environment approval gate.
   - Inlines the binary build instead of fetching from `ci.yml` to avoid
     cross-workflow artifact plumbing across triggers.
   - **Never add a `pull_request` or `pull_request_target` trigger here.**
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
4. **`build-release.yml`** - `push` to main, tags, schedule, `repository_dispatch`
   - A `release-plan` job (`scripts/release_plan.py`) picks the mode: **push** (main: unit tests + Linux/Windows binaries), **full** (schedule/dispatch: every lane; a green full run is promotion evidence for its SHA), or **promoted** (tag whose SHA has evidence: build, smoke, release validation; no repeated unit/integration suites). A tag without evidence fails closed; the one-tag override is the repository variable `APM_RELEASE_FULL_VALIDATION_TAG`.
   - Builds run per platform (`build-linux-x86`, `build-linux-arm`, `build-windows`), so each platform's integration and release validation start when its own binary exists. Linux integration is duration-sharded (x86_64 runs the hermetic corpus incl. `architecture_contract`; arm64 deselects those platform-agnostic contracts); network-dependent tests run once in a lane that retries only failed tests.
   - **macOS** stays at one Intel and one ARM job per run (capacity), only on full and promoted runs. Full runs add a platform-sensitive integration subset: ARM `(lifecycle_smoke or windows_compat or platform_sensitive)`, Intel `(windows_compat or platform_sensitive)`; ARM is the macOS unit-test authority.
   - Publication jobs do not receive `ADO_APM_PAT`; live ADO PAT acceptance is an explicit `ado_pat_e2e` mode in `auth-acceptance.yml`. Full 5-platform binary output (linux x86_64/arm64, darwin x86_64/arm64, windows x86_64).
5. **`ci-runtime.yml`** - nightly schedule, manual dispatch, path-filtered push
   - **Linux x86_64 only**. Live inference smoke tests (`apm run`) isolated from release pipeline.
   - Uses `GH_MODELS_PAT` for GitHub Models API access.
   - Failures do not block releases - annotated as warnings.

## Platform Testing Strategy
- **PR time**: Linux-only combined build-and-test in `ci.yml`. Catches logic bugs and dependency issues before merge. Windows + macOS are tested post-merge (platform-specific issues are rare and the full matrix runs on every push to main).
- **Merge queue**: Linux integration (sharded) plus the full Windows unit suite (sharded) gate the merge.
- **Post-merge**: Linux x86_64/arm64 and Windows unit + build on every push; nightly full runs add macOS and every integration lane.
- **Rationale**: ci.yml has always been Linux-only - Windows and macOS are covered by `build-release.yml` on every push to main. This keeps PR feedback fast while still catching platform issues before release.

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
- **Merge queue workflow**: ci.yml (Tier 1 against tentative merge ref) + ci-integration.yml (Tier 2: build -> smoke-test -> integration-tests -> release-validation). Queue auto-merges on success; ejects on failure.
- **Push/Release workflow (Linux + Windows)**: release-plan -> build-<platform> -> (integration-<platform> shards, release-validation) per platform -> integration-tests summary -> create-release -> publish-pypi (gh-aw-compat runs in parallel, informational). A promoted tag skips the integration lanes.
- **Homebrew distribution**: `microsoft/homebrew-apm` polls the latest public APM release and updates its formula with its own `GITHUB_TOKEN`; this workflow must not send a cross-repository dispatch or hold a tap credential.
- **Push/Release workflow (macOS)**: build-and-validate-macos-intel / -arm (full and promoted runs only: build + startup smoke + release validation; full runs add unit tests on ARM and the platform-sensitive integration subset) -> create-release
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
- **macOS as root nodes**: macOS consolidated jobs start immediately - no dependency on Linux/Windows test completion.
- **Duration-balanced shards**: `scripts/ci_sharding.py` keeps xdist groups and modules whole, balances by `.github/ci-durations/*.json` (nightly publishes fresh `integration-durations`), and plans the shard count from a target per-shard wall time.
- **Native uv caching**: `setup-uv` action with `enable-cache: true` replaces manual `actions/cache@v3` blocks.
- **Targeted setup-node usage**: Node.js is only installed in `ci-runtime.yml`, macOS consolidated jobs, and integration-tests/release-validation phases (for `apm runtime setup copilot` -> npm install).
- **macOS runner consolidation**: Each macOS arch has a single consolidated job (build + platform-sensitive integration + release-validation), on full and promoted runs only. Never add macOS jobs or matrices: macOS capacity is scarce; reduce the work per job instead.
- **Tier 2 runs once per merged PR**, not per WIP push, since it triggers on `merge_group` only. Saves the bulk of integration minutes that the previous per-push flow burned.
- UPX compression when available (reduces binary size ~50%)
- Python optimization level 2 in PyInstaller
- Aggressive module exclusions (tkinter, matplotlib, etc.)