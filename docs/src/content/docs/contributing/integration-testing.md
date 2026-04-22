---
title: "Integration Testing"
sidebar:
  order: 3
---

This document describes APM's integration testing strategy to ensure runtime setup scripts work correctly and the golden scenario from the README functions as expected.

## Testing Strategy

APM uses a tiered approach to integration testing:

Some command families also keep their own suite-specific maintainer
notes next to the tests. For example, the marketplace CLI integration
suite is documented in `tests/integration/marketplace/README.md`, which
also points back to the package-based command layout in
`src/apm_cli/commands/marketplace/`.

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

### Smoke Tests
```bash
# Run all smoke tests
pytest tests/integration/test_runtime_smoke.py -v

# Run specific test
pytest tests/integration/test_runtime_smoke.py::TestRuntimeSmoke::test_codex_runtime_setup -v
```

### E2E Tests

#### Option 1: Complete CI Process Simulation (Recommended)
```bash
export GITHUB_TOKEN=your_token_here
./scripts/test-integration.sh
```

This script (`scripts/test-integration.sh`) is a unified script that automatically adapts to your environment:

**Local mode** (no existing binary):
1. **Builds binary** with PyInstaller (like CI build job)
2. **Sets up symlink and PATH** (like CI artifacts download)
3. **Installs runtimes** (codex/llm setup)
4. **Installs test dependencies** (like CI test setup)
5. **Runs integration tests** with the built binary (like CI integration-tests job)

**CI mode** (binary exists in `./dist/`):
1. **Uses existing binary** from CI build artifacts
2. **Sets up symlink and PATH** (standard CI process)
3. **Installs runtimes** (codex/llm setup)
4. **Installs test dependencies** (like CI test setup)  
5. **Runs E2E tests** with pre-built binary

#### Option 2: Direct pytest execution
```bash
# Set up environment
export APM_E2E_TESTS=1
export GITHUB_TOKEN=your_github_token_here
export GITHUB_MODELS_KEY=your_github_token_here  # LLM runtime expects this specific env var

# Run E2E tests
pytest tests/integration/test_golden_scenario_e2e.py -v -s

# Run specific E2E test
pytest tests/integration/test_golden_scenario_e2e.py::TestGoldenScenarioE2E::test_complete_golden_scenario_codex -v -s
```

**Note**: Both `GITHUB_TOKEN` and `GITHUB_MODELS_KEY` should contain the same GitHub token value, but different runtimes expect different environment variable names.

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
1. Add smoke test for runtime setup
2. Add E2E test for golden scenario with new runtime
3. Update CI matrix if new platform support

### For New Features:
1. Add smoke test for compilation/validation
2. Add E2E test if feature requires API calls
3. Keep tests focused and fast

---

This testing strategy ensures we ship with confidence while maintaining fast development cycles.
