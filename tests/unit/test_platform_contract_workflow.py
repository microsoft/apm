"""Semantic contracts for hosted PR6 platform evidence."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import (
    assert_exact_command,
    assert_unconditional,
    effective_env,
    load_workflow,
    shell_commands,
    shell_tokens,
    workflow_job,
    workflow_step,
    workflow_step_index,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-release.yml"
WINDOWS_RELEASE_VALIDATION = ROOT / "scripts" / "windows" / "test-release-validation.ps1"
MACOS_VERSION_TEST_ID = (
    "tests/integration/test_core_smoke.py::TestBinaryStartup::test_apm_version_runs"
)
MACOS_RICH_TABLE_TEST_ID = (
    "tests/integration/test_core_smoke.py::TestBinaryStartup::test_apm_rich_table_runs"
)
WINDOWS_TEST_ID = "tests/integration/test_windows_installer_launchers.py"
MACOS_STARTUP_CONTRACTS = (
    (
        "build-and-validate-macos-intel",
        "macos-15-intel",
        "${{ github.workspace }}/dist/apm-darwin-x86_64/apm",
        None,
    ),
    (
        "build-and-validate-macos-arm",
        "macos-latest",
        "${{ github.workspace }}/dist/apm-darwin-arm64/apm",
        "github.ref_type == 'tag' || github.event_name == 'schedule' || "
        "github.event_name == 'repository_dispatch'",
    ),
)
NON_LIVE_UNIX_INTEGRATION_STEPS = (
    ("integration-tests", "Run integration tests (Unix)"),
    ("build-and-validate-macos-arm", "Run integration tests"),
)
NON_LIVE_UNIX_TIMEOUT_MINUTES = {
    ("integration-tests", "Run integration tests (Unix)"): 60,
    ("build-and-validate-macos-arm", "Run integration tests"): 60,
}
NON_LIVE_UNIX_PYTEST_ARGS = "-n 4 --dist loadgroup"
NON_LIVE_MARK_EXPRESSION = "not live"
INTEL_FOCUSED_INTEGRATION_STEP = "Run focused Intel integration tests"
INTEL_FOCUSED_MARK_EXPRESSION = "lifecycle_smoke and not live"
RUNTIME_SETUP_STEPS = (
    ("build-and-test", "Run smoke tests"),
    ("build-and-validate-macos-intel", INTEL_FOCUSED_INTEGRATION_STEP),
    ("build-and-validate-macos-intel", "Run release validation tests"),
    ("build-and-validate-macos-arm", "Run integration tests"),
    ("build-and-validate-macos-arm", "Run release validation tests"),
    ("integration-tests", "Run integration tests (Unix)"),
    ("integration-tests", "Run integration tests (Windows)"),
    ("release-validation", "Run release validation tests (Unix)"),
    ("release-validation", "Run release validation tests (Windows)"),
)


def _workflow() -> dict:
    return load_workflow(WORKFLOW)


def _assert_macos_startup_steps(workflow: dict) -> None:
    for job_id, runner, binary_path, job_condition in MACOS_STARTUP_CONTRACTS:
        job = workflow_job(workflow, job_id)
        step = workflow_step(job, "Test macOS non-shell binary startup")
        if job_condition is None:
            assert_unconditional(job, label=f"{job_id} job")
        else:
            assert job.get("if") == job_condition
        assert_unconditional(step, label=f"{job_id} startup step")
        assert job["runs-on"] == runner
        assert effective_env(workflow, job, step).get("GITHUB_TOKEN") is None
        assert step["env"] == {
            "APM_E2E_TESTS": "1",
            "APM_BINARY_PATH": binary_path,
        }
        tokens = shell_tokens(step)
        assert tokens[:3] == ["test", "-x", "$APM_BINARY_PATH"]
        assert_exact_command(
            shell_commands(step),
            [
                "uv",
                "run",
                "--frozen",
                "pytest",
                MACOS_VERSION_TEST_ID,
                MACOS_RICH_TABLE_TEST_ID,
                "-vv",
                "-ra",
                "--tb=short",
            ],
            label=f"{job_id} startup step",
        )
        assert workflow_step_index(job, "Build binary") < workflow_step_index(
            job,
            "Test macOS non-shell binary startup",
        )
        assert workflow_step_index(
            job,
            "Test macOS non-shell binary startup",
        ) < workflow_step_index(job, "Upload binary as workflow artifact")


def _assert_windows_installer_step(workflow: dict) -> None:
    job = workflow_job(workflow, "build-and-test")
    step = workflow_step(job, "Test install.ps1 end-to-end (Windows)")
    assert step.get("if") == "matrix.platform == 'windows'"
    assert effective_env(workflow, job, step).get("GITHUB_TOKEN") is None
    assert step["env"] == {"APM_E2E_TESTS": "1"}
    tokens = shell_tokens(step)
    assert tokens[:4] == ["uv", "run", "--frozen", "pytest"]
    assert WINDOWS_TEST_ID in tokens
    assert "-vv" in tokens
    assert "-ra" in tokens
    assert "--tb=short" in tokens


def _assert_standalone_integration_timeouts(workflow: dict) -> None:
    job = workflow_job(workflow, "integration-tests")
    unix_step = workflow_step(job, "Run integration tests (Unix)")
    windows_step = workflow_step(job, "Run integration tests (Windows)")
    assert unix_step.get("timeout-minutes") == 60
    assert windows_step.get("timeout-minutes") == 20


def _assert_non_live_unix_integration_parallelism(workflow: dict) -> None:
    for job_id, step_name in NON_LIVE_UNIX_INTEGRATION_STEPS:
        step = workflow_step(workflow_job(workflow, job_id), step_name)
        assert step["env"].get("PYTEST_MARK_EXPR") == NON_LIVE_MARK_EXPRESSION
        assert step["env"].get("PYTEST_EXTRA_ARGS") == NON_LIVE_UNIX_PYTEST_ARGS
        assert step.get("timeout-minutes") == NON_LIVE_UNIX_TIMEOUT_MINUTES[(job_id, step_name)]


def _assert_intel_focused_integration(workflow: dict) -> None:
    job = workflow_job(workflow, "build-and-validate-macos-intel")
    step = workflow_step(job, INTEL_FOCUSED_INTEGRATION_STEP)
    assert step.get("if") == (
        "github.ref_type == 'tag' || github.event_name == 'schedule' || "
        "github.event_name == 'repository_dispatch'"
    )
    assert step["env"].get("PYTEST_MARK_EXPR") == INTEL_FOCUSED_MARK_EXPRESSION
    assert step["env"].get("PYTEST_EXTRA_ARGS") == NON_LIVE_UNIX_PYTEST_ARGS
    assert step.get("timeout-minutes") == 30
    assert_exact_command(
        shell_commands(step),
        ["uv", "run", "./scripts/test-integration.sh"],
        label="macOS Intel focused integration step",
    )


def test_macos_jobs_run_non_shell_binary_startup_after_build() -> None:
    """Both macOS jobs execute the exact generated artifact before upload."""
    _assert_macos_startup_steps(_workflow())


def test_windows_installer_contract_is_windows_only_and_tokenless() -> None:
    """The Windows E2E has exact gating and no effective repository token."""
    _assert_windows_installer_step(_workflow())


def test_standalone_integration_timeouts_are_platform_specific() -> None:
    """Standalone Unix has headroom while Windows retains its proven bound."""
    _assert_standalone_integration_timeouts(_workflow())


def test_linux_and_arm_retain_non_live_corpus_grouped_parallelism() -> None:
    """Linux and macOS ARM keep the bounded non-live integration corpus."""
    _assert_non_live_unix_integration_parallelism(_workflow())


def test_intel_integration_is_marker_scoped_and_bounded() -> None:
    """Intel keeps focused native coverage without replaying the full corpus."""
    _assert_intel_focused_integration(_workflow())


@pytest.mark.parametrize(("job_id", "step_name"), RUNTIME_SETUP_STEPS)
def test_runtime_setup_uses_builtin_github_api_token(
    job_id: str,
    step_name: str,
) -> None:
    """Public runtime metadata must not use the private-module PAT."""
    workflow = _workflow()
    step = workflow_step(workflow_job(workflow, job_id), step_name)
    assert step["env"]["GITHUB_API_TOKEN"] == "${{ github.token }}"
    assert step["env"]["GITHUB_APM_PAT"] == "${{ secrets.GH_CLI_PAT }}"


def test_release_validation_keeps_live_inference_decoupled() -> None:
    """Release gates install runtimes but do not invoke paid live inference."""
    workflow = _workflow()
    job = workflow_job(workflow, "release-validation")
    for step_name in (
        "Run release validation tests (Unix)",
        "Run release validation tests (Windows)",
    ):
        env = effective_env(workflow, job, workflow_step(job, step_name))
        assert "APM_RUN_INFERENCE_TESTS" not in env
        assert "GITHUB_TOKEN" not in env

    script = WINDOWS_RELEASE_VALIDATION.read_text(encoding="utf-8")
    assert script.count('$env:APM_RUN_INFERENCE_TESTS -eq "1"') >= 2
    assert '$env:APM_RUN_INFERENCE_TESTS -ne "1"' in script
    assert "$testsTotal = 4" in script
    assert "$env:GITHUB_APM_PAT -or $env:GITHUB_TOKEN" in script


@pytest.mark.parametrize(
    ("job_id", "step_name"),
    NON_LIVE_UNIX_INTEGRATION_STEPS,
)
def test_non_live_unix_serial_integration_mutation_is_rejected(
    job_id: str,
    step_name: str,
) -> None:
    """No non-live release node can silently regress to the serial path."""
    workflow = deepcopy(_workflow())
    step = workflow_step(workflow_job(workflow, job_id), step_name)
    del step["env"]["PYTEST_EXTRA_ARGS"]

    with pytest.raises(AssertionError):
        _assert_non_live_unix_integration_parallelism(workflow)


@pytest.mark.parametrize(
    ("job_id", "step_name"),
    NON_LIVE_UNIX_INTEGRATION_STEPS,
)
def test_non_live_unix_timeout_mutation_is_rejected(
    job_id: str,
    step_name: str,
) -> None:
    """Every non-live Unix release node retains its measured timeout bound."""
    workflow = deepcopy(_workflow())
    step = workflow_step(workflow_job(workflow, job_id), step_name)
    step["timeout-minutes"] = NON_LIVE_UNIX_TIMEOUT_MINUTES[(job_id, step_name)] + 1

    with pytest.raises(AssertionError):
        _assert_non_live_unix_integration_parallelism(workflow)


def test_intel_non_live_corpus_mutation_is_rejected() -> None:
    """Intel cannot silently regain the redundant non-live corpus."""
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, "build-and-validate-macos-intel"),
        INTEL_FOCUSED_INTEGRATION_STEP,
    )
    step["env"]["PYTEST_MARK_EXPR"] = NON_LIVE_MARK_EXPRESSION

    with pytest.raises(AssertionError):
        _assert_intel_focused_integration(workflow)


def test_arm_focused_subset_mutation_is_rejected() -> None:
    """ARM remains the broad non-live macOS release authority."""
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, "build-and-validate-macos-arm"),
        "Run integration tests",
    )
    step["env"]["PYTEST_MARK_EXPR"] = INTEL_FOCUSED_MARK_EXPRESSION

    with pytest.raises(AssertionError):
        _assert_non_live_unix_integration_parallelism(workflow)


def test_unix_integration_twenty_minute_timeout_mutation_is_rejected() -> None:
    """The prior Unix timeout cannot satisfy the packaged timing contract."""
    workflow = deepcopy(_workflow())
    unix_step = workflow_step(
        workflow_job(workflow, "integration-tests"),
        "Run integration tests (Unix)",
    )
    unix_step["timeout-minutes"] = 20

    with pytest.raises(AssertionError):
        _assert_standalone_integration_timeouts(workflow)


def test_missing_unix_integration_timeout_mutation_is_rejected() -> None:
    """Removing the Unix timeout cannot silently make the step unbounded."""
    workflow = deepcopy(_workflow())
    unix_step = workflow_step(
        workflow_job(workflow, "integration-tests"),
        "Run integration tests (Unix)",
    )
    del unix_step["timeout-minutes"]

    with pytest.raises(AssertionError):
        _assert_standalone_integration_timeouts(workflow)


@pytest.mark.parametrize("scope", ("workflow", "job", "step"))
def test_windows_token_scope_mutations_are_rejected(scope: str) -> None:
    """A token inherited from any Actions scope must fail the contract."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, "build-and-test")
    step = workflow_step(job, "Test install.ps1 end-to-end (Windows)")
    {"workflow": workflow, "job": job, "step": step}[scope].setdefault("env", {})[
        "GITHUB_TOKEN"
    ] = "secret"

    with pytest.raises(AssertionError):
        _assert_windows_installer_step(workflow)


@pytest.mark.parametrize("job_id", [contract[0] for contract in MACOS_STARTUP_CONTRACTS])
@pytest.mark.parametrize("scope", ("workflow", "job", "step"))
def test_macos_token_scope_mutations_are_rejected(job_id: str, scope: str) -> None:
    """A token inherited from any Actions scope must fail the macOS contract."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, job_id)
    step = workflow_step(job, "Test macOS non-shell binary startup")
    {"workflow": workflow, "job": job, "step": step}[scope].setdefault("env", {})[
        "GITHUB_TOKEN"
    ] = "secret"

    with pytest.raises(AssertionError):
        _assert_macos_startup_steps(workflow)


def test_windows_linux_gate_mutation_is_rejected() -> None:
    """A Linux condition cannot satisfy the Windows-only platform contract."""
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, "build-and-test"),
        "Test install.ps1 end-to-end (Windows)",
    )
    step["if"] = "matrix.platform == 'linux'"

    with pytest.raises(AssertionError):
        _assert_windows_installer_step(workflow)


@pytest.mark.parametrize("job_id", [contract[0] for contract in MACOS_STARTUP_CONTRACTS])
@pytest.mark.parametrize("scope", ("job", "step"))
def test_macos_disabled_mutations_are_rejected(job_id: str, scope: str) -> None:
    """The macOS startup evidence cannot be disabled at job or step scope."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, job_id)
    step = workflow_step(job, "Test macOS non-shell binary startup")
    {"job": job, "step": step}[scope]["if"] = False

    with pytest.raises(AssertionError):
        _assert_macos_startup_steps(workflow)


@pytest.mark.parametrize("job_id", [contract[0] for contract in MACOS_STARTUP_CONTRACTS])
def test_macos_echo_replacement_mutation_is_rejected(job_id: str) -> None:
    """An echo cannot replace the exact frozen pytest invocation."""
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, job_id),
        "Test macOS non-shell binary startup",
    )
    step["run"] = (
        'test -x "$APM_BINARY_PATH"\n'
        "echo uv run --frozen pytest "
        f"{MACOS_VERSION_TEST_ID} {MACOS_RICH_TABLE_TEST_ID} "
        "-vv -ra --tb=short\n"
    )

    with pytest.raises(AssertionError):
        _assert_macos_startup_steps(workflow)
