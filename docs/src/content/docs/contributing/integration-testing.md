---
title: "Integration Testing"
sidebar:
  order: 3
---

APM integration testing covers PR checks, merge queue runs, runtime smoke, and release qualification.

## Testing Strategy

APM uses a tiered approach to integration testing:

### 1. **Core Smoke** (merge queue and release builds)
- **Location**: `tests/integration/test_core_smoke.py`
- **Purpose**: fast verification that the built native binary starts and satisfies core CLI contracts
- **Scope**: binary startup, `--version`, `--help`, and network-free core behavior
- **Trigger**: merge queue integration workflow and each native release-platform build

Runtime installation smoke remains in `tests/integration/test_runtime_smoke.py`
and runs only in the runtime workflow or full integration selections that opt in
to the required runtimes.

### 2. **End-to-End Golden Scenario Tests** (merge queue and promotion runs)
- **Location**: `tests/integration/test_golden_scenario_e2e.py`
- **Purpose**: Complete verification of the README golden scenario
- **Scope**:
  - Full runtime setup and configuration
  - Project initialization (`apm init`)
  - Dependency installation (`apm install`)
  - Real API calls to GitHub Models
  - Runtime-specific execution when the selected lane provisions that runtime
- **Duration**: captured in hosted timing artifacts
- **Trigger**: merge queue integration workflow, plus tag, schedule, and repository-dispatch release qualification runs

### 3. **Lifecycle Smoke** (PR-time required check)
- **Location**: selected declaratively via `lifecycle_smoke and not lifecycle_merge_group`. Tests marked `lifecycle_merge_group` remain outside the bounded required set.
- **Purpose**: Promote a stable, hermetic slice of Consume/Produce/Govern lifecycle contracts onto the PR-time critical path, so regressions in install, lock, deployment ownership, compile, pack, prune, uninstall, audit, and repair fail the PR.
- **Scope**: the family contains a static authority guard plus content-hash, policy, hook, virtual-package, audit, auth, and installed-console rows. Real subprocess cases use the uv-installed `apm` command and local Git. This is not frozen PyInstaller coverage.
- **Prerequisites**: the pytest step sets `APM_E2E_TESTS=1` so subprocess rows execute. `APM_RUN_INTEGRATION_TESTS` remains unset, the socket guard denies network sockets, and the job binds no credentials.
- **Duration**: the required expression must remain inside its hard 6-minute job timeout; hosted duration is authoritative.
- **Trigger**: every pull request and merge queue run (`ci.yml`'s `lifecycle-smoke` job, required via `merge-gate.yml`)
- **Selection mechanism**: `pytest --strict-markers -m 'lifecycle_smoke and not lifecycle_merge_group' tests/integration` -- declarative, not a file/node-id list. No central count or membership list is maintained.
- **Full-coverage path**: merge-group workflow `ci-integration.yml` calls the shared `release-integration.yml` shard runner. Its Unix step invokes `scripts/test-integration.sh` over `tests/integration/`, so the complete lifecycle family remains exercised.
- **Drift guard**: `tests/quality/test_ci_topology.py` independently collects the full, merge-group-only, and required selections; verifies their set partition; and preserves the required expression, full-integration execution path, step-level `APM_E2E_TESTS: "1"` binding, network/credential prohibitions, and required-check membership.
- **Fixture controls**: lifecycle helpers set `APM_TEST_LOOPBACK_PORTS` for a port-scoped local registry and `APM_TEST_FAIL_LOCK_REPLACE=1` for atomic-write fault injection. These are internal test controls, not user-facing APM settings.
- **Learning ledger**: `tests/fixtures/lifecycle_bug_ledger.json` maps representative escaped defects to generalized laws, oracle tiers, phases, and executable regression node IDs, including coverage of already-correct behavior. Use same-workspace transitions to prove survivor ownership and scoped cleanup. It is not a bug-count census; `tests/quality/test_lifecycle_bug_ledger.py` validates its taxonomy and links.
- **Generated lifecycle model**: `test_generated_lifecycle_state_machine.py` uses Hypothesis to generate guarded install, dry-run, audit, tamper, repair, declaration, and prune sequences against the real CLI. The model tracks declaration, materialization, integrity, and lock state independently of the product lockfile. Every transition captures complete project and user roots, and mutating commands must stay inside reviewed write sets. It stays in the merge-group family until hosted runtime supports promotion to the bounded PR-time smoke set.
- **Known gap**: a late lockfile replacement failure can leave target files on the newly declared target while retaining the prior lockfile. The required lifecycle suite bounds that blast radius and proves the next install converges; expanding the install transaction is a separate design decision recorded in the ledger.
- **Run it locally** (the exact command CI runs):
  ```bash
  APM_E2E_TESTS=1 uv run --extra dev pytest -p no:cacheprovider -q --strict-markers \
    -n 2 --dist loadgroup \
    -m 'lifecycle_smoke and not lifecycle_merge_group' tests/integration
  ```

### 4. **Live Guardrailing Hero** (scheduled/repository dispatch)
- **Location**: `tests/integration/test_guardrailing_hero_e2e.py`
- **Purpose**: Preserve the real remote, token-gated packaged CLI hero without multiplying it across the default packaged platform matrix
- **Scope**: project initialization, two GitHub-backed installs, compile/deploy, and prompt startup through the built Linux x64 binary
- **Trigger**: one `ci-runtime.yml` invocation on schedule or repository dispatch that fails the workflow on hero or runtime-smoke errors; never pull requests or the generic integration script
- **Selection mechanism**: the explicit test node with `-m live`; collection gates remain owned by `tests/integration/conftest.py`

Nightly runtime smoke sets the required live opt-ins explicitly
(`APM_E2E_TESTS=1`, `APM_RUN_INTEGRATION_TESTS=1`, and tokens). The separate
inference-validation step is annotated but non-blocking.

## Running Tests Locally

Integration tests live under `tests/integration/` and run via `pytest`
directly. Each test module declares the preconditions it needs as
standard pytest markers; the registry in
`tests/integration/conftest.py` (`_MARKER_CHECKS`) automatically skips
tests whose precondition is not met, so you only have to install/set
what the test family you want actually requires.

### The marker registry

| Marker | Precondition | How to satisfy it |
| --- | --- | --- |
| `requires_e2e_mode` | Opt-in for the heavyweight golden-scenario suite | `export APM_E2E_TESTS=1` |
| `requires_network_integration` | Opt-in for tests that hit live registries | `export APM_RUN_INTEGRATION_TESTS=1` |
| `requires_windows` | A Windows-only process or filesystem boundary | Run on Windows |
| `requires_inference` | Opt-in for tests that call inference APIs | `export APM_RUN_INFERENCE_TESTS=1` |
| `requires_github_token` | A token usable against `github.com` / GitHub Models | `export GITHUB_APM_PAT=...` (or `GITHUB_TOKEN`) |
| `requires_ado_pat` | Azure DevOps PAT for ADO host tests | `export ADO_APM_PAT=...` |
| `requires_ado_bearer` | Azure CLI signed in + opt-in flag | `az login` and `export APM_TEST_ADO_BEARER=1` |
| `requires_apm_binary` | A built `apm` binary on disk or `PATH` | `scripts/build-binary.sh` (or set `APM_BINARY_PATH`) |
| `requires_runtime_codex` | The `codex` runtime installed under `~/.apm/runtimes/` | `apm runtime setup codex` |
| `requires_runtime_copilot` | The GitHub Copilot CLI runtime installed under `~/.apm/runtimes/` | `apm runtime setup copilot` |
| `requires_runtime_llm` | The `llm` runtime installed under `~/.apm/runtimes/` | `apm runtime setup llm` |
| `live` | Tests that hit real third-party repositories; deselected by default | Override the deselect: `pytest -m live tests/integration -v` |

Without any of those env vars or runtimes, a local `pytest tests/integration`
invocation is silent rather than red: every test is collected and reported as
`SKIPPED` with a one-line reason, so you can see exactly what is missing and
why.

CI uses `--strict-runtime-prerequisites` for provisioned selections. If a
selected runtime test would otherwise skip because its runtime is missing,
collection fails instead. That prevents green builds caused by omitted runtime
setup.

### Three marker axes

Pytest markers compose across independent axes:

| Axis | Question | Markers |
| --- | --- | --- |
| Behavioral | What boundary does the test cross? | `unit`, `component`, `e2e` |
| Scheduling | When is the test selected? | `integration`, `slow`, `benchmark`, `live` |
| Prerequisite | What environment must exist? | `requires_*` |
| CI-selection | Is this test part of a named required CI gate? | `lifecycle_smoke` |

`live` is both an opt-in scheduling marker and an external-service
prerequisite. Behavioral markers do not replace prerequisite markers.
`lifecycle_smoke` is orthogonal to all three: it does not describe a
test's boundary, scheduling, or precondition, only that
`ci.yml`'s required `lifecycle-smoke` job selects it via
`-m lifecycle_smoke` (see Tier 3 above for the full rationale).

The behavioral definitions are:

| Marker | Definition |
| --- | --- |
| `unit` | Pure logic with no filesystem and no CLI |
| `component` | In-process behavior that touches a filesystem or one command boundary |
| `e2e` | A real installed CLI crossing at least one command boundary |

`pyproject.toml` owns these definitions.
Module-level `pytestmark` is the sole behavioral classification authority.
This is a marker-only behavioral taxonomy.
Every classified module declares exactly one behavioral marker, and every
collected node in that module must inherit the same classification. Function-
or class-level behavioral markers are rejected because they would split the
module's authority.

There is no central module whitelist or exact classified-module count. New
modules opt in by adding one module-level marker. The trade-off is deliberate:
APM gives up the old closed-set/count ratchet in exchange for distributed
ownership and removal of a central merge hotspot. Repository-wide collection
still rejects empty, mixed, and multiple classifications deterministically.

Directory names and `_e2e.py` suffixes are not proof of behavior.
`test_policy_pinned_constraint_e2e.py` is `component` because it uses Click
in-process; `test_core_smoke.py` is `e2e` because it invokes an installed
binary through subprocess boundaries.

To classify a module:

1. Confirm the whole module has one behavioral boundary.
2. Add exactly one module-level behavioral `pytestmark`, preserving scheduling and
   prerequisite markers.
3. Document why behavior wins if the filename suggests another boundary.
4. Run the contracts:

```bash
uv run --extra dev pytest -p no:cacheprovider -q tests/quality
uv run --frozen python scripts/check_test_assertions.py
uv run --frozen python scripts/check_exact_test_duplicates.py
```

The assertion and exact-duplicate baseline updaters only accept reductions.

```bash
uv run --frozen python scripts/check_test_assertions.py --update-baseline
uv run --frozen python scripts/check_exact_test_duplicates.py --update-baseline
```

Provisional mode is CI-only and allowed only on draft pull requests.
Contributor commands, ready pull requests, merge queue runs, and final
validation are strict. Do not pass the internal provisional flag manually;
remove `provisional` metadata after remeasurement and review.

### Common invocations

```bash
# Run everything you currently have the prerequisites for
uv run pytest tests/integration -v

# Run a single suite (the marker registry still applies)
uv run pytest tests/integration/test_golden_scenario_e2e.py -v

# Run only a marker family
uv run pytest tests/integration -m requires_github_token -v

# Run the bounded generated lifecycle model with a real local apm command
APM_E2E_TESTS=1 APM_BINARY_PATH="$(command -v apm)" \
  uv run --extra dev pytest -q \
  tests/integration/test_generated_lifecycle_state_machine.py
```

### Hermetic lifecycle fixtures

`tests/integration/test_hermetic_lifecycle_foundation.py` is the cross-module
contract. Complete the [development setup](../development-guide/) first.

| Utility | Owns | Contract test |
| --- | --- | --- |
| `isolated_apm_environment.py` | Child roots, environment, Python socket tripwire | `test_isolated_apm_environment_contract.py` |
| `local_git_repository.py` | Deterministic local Git origins | `test_local_git_repository_factory_contract.py` |
| `local_package.py` | Source-only package inputs | `test_local_package_factory_contract.py` |
| `apm_lifecycle_runner.py` | Bounded process execution and evidence | `test_apm_lifecycle_runner_contract.py` |
| `lifecycle_state.py` | Exact bytes and semantic durable-state receipts | `test_lifecycle_state_snapshot_contract.py` |
| `artifact_snapshot.py` | Read-only filesystem observations | `test_artifact_snapshot_contract.py` |
| `scenario_rows.py` | Immutable scenario data | `test_scenario_rows_contract.py` |

Source fixtures author only source inputs; the real APM CLI creates lockfiles,
deployed trees, compiled output, bundles, hashes, cache state, and audit
reports.

Use `ArtifactSnapshot` for one complete filesystem root and
`ArtifactSnapshotSet` when an operation can affect multiple isolated roots.
These open-world captures complement `LifecycleStateSnapshot`: the latter
explains semantic lock and deployment state, while the former catches stray
files even when the lockfile fails to record them.

Hypothesis failures print a minimized transition program that can be replayed
by running the failing test with the reported example. Keep the generated model
bounded and deterministic in CI (`database=None`, `derandomize=True`), and turn
every confirmed product defect into a named regression before extending the
ledger. The static scenario rows remain valuable for exact reproductions; the
generated model searches valid orderings that authored rows may miss.

`IsolatedApmEnvironment` builds deterministic child environments for
APM/Git/GitHub/ADO/GitLab/SSH flows with a best-effort Python socket guard.
The guard preserves native optional socket API availability: it defines
`sendmsg` only when the native socket supports it, keeping feature detection
platform-correct. It isolates APM, Git, GH, Azure, home, cache, and temporary
roots, not arbitrary variables; it is not a general credential scrubber.

For deeply nested fixtures that populate real sparse Git caches, use short
pytest-owned roots such as `tmp_path_factory.mktemp("r")` instead of
test-named `tmp_path` roots to avoid Git for Windows metadata path limits.
Retain worker-equivalent directory depth in focused gates so they still
exercise the paths used by the sharded suite.

When a fixture monkeypatches a temporary config path, reset the config cache at
setup and again in `finally`. Path monkeypatching alone does not invalidate
cached config in shard-exposed fixtures.
Import-fallback fixtures must also restore parent-package attributes, not just
`sys.modules`, so import spellings see the same module after teardown.

It is also not an OS/native-code sandbox: executables found through `PATH`
remain trusted, reflective access to CPython internals or native extensions can
bypass Python monkey-patches, `file://` access is not confined by the OS, and
hostile post-creation filesystem races are outside the contract.
`GIT_ALLOW_PROTOCOL=file` and local `url.*.insteadOf` rewriting separately
restrict Git transport in reviewed scenarios.

Keep modules flat. Inside a pytest test with `tmp_path` and
`apm_binary_path`, compose them directly:

```python
import os

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
environment = isolated.subprocess_env()
sources = LocalPackageFactory(isolated.package_root)
project = sources.create("consumer", targets=("copilot",))
sources.add_skill(
    project,
    "example",
    "---\nname: example\ndescription: Fixture\n---\n# Example\n",
)
result = ApmLifecycleRunner((str(apm_binary_path),)).run(
    ("install", "--target", "copilot"),
    cwd=project.root,
    env=environment,
)
snapshot = ArtifactSnapshot.capture(project.root)
assert result.returncode == 0
assert "apm.lock.yaml" in snapshot.paths
```

For auth-bearing remote-package scenarios, create a local origin with
`LocalGitRepositoryFactory` and pass the complete environment returned by
`url_rewrite_subprocess_env()` to `ApmLifecycleRunner`. That process-scoped Git
rewrite survives the production auth environment builder; do not hand-merge
`GIT_CONFIG_*` slots or rely on the older global-config-only rewrite.
`ApmLifecycleRunner((str(apm_binary_path),))` invokes the fixture-selected
console script. Packaged-binary tests belong to the separate platform lane.

`test_packaged_virtual_file_lifecycle_e2e.py`,
`test_deployed_files_e2e.py`, and
`test_silent_adopt_existing_files_e2e.py` are narrow hermetic packaged
counterparts to the live hero. They run the real binary against a local bare
Git origin through process-scoped URL rewriting, then check package
installation, deployment lifecycle, and exact lock provenance without
credentials or live HTTP.

```bash
# The three hermetic packaged counterparts (real binary, local file:// origin, no creds):
uv run pytest tests/integration/test_packaged_virtual_file_lifecycle_e2e.py -v
uv run pytest tests/integration/test_deployed_files_e2e.py -v
uv run pytest tests/integration/test_silent_adopt_existing_files_e2e.py -v

# Supporting hermetic foundation + contract suites:
uv run pytest tests/integration/test_local_package_factory_contract.py -v
uv run pytest tests/integration/test_hermetic_lifecycle_foundation.py -v
uv run pytest -n auto tests/integration/test_hermetic_lifecycle_foundation.py -v
```

These suites need no PAT and make no live HTTP calls: the packaged binary reaches
the dependency through a process-scoped `file://` URL rewrite. If one fails with a
network or authentication error, the rewrite did not apply -- confirm the test uses
the `hermetic_packaged_sample` fixture (which sets `GIT_CONFIG_COUNT` and
`GIT_ALLOW_PROTOCOL=file`) rather than invoking `apm` against the raw GitHub URL.

### Apm binary resolution

Tests that need to shell out to a real `apm` binary use the
`apm_binary_path` fixture and the `requires_apm_binary` marker. The
binary is resolved in this order, so a local build is preferred over a
system install:

1. `APM_BINARY_PATH` env var
2. `./dist/apm-<os>-<arch>/apm` (the layout produced by `scripts/build-binary.sh`)
3. `shutil.which("apm")`

`apm_engine_command` is the canonical fixture for narrowly scoped Python
filesystem-boundary fault injection: it returns `(sys.executable, "-m",
"apm_cli.cli")` so `ApmLifecycleRunner` executes the installed Python engine.
This is an explicit engine contract, not packaged-binary coverage, and it is
independent of `APM_BINARY_PATH` so frozen CI cannot bypass the instrumentation.
Packaged executable tests must continue to use `apm_binary_path`.

### Adding an integration test that needs a precondition

1. Apply the marker at module or test level:
   ```python
   import pytest
   pytestmark = pytest.mark.requires_github_token
   ```
2. If you need a brand-new precondition, add an entry to
   `_MARKER_CHECKS` in `tests/integration/conftest.py` (predicate +
   skip reason) and declare the marker in `pyproject.toml`. That is
   the only place the precondition needs to live.

### CI orchestrator: `scripts/test-integration.sh`

`scripts/test-integration.sh` is the thin orchestrator the CI integration job
invokes. Its responsibilities are: resolve GitHub / ADO tokens, detect platform,
locate or build the apm PyInstaller binary, install the requested runtimes,
install Python test dependencies, and run `pytest tests/integration/` once. All
per-test gating lives in the marker registry described above.

`APM_TEST_RUNTIMES` controls which external runtimes the orchestrator installs:

- unset locally: install `copilot codex llm`
- `copilot`: install only Copilot CLI when explicitly needed for local reproduction
- `none`: install no external runtime up front; native and merge-queue CI use this selection

Config-rendering cases do not require Copilot itself. Tests whose purpose is
runtime installation still execute their own setup in isolated environments.

Release promotion sets `PYTEST_MARK_EXPR` instead of editing test lists. Full
non-live qualification uses `not live`; the focused macOS Intel lifecycle lane
uses `lifecycle_smoke and not live`. Live ADO PAT coverage is owned by the
**Auth Acceptance Tests** workflow: dispatch it with `ado_pat_e2e: true` and an
`ado_repo` acceptance fixture. That workflow selects `live and requires_ado_pat`,
requires `AUTH_TEST_ADO_APM_PAT`, and fails with the normal actionable auth
diagnostic when the PAT is rejected.

Run the same focused acceptance locally with:

```bash
APM_TEST_ADO_REPO=dev.azure.com/org/project/_git/repo \
ADO_APM_PAT=... \
uv run pytest tests/integration/ -m "live and requires_ado_pat" -v
```

The orchestrator is mainly intended for reproducing the full CI
environment end-to-end; for local iteration prefer the direct
`pytest` invocations earlier on this page.

## CI/CD Integration

### Pull requests and merge queue

The required `gate` check is the single merge authority. It polls the exact
check names expected for the event SHA and accepts only `success`; skipped,
neutral, missing, duplicate, failed, cancelled, or timed-out checks fail closed.

At PR time the gate requires lint, test architecture ratchets, Linux unit
shards, Windows compatibility, APM self-check, NOTICE drift, and Lifecycle
Smoke. In merge queue context it also requires the Linux build, core smoke,
full integration, release validation, and the coverage combine gate.
The Windows compatibility selection uses two workers with `--dist loadgroup`.
Merge-queue smoke, integration shards, and isolated validation can start after
the build independently; all remain required at the final gate.

### Release qualification

`build-release.yml` uses `release-platform.yml` to:

1. Reuse a qualified candidate or build fresh.
2. For fresh full validation, call canonical source CI for lint, architecture
   ratchets, coverage, red-team tests, and lifecycle smoke.
3. Run independent native unit and build jobs for each platform.
4. Run integration, isolated archive validation, and Windows installer tests
   depending only on their platform's build artifact.
5. Promote only those exact, verified, unpacked, tested archives.

Unix integration uses `release-integration.yml`: one shard, four workers, and
`duration_based_chunks` assignment per native lane. Every shard must succeed.

For tag pushes, `rc.plan` in `scripts/release-candidate.cjs` treats only names
matching `/^v[0-9]+\.[0-9]+\.[0-9]+$/` as stable. All other `v*` tag pushes,
including PEP 440 `v1.2.3a1`, `v1.2.3b1`, and `v1.2.3rc1`, are classified as
prereleases: they do not build or publish stable docs or PyPI distributions.
Classification does not guarantee successful qualification.

Stable-tag read-only docs and wheel builds start after planning. Publishers
require successful GitHub Release creation and their own builds; prebuilding
neither grants publishing permissions nor bypasses qualification.

`scripts/package_release.py` packages each native archive once, records archive
and executable SHA-256 digests, verifies member safety and embedded version/build
SHA, extracts into a fresh destination, and rechecks the executable before upload.
Publication never repacks archives.
Candidate artifacts are attempt-scoped (`candidate-<run_attempt>-<binary_name>`;
evidence is `release-candidate-evidence-<run_attempt>`), so partial reruns must
not reuse old-attempt artifacts. Before publication, use **Re-run all jobs** to
regenerate complete qualification; afterward, follow [publication recovery](#publication-recovery).

### Candidate reuse on tags

A tag can reuse only successful full qualification from
`build-release.yml` on `main` at the exact tag commit SHA, triggered by
`schedule` or `repository_dispatch` with type `manual-build-release`.
Evidence must match immutable run and artifact identities and digests.
PR, `workflow_dispatch`, ordinary `main` push, and earlier tag-push runs are
not eligible cross-run sources. Fresh tag qualification is usable within its
own run. Absent or expired candidates trigger a fresh full build; API errors
or inconsistent evidence fail closed.

### Publication recovery

The human release operator authorizes recovery after publication starts.
Qualification reruns are not publisher retries:
`verified-release-assets-<run_attempt>`, `docs-pages-<run_attempt>`, and
`pypi-distributions-<run_attempt>` are attempt-scoped. **Re-run failed jobs**
increments the attempt without rerunning successful producers, so it cannot
recover their artifacts. **Re-run all jobs** also revisits successful publishers;
it is not a safe delivery-only retry.

1. Stop retries. Retain exact successful producer and qualification evidence:
   run IDs, attempts, SHA/version, artifact IDs/digests, and publisher logs.
2. For failed downloads, verify the expected producer attempt and artifact
   availability. For failed Pages deployment, record the existing deployment
   and source version. For partial PyPI uploads, inventory accepted and missing
   filenames/versions. In every case, verify existing public GitHub/PyPI asset
   hashes and versions against retained producer evidence before any retry.
3. Request release-operator authorization with that inventory.
   The workflow provides no publisher-only recovery using immutable producer
   identities; stop if safe continuation cannot be established.

### Platform matrix

Full release qualification runs on five native platforms for tags, schedules,
and `repository_dispatch` `manual-build-release` runs:

- Linux x86_64: `ubuntu-24.04`
- Linux arm64: `ubuntu-24.04-arm`
- Windows x86_64: `windows-latest`
- macOS Intel: `macos-15-intel`
- macOS Apple Silicon: `macos-latest`

Ordinary `main` pushes keep the existing four-platform policy and exclude macOS
Apple Silicon. macOS Intel runs the focused `lifecycle_smoke and not live`
integration selection; full non-live lanes use `not live`.

### Windows installer candidate testing

The Windows installer job consumes the candidate archive produced by the build
job. It does not depend on integration, release validation, or a pinned GitHub
Release destination. Tests may use a previous binary only as an upgrade source;
the destination under test is the freshly built candidate archive.

Coverage has two paths. The fresh-candidate path checks install, native launch,
junction cleanup, tamper rejection, and same-version reinstall; a canary proves
the old candidate release tree is replaced. The older-source path installs the
baseline once, then runs the real `apm self-update` lifecycle to the candidate
and performs the cross-version upgrade assertions there.

The wrapper requires six workflow-supplied inputs and has no published-release
fallback:

```text
APM_CANDIDATE_ARCHIVE
APM_CANDIDATE_VERSION
APM_CANDIDATE_SHA256
APM_BASELINE_ARCHIVE
APM_BASELINE_VERSION
APM_BASELINE_SHA256
```

Archive inputs are ZIP paths. SHA-256 inputs are hex digest strings, not
`.sha256` file paths. Version inputs accept either the packager metadata form
`X.Y.Z` or a historical `vX.Y.Z`; the wrapper normalizes to a leading `v`, and
the baseline version must be older than the candidate version. Missing or bad
candidate metadata fails the Windows E2E run; it does not skip or fall back to a
published release.

The job entrypoint is pytest, not the PowerShell helper. Set `APM_E2E_TESTS=1`
and the six variables above, then run:

```bash
uv run --frozen --extra dev pytest -q tests/integration/test_windows_installer_launchers.py
```

Python owns the verified local HTTP server and mandatory suite arguments. It
serves the verified archives and the current checkout's `install.ps1` bytes on
`127.0.0.1` through the production mirror variables (`APM_RELEASE_BASE_URL`,
`APM_INSTALLER_BASE_URL`, `APM_NO_DIRECT_FALLBACK=1`). Production `install.ps1`
is unchanged. The test also compares the installed candidate executable hash
with the executable member inside the candidate ZIP.

The Windows isolation root is `tmp_path_factory.mktemp("wi") / "i"`: a short,
nonexistent child for the shared helper, while the PowerShell install prefix
still includes spaces and `&`.

Production uses the short `RUNNER_TEMP/apm-windows-installer` base directory.
`scripts/windows/validate-installer-result.ps1` requires a JUnit report with
at least one concrete test case, no skips, and no failures or errors. Pytest
exit 0 alone is insufficient.

### Windows unit hang diagnostics

The Windows full-unit step in `.github/workflows/release-unit.yml` runs:

```sh
uv run --frozen pytest tests/unit tests/test_console.py -n auto --dist worksteal --durations=50 --store-durations --junitxml=test-results/unit.xml -vv --tb=short --show-capture=no --no-showlocals
```

`PYTHONUNBUFFERED=1` keeps named test starts and outcomes visible while the suite runs. Captured test output and local-variable dumps remain disabled. These diagnostics identify candidate unfinished tests, not stack frames or the exact blocked phase; an outcome can appear before fixture teardown completes.

The 60-minute step limit fails closed on hangs. Test selection and parallelism
are unchanged: Windows unit runs still use xdist `-n auto --dist worksteal`.
The PR-time `windows_compat` gate exercises name visibility during setup, call,
and teardown after a failed call.

### GitHub Actions Authentication

E2E tests require proper GitHub Models API access:

**Required Permissions:**
- `contents: read` - for repository access
- `models: read` - **Required for GitHub Models API access**

**Environment Variables:**
- `GITHUB_TOKEN` - user-scoped token for GitHub Models runtime calls
- `GITHUB_APM_PAT` - package access token; used as fallback by runtime setup

Runtime setup prefers `GITHUB_TOKEN` for GitHub Models and falls back to `GITHUB_APM_PAT` when no user-scoped token is present.

### Lifecycle Smoke verifies

- Install content-hash roundtrip (Consume contract)
- Virtual-skill lock convergence (Produce contract, adjacent to the #2226 ADO lock-coordinate fix)
- Policy pinned-constraint enforcement (Govern contract)
- The virtual/manifestless lifecycle matrix: install, lock, frozen-install, update, and audit stay consistent (the direct #2240 regression)
- The ADO lock-coordinate single-owner guard (the direct #2226 regression)
- Architecture registry coverage: every registered guard has exactly one mutation case. Only the cheap completeness assertion runs in PR smoke; the full mutation matrix remains in integration qualification.
- Prune's merged-hook and ownership-sidecar reconciliation for the `claude` target (the direct #2249 regression -- an orphaned package's merged hook entries and sidecar markers must be cleaned up, not left pointing at deleted scripts)
- No network, no credentials, no built binary required for any of the above

### Timing and performance capture

CI pytest lanes record `--durations=50`, JUnit XML, and `.test_durations`;
`test-results-*` artifacts contain the files that lane writes.

Do not restore mutable per-shard timing histories: different partition maps can
omit tests. Merge-queue integration freezes one immutable `.test_durations`
artifact in the existing build job, with no extra CI job; all four shards
download that identical snapshot. PR unit shards record timings and JUnit only.
They do not restore a rolling cache or present the shards as timing-balanced:
the shared planner job required for that can cost more runner allocation time
than it saves in balancing.

Native Unix builds also freeze any available timing history through
`.github/actions/pytest-timing`. All shards for that build consume the same
immutable snapshot. Measured shard histories are retained as artifacts, not independently
restored or written back by each shard. Hints never replace test execution or
act as an allowlist. Grouped tests still use `--dist loadgroup` so
HOME-mutating fixtures stay on one worker within each isolated job.

Inspect expensive fixtures by opening the slowest `--durations=50` entries and
correlating fixture-heavy node IDs with JUnit XML for the same OS, architecture,
suite, and shard.

A historical comparison at source commit `1687380d7` measured 34m41s -> 24m53s
(about 28% lower wall time) with 22.77% more unweighted runner time. The
experiments are retired; these results do not qualify the current code or
guarantee a speedup.

## Debugging Test Failures

### Smoke Test Failures
- Check the core smoke or runtime smoke step that failed.
- Verify the native binary path and platform match the selected lane.
- For runtime smoke, verify the relevant opt-in and runtime setup logs.

### E2E Test Failures  
- **Use the unified integration script first**: Run `./scripts/test-integration.sh` to reproduce the exact CI environment locally
- Verify `GITHUB_TOKEN` has required permissions (`models:read`)
- Ensure the required GitHub and ADO tokens for the selected markers are set
- Check GitHub Models API availability
- Review actual vs expected output
- Test locally with same environment

### Lifecycle Smoke Failures
- These tests are hermetic -- no credentials, no built binary, no network (a real socket attempt raises `OSError`, it does not hang or retry). A failure is a genuine regression, not an environment issue.
- Run the exact CI command from the "Run it locally" block under Tier 3 above to reproduce.
- If the failure is about the CI job's shape (marker not registered, wrong `-m`/`--strict-markers` invocation, unbounded root, timeout, empty marker family, or required-check wiring) rather than test logic, check `tests/quality/test_ci_topology.py` -- that guard pins the job's contract and its own failure message will point at what drifted.
- For hanging issues: Check command transformation in script runner (codex expects prompt content, not file paths)

## Adding New Tests

### For New Runtime Support:
1. Add a smoke test for runtime setup, marked
   `@pytest.mark.requires_runtime_<name>` (and add the marker entry to
   `_MARKER_CHECKS` in `tests/integration/conftest.py` if the runtime
   is brand new).
2. Add an E2E test for the golden scenario with the new runtime,
   marked `@pytest.mark.requires_e2e_mode` and any token markers it
   needs.
3. Update the CI matrix if the runtime introduces new platform
   support.

### For New Features:
1. Add a smoke test for compilation/validation.
2. Add an E2E test if the feature requires API calls -- pick the
   smallest set of markers that captures its real preconditions
   (`requires_github_token`, `requires_network_integration`, etc.)
   so contributors without those credentials still get a clean
   `SKIPPED` rather than a hard failure.
3. Keep tests focused and fast.
