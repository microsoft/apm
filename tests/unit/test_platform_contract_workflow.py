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
        "github.event_name == 'workflow_dispatch'",
    ),
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


def test_macos_jobs_run_non_shell_binary_startup_after_build() -> None:
    """Both macOS jobs execute the exact generated artifact before upload."""
    _assert_macos_startup_steps(_workflow())


def test_windows_installer_contract_is_windows_only_and_tokenless() -> None:
    """The Windows E2E has exact gating and no effective repository token."""
    _assert_windows_installer_step(_workflow())


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
