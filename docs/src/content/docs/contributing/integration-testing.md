---
title: "Integration Testing"
sidebar:
  order: 3
---

This document describes APM's integration testing strategy to ensure runtime setup scripts work correctly and the golden scenario from the README functions as expected.

## Testing Strategy

APM uses a tiered approach to integration testing:

### 1. **Smoke Tests** (merge queue, runtime changes, and releases)
- **Location**: `tests/integration/test_runtime_smoke.py`
- **Purpose**: Fast verification that runtime setup scripts work
- **Scope**: 
  - Runtime installation (codex, llm)
  - Binary functionality (`--version`, `--help`)
  - APM runtime detection
  - Workflow compilation without execution
- **Duration**: ~2-3 minutes per platform
- **Trigger**: merge queue integration workflow, runtime-code pushes, scheduled/manual runs, and release validation

### 2. **End-to-End Golden Scenario Tests** (merge queue and promotion runs)
- **Location**: `tests/integration/test_golden_scenario_e2e.py`
- **Purpose**: Complete verification of the README golden scenario
- **Scope**:
  - Full runtime setup and configuration
  - Project initialization (`apm init`)
  - Dependency installation (`apm install`)
  - Real API calls to GitHub Models
  - Copilot, Codex, LLM, and Gemini runtime execution
- **Duration**: ~10-15 minutes per platform (with 20-minute timeout)  
- **Trigger**: merge queue integration workflow, plus tag, schedule, and manual promotion runs

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
| `live` | Tests that hit real GitHub repos via cloning; deselected by default | Override the deselect: `pytest -m live tests/integration -v` |

Without any of those env vars or runtimes a `pytest tests/integration`
invocation is silent rather than red: every test is collected and
reported as `SKIPPED` with a one-line reason, so you can see exactly
what is missing and why.

### Common invocations

```bash
# Run everything you currently have the prerequisites for
uv run pytest tests/integration -v

# Run a single suite (the marker registry still applies)
uv run pytest tests/integration/test_golden_scenario_e2e.py -v

# Run only a marker family
uv run pytest tests/integration -m requires_github_token -v
```

### Hermetic lifecycle fixtures

`tests/integration/test_hermetic_lifecycle_foundation.py` is the cross-module
contract. Complete the [development setup](../development-guide/) first, then
directly import the flat utility that owns each concern:

| Utility | Contract test |
| --- | --- |
| `tests/utils/isolated_apm_environment.py` | `test_isolated_apm_environment_contract.py` |
| `tests/utils/local_git_repository.py` | `test_local_git_repository_factory_contract.py` |
| `tests/utils/local_package.py` | `test_local_package_factory_contract.py` |
| `tests/utils/apm_lifecycle_runner.py` | `test_apm_lifecycle_runner_contract.py` |
| `tests/utils/artifact_snapshot.py` | `test_artifact_snapshot_contract.py` |
| `tests/utils/scenario_rows.py` | `test_scenario_rows_contract.py` |

Source fixtures author only source inputs; the real APM CLI creates lockfiles,
deployed trees, compiled output, bundles, hashes, cache state, and audit
reports.

`IsolatedApmEnvironment` sanitizes child environments and installs a
best-effort Python socket tripwire. It is not an OS/native-code sandbox:
executables found through `PATH` remain trusted, reflective access to CPython
internals or native extensions can bypass Python monkey-patches, `file://`
access is not confined by the OS, and hostile post-creation filesystem races
are outside the contract. `GIT_ALLOW_PROTOCOL=file` and local
`url.*.insteadOf` rewriting separately restrict Git transport in reviewed
scenarios.

Keep modules flat: no facade, base class, registry, or shared DSL. Command
failures retain command, return code, stdout, and stderr evidence. Hermetic
tests require no network or token marker.

Inside a pytest test that accepts `tmp_path`, compose the utilities explicitly:

```python
import os
import subprocess

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory
from tests.utils.scenario_rows import LifecycleAction, ScenarioRow

isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
environment = isolated.subprocess_env()
sources = LocalPackageFactory(isolated.package_root)
dependency = sources.create("fixture")
sources.add_skill(
    dependency,
    "example",
    "---\nname: example\ndescription: Fixture\n---\n# Example\n",
)
repositories = LocalGitRepositoryFactory(
    isolated.repository_root,
    env=environment,
)
repository = repositories.create("fixture", source_tree=dependency.root)
commit = repositories.commit(repository, message="seed")
git_source = "git@gitlab.example.invalid:group/fixture.git"
subprocess.run(
    ("git", "config", "--global", f"url.{repository.file_url}.insteadOf", git_source),
    env=environment,
    check=True,
    timeout=30,
)
project = LocalPackageFactory(isolated.work_root).create(
    "consumer",
    dependencies=({"git": git_source, "type": "gitlab", "ref": commit.sha},),
    targets=("copilot",),
)
row = ScenarioRow(
    id="example",
    source_inputs=(dependency.root, project.manifest_path),
    lifecycle_actions=(LifecycleAction(("install", "--target", "copilot")),),
)
results = ApmLifecycleRunner().run_sequence(
    tuple(action.args for action in row.lifecycle_actions),
    expected_returncodes=tuple(action.expected_returncode for action in row.lifecycle_actions),
    scenario_id=row.id,
    cwd=project.root,
    env=environment,
)
snapshot = ArtifactSnapshot.capture(project.root)
assert results[0].returncode == 0
assert "apm.lock.yaml" in snapshot.paths
```

`ApmLifecycleRunner()` invokes `apm` through `PATH`; under
`uv run --extra dev pytest`, this is the development environment's console
script. Pass `(sys.executable, "-m", "apm_cli.cli")` to run the source module.
To test a packaged standalone binary, build it or set `APM_BINARY_PATH`, then
pass `(str(apm_binary_path),)`.

Run the changed utility contract first, then the cross-module contract serially
and with xdist:

```bash
# One utility contract
uv run pytest tests/integration/test_local_package_factory_contract.py -v

# Serial
uv run pytest tests/integration/test_hermetic_lifecycle_foundation.py -v

# xdist
uv run pytest -n auto tests/integration/test_hermetic_lifecycle_foundation.py -v
```

### Apm binary resolution

Tests that need to shell out to a real `apm` binary use the
`apm_binary_path` fixture and the `requires_apm_binary` marker. The
binary is resolved in this order, so a local build is preferred over a
system install:

1. `APM_BINARY_PATH` env var
2. `./dist/apm-<os>-<arch>/apm` (the layout produced by `scripts/build-binary.sh`)
3. `shutil.which("apm")`

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

`scripts/test-integration.sh` is the thin orchestrator the CI
integration job invokes. Its sole responsibilities are: resolve
GitHub / ADO tokens, detect platform, locate or build the apm
PyInstaller binary, install runtimes (codex / copilot / llm),
install python test dependencies, and run
`pytest tests/integration/` once. All per-test gating lives in the
marker registry described above. New integration tests dropped into
`tests/integration/` are picked up automatically; add the right
`requires_*` marker and the registry will skip the test when its
precondition is missing.

The orchestrator is mainly intended for reproducing the full CI
environment end-to-end; for local iteration prefer the direct
`pytest` invocations earlier on this page.

## CI/CD Integration

### GitHub Actions Workflow

**On PR and merge queue:**
1. PR-time unit checks run first; merge queue adds Linux smoke, integration, and release-validation gates.

**On version tag releases:**
1. Unit tests + Smoke tests
2. Build binaries (cross-platform)
3. **E2E golden scenario tests** (using built binaries)
4. Create GitHub Release
5. Publish to PyPI 

**Manual workflow dispatch:**
- Test builds (uploads as workflow artifacts)
- Allows testing the full build pipeline without creating a release
- Useful for validating changes before tagging

### GitHub Actions Authentication

E2E tests require proper GitHub Models API access:

**Required Permissions:**
- `contents: read` - for repository access
- `models: read` - **Required for GitHub Models API access**

**Environment Variables:**
- `GITHUB_TOKEN` - user-scoped token for GitHub Models runtime calls
- `GITHUB_APM_PAT` - package access token; used as fallback by runtime setup

Runtime setup prefers `GITHUB_TOKEN` for GitHub Models and falls back to `GITHUB_APM_PAT` when no user-scoped token is present.

### Release Pipeline Sequencing

The workflow ensures quality gates at each step:

1. **build-and-test** jobs - Unit tests plus binary builds
2. **integration-tests** job - Comprehensive runtime scenarios
3. **release-validation** job - Final shipped-binary validation
4. **create-release** job - GitHub release creation
5. **publish-pypi** job - PyPI package publication

Each stage must succeed before proceeding to the next, ensuring only fully validated releases reach users.

The [`microsoft/homebrew-apm`](https://github.com/microsoft/homebrew-apm) tap updates independently: it polls the latest APM release and commits formula updates with its own repository-scoped `GITHUB_TOKEN`. The release pipeline does not hold a cross-repository Homebrew credential.

### Test Matrix

Promotion integration tests run on:
- **Linux**: ubuntu-24.04 (x86_64), ubuntu-24.04-arm (arm64)
- **Windows**: windows-latest (x86_64)
- **macOS Intel**: macos-15-intel (x86_64)
- **macOS Apple Silicon**: macos-latest (arm64)

**Python Version**: 3.12 (standardized across all environments)
**Package Manager**: uv (for fast dependency management and virtual environments)

## What the Tests Verify

### Smoke Tests Verify:
- ✅ Runtime setup scripts execute successfully
- ✅ Binaries are downloaded and installed correctly
- ✅ Binaries respond to basic commands
- ✅ APM can detect installed runtimes
- ✅ Configuration files are created properly
- ✅ Workflow compilation works (without execution)

### E2E Tests Verify:
- ✅ Complete golden scenario from README works
- ✅ `apm runtime setup copilot` installs and configures GitHub Copilot CLI
- ✅ `apm runtime setup codex` installs and configures Codex
- ✅ `apm runtime setup llm` installs and configures LLM
- ✅ `apm init my-hello-world` creates project correctly
- ✅ `apm install` handles dependencies
- ✅ `apm run start --param name="Tester"` executes successfully
- ✅ Real API calls to GitHub Models work
- ✅ Parameter substitution works correctly
- ✅ MCP integration functions (GitHub tools)
- ✅ Binary artifacts work across platforms
- ✅ Release pipeline integrity (GitHub Release → PyPI)

## Benefits

### **Speed vs Confidence Balance**
- **Smoke tests**: Fast feedback (2-3 min) on every change
- **E2E tests**: High confidence (15 min) only when shipping

### **Cost Efficiency**
- Smoke tests use no API credits
- E2E tests only run on releases (minimizing API usage)
- Manual workflow dispatch for test builds without publishing

### **Platform Coverage**
- Tests run on all supported platforms
- Catches platform-specific runtime issues

### **Release Confidence**
- E2E tests must pass before any publishing steps
- Multi-stage release pipeline ensures quality gates
- Guarantees shipped releases work end-to-end
- Users can trust the README golden scenario
- Cross-platform binary verification

## Debugging Test Failures

### Smoke Test Failures
- Check runtime setup script output
- Verify platform compatibility
- Check network connectivity for downloads

### E2E Test Failures  
- **Use the unified integration script first**: Run `./scripts/test-integration.sh` to reproduce the exact CI environment locally
- Verify `GITHUB_TOKEN` has required permissions (`models:read`)
- Ensure both `GITHUB_TOKEN` and `GITHUB_MODELS_KEY` environment variables are set
- Check GitHub Models API availability
- Review actual vs expected output
- Test locally with same environment
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
