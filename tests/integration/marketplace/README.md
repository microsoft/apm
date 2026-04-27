# Marketplace Integration Test Suite

This document describes the three-tier test strategy for the
`apm marketplace` command group.  It is intended for maintainers
and contributors who need to run, extend, or triage marketplace tests.

---

## 1. Test Tier Overview

### Tier 1 -- Unit (tests/unit/marketplace/)

Scope: every function, class, and edge-case in the marketplace library
modules (`builder`, `ref_resolver`, `yml_schema`, `semver`,
`tag_pattern`, `publisher`, `pr_integration`, `init_template`,
`git_stderr`, `errors`).

All external I/O is replaced by mocks.  `git ls-remote` is never called.
No files are created on disk.

Run command:

    uv run pytest tests/unit/marketplace/ -x -q

Expected runtime: < 10 seconds.

### Tier 2 -- Integration (this directory)

Scope: end-to-end CLI command behaviour on a real temp filesystem, with
`git ls-remote` replaced by a patch on `RefResolver.list_remote_refs`.

What is tested:
- Real YAML parsing of `marketplace.yml` written to a tmp_path.
- Actual `MarketplaceBuilder` pipeline (load, resolve via mock, compose,
  write) producing a `marketplace.json` on disk.
- CLI exit codes, stdout/stderr content, and `marketplace.json` content.
- Anthropic golden-file assertion: canonical input -> byte-exact output
  against `tests/fixtures/marketplace/golden.json`.
- Dry-run flag (file not written), schema errors (exit 2), offline mode.
- `init`, `outdated`, `check`, `doctor`, and `publish` command paths.

What is NOT tested here:
- Real network calls to github.com.
- Real `gh` CLI invocations for PRs.
- Any secrets or tokens.

Run command:

    uv run pytest tests/integration/marketplace/ -x -q

Expected runtime: < 30 seconds.

### Tier 3 -- Live e2e (test_live_e2e.py)

Scope: full round-trip against a real remote marketplace repository on
GitHub.  Requires the `APM_E2E_MARKETPLACE` environment variable.

What is tested:
- `apm marketplace build` resolves real tags from the remote repo.
- `apm marketplace outdated` reports upgrades correctly.
- `apm marketplace check` exits 0 for reachable entries.
- `apm marketplace doctor` exits 0 with git and network available.

What is NOT tested here:
- `apm marketplace publish` -- publish writes to third-party repos and
  is intrinsically destructive.  Publish coverage stays at unit and
  integration tiers with a mocked publisher and PR integrator.

Default behaviour: ALL live tests are skipped when `APM_E2E_MARKETPLACE`
is unset.  CI never fails because of missing env vars.

Run command (maintainer only):

    export APM_E2E_MARKETPLACE=owner/your-marketplace-repo
    uv run pytest tests/integration/marketplace/test_live_e2e.py -v

Expected runtime: 30-120 seconds (depends on network and remote ref count).

---

## 2. Why the Live Tier Is Env-Var-Gated

The live tier runs `git ls-remote` against a real GitHub remote.  This:

- Consumes GitHub anonymous rate limits (60 req/hour) in CI.
- Requires a repo that stays stable and public.
- Is non-deterministic: tags on the remote can change.

By gating on `APM_E2E_MARKETPLACE`, the live tests are:
- Invisible to CI unless a maintainer explicitly opts in.
- Safe to run locally against any marketplace repo the maintainer controls.
- Documented in a single env var so they are easy to discover.

The `live_marketplace_repo` fixture in `conftest.py` validates that the
env var value is in `owner/repo` format and raises `pytest.skip` with a
clear message if the var is absent.

---

## 3. Coverage Matrix

| Command | Unit | Integration | Live e2e |
|---------|------|-------------|----------|
| build   | X    | X           | E        |
| outdated| X    | X           | E        |
| check   | X    | X           | E        |
| init    | X    | X           | --       |
| doctor  | X    | X           | E        |
| publish | X    | X           | --       |

Key:
- X  = covered.
- -- = not applicable (init and publish have no safe live test).
- E  = env-var-gated (`APM_E2E_MARKETPLACE` must be set).

---

## 4. Anthropic Compliance Check

The golden-file assertion is the contract between `MarketplaceBuilder`
and the Anthropic schema.  The fixture lives at:

    tests/fixtures/marketplace/golden.json

The integration test `test_build_integration.py::TestBuildGoldenFile`
writes a canonical `marketplace.yml` input, runs the full build
pipeline with refs mocked to return the exact SHAs in the fixture, and
then asserts byte-level equality between the produced `marketplace.json`
and the golden file.

If the golden file is updated, the integration test must be re-run to
confirm the new file still passes.  The key ordering contract is:
`name -> description -> version -> owner -> metadata -> plugins`.
Each plugin must have `name -> (description) -> tags -> source`.
Each source must have `type -> repository -> (path) -> ref -> commit`.

APM-only keys (`subdir`, `version`, `ref` in yml, `tag_pattern`,
`include_prerelease`) must NEVER appear in `marketplace.json`.

---

## 5. Failure Triage Guide

| Symptom | First suspect | Action |
|---------|--------------|--------|
| Unit tests fail | Library logic | Check the specific unit test file in tests/unit/marketplace/. |
| Integration tests fail on yml parse | yml_schema.py | Confirm the test fixture YAML is valid. |
| Integration tests fail on JSON content | builder.compose_marketplace_json | Check key order and golden fixture. |
| Integration tests fail on exit code | CLI command handler | Inspect the sys.exit() paths in commands/marketplace.py. |
| Integration tests fail on mock | conftest.py fixture | Confirm mock_ref_resolver patches the right import path. |
| Live tests fail on resolution | Real remote | Check that APM_E2E_MARKETPLACE points to a valid repo with tags. |
| Live tests fail on timeout | Network or rate limit | Increase timeout or set GITHUB_TOKEN to raise rate limit. |
| Golden file mismatch | Builder output | Compare actual vs golden with `diff`. Update golden if intentional. |

---

## 6. Adding a New Test

1. Identify the tier: does it need real disk I/O? -> integration.  Does it
   need a real remote? -> live.  Otherwise -> unit.
2. Use the fixtures from `conftest.py` (`mkt_repo_root`, `mock_ref_resolver`,
   `live_marketplace_repo`).
3. Follow the one-style-per-file convention: use `subprocess.run` (via
   `run_cli`) when the test needs real CWD/env handling; use `CliRunner`
   when the test only needs to inspect output strings.
4. Assert both the exit code and the output content.
5. Name tests as `test_<command>_<scenario>` for discoverability.

---

## 7. Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| APM_E2E_MARKETPLACE | Enables live tier; value is `owner/repo`. | Unset (skips live tests). |
| GITHUB_TOKEN | Raises GitHub rate limit from 60 to 5000 req/hour. | Unset (anonymous). |

---

_This file is maintained alongside the test suite.  Update it when
adding new commands or tiers._
