---
title: "Integration Testing"
sidebar:
  order: 3
---

This document describes APM's integration testing strategy to ensure runtime setup scripts work correctly and the golden scenario from the README functions as expected.

## Testing Strategy

APM uses a tiered approach to integration testing:

### 1. **Smoke Tests** (Every CI run)
- **Location**: `tests/integration/test_runtime_smoke.py`
- **Purpose**: Fast verification that runtime setup scripts work
- **Scope**: 
  - Runtime installation (codex, llm)
  - Binary functionality (`--version`, `--help`)
  - APM runtime detection
  - Workflow compilation without execution
- **Duration**: ~2-3 minutes per platform
- **Trigger**: Every push/PR

### 2. **End-to-End Golden Scenario Tests** (Releases only)
- **Location**: `tests/integration/test_golden_scenario_e2e.py`
- **Purpose**: Complete verification of the README golden scenario
- **Scope**:
  - Full runtime setup and configuration
  - Project initialization (`apm init`)
  - Dependency installation (`apm install`)
  - Real API calls to GitHub Models
  - Both Codex and LLM runtime execution
- **Duration**: ~10-15 minutes per platform (with 20-minute timeout)  
- **Trigger**: Only on version tags (releases)

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
| `requires_inference` | Opt-in for tests that call inference APIs | `export APM_RUN_INFERENCE_TESTS=1` |
| `requires_github_token` | A token usable against `github.com` / GitHub Models | `export GITHUB_APM_PAT=...` (or `GITHUB_TOKEN`) |
| `requires_ado_pat` | Azure DevOps PAT for ADO host tests | `export ADO_APM_PAT=...` |
| `requires_ado_bearer` | Azure CLI signed in + opt-in flag | `az login` and `export APM_TEST_ADO_BEARER=1` |
| `requires_apm_binary` | A built `apm` binary on disk or `PATH` | `scripts/build-binary.sh` (or set `APM_BINARY_PATH`) |
| `requires_runtime_codex` | The `codex` runtime installed under `~/.apm/runtimes/` | `apm runtime setup codex` |
| `requires_runtime_copilot` | The GitHub Copilot CLI runtime installed under `~/.apm/runtimes/` | `apm runtime setup copilot` |
| `requires_runtime_llm` | The `llm` runtime installed under `~/.apm/runtimes/` | `apm runtime setup llm` |

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

### Legacy: `scripts/test-integration.sh`

`scripts/test-integration.sh` is the legacy wrapper that built a
binary, set up runtimes, and shelled out to pytest. It is being
retired (see `microsoft/apm#1166`); prefer the direct `pytest`
invocations above. The script is still wired into CI for the moment
and continues to work, but new test plumbing belongs in the marker
registry, not in the bash script.

## CI/CD Integration

### GitHub Actions Workflow

**On every push/PR:**
1. Unit tests + **Smoke tests** (runtime installation verification)

**On version tag releases:**
1. Unit tests + Smoke tests
2. Build binaries (cross-platform)
3. **E2E golden scenario tests** (using built binaries)
4. Create GitHub Release
5. Publish to PyPI 
6. Update Homebrew Formula

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
- `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` - for Codex runtime
- `GITHUB_MODELS_KEY: ${{ secrets.GITHUB_TOKEN }}` - for LLM runtime (expects different env var name)

Both runtimes authenticate against GitHub Models but expect different environment variable names.

### Release Pipeline Sequencing

The workflow ensures quality gates at each step:

1. **test** job - Unit tests + smoke tests (all platforms)
2. **build** job - Binary compilation (depends on test success)
3. **integration-tests** job - Comprehensive runtime scenarios (depends on build success)
4. **create-release** job - GitHub release creation (depends on integration-tests success)
5. **publish-pypi** job - PyPI package publication (depends on release creation)
6. **update-homebrew** job - Homebrew formula update (depends on PyPI publication)

Each stage must succeed before proceeding to the next, ensuring only fully validated releases reach users.

### Test Matrix

All integration tests run on:
- **Linux**: ubuntu-24.04 (x86_64)
- **macOS Intel**: macos-13 (x86_64) 
- **macOS Apple Silicon**: macos-14 (arm64)

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
- ✅ Release pipeline integrity (GitHub Release → PyPI → Homebrew)

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
- Automatic Homebrew formula updates

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

---

This testing strategy ensures we ship with confidence while maintaining fast development cycles.
