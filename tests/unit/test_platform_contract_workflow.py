"""Static contracts for hosted PR6 platform evidence."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-release.yml"


def test_macos_intel_runs_non_shell_binary_startup_after_build() -> None:
    """The Intel job must execute the existing list-form binary startup test."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    intel_start = workflow.index("  build-and-validate-macos-intel:")
    arm_start = workflow.index("  build-and-validate-macos-arm:")
    intel_job = workflow[intel_start:arm_start]

    build_step = "      - name: Build binary"
    test_id = "tests/integration/test_core_smoke.py::TestBinaryStartup::test_apm_version_runs"
    binary_path = "APM_BINARY_PATH: ${{ github.workspace }}/dist/apm-darwin-x86_64/apm"

    assert test_id in intel_job
    assert binary_path in intel_job
    assert 'APM_E2E_TESTS: "1"' in intel_job
    assert intel_job.index(build_step) < intel_job.index(test_id)
    assert "matrix:" not in intel_job


def test_windows_installer_contract_reports_its_exact_test_id() -> None:
    """The hosted Windows log must distinguish a pass from a skip."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    windows_start = workflow.index("  build-and-test:")
    intel_start = workflow.index("  build-and-validate-macos-intel:")
    build_job = workflow[windows_start:intel_start]

    test_id = "tests/integration/test_windows_installer_launchers.py"
    assert test_id in build_job
    command_start = build_job.index(test_id)
    command = build_job[command_start : command_start + 160]
    assert "-vv -ra --tb=short" in command
