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
MACOS_TEST_ID = "tests/integration/test_core_smoke.py::TestBinaryStartup::test_apm_version_runs"
WINDOWS_TEST_ID = "tests/integration/test_windows_installer_launchers.py"


def _workflow() -> dict:
    return load_workflow(WORKFLOW)


def _assert_macos_startup_step(workflow: dict) -> None:
    job = workflow_job(workflow, "build-and-validate-macos-intel")
    step = workflow_step(job, "Test macOS non-shell binary startup")
    assert_unconditional(job, label="macOS Intel job")
    assert_unconditional(step, label="macOS Intel startup step")
    assert job["runs-on"] == "macos-15-intel"
    assert step["env"] == {
        "APM_E2E_TESTS": "1",
        "APM_BINARY_PATH": "${{ github.workspace }}/dist/apm-darwin-x86_64/apm",
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
            MACOS_TEST_ID,
            "-vv",
            "-ra",
            "--tb=short",
        ],
        label="macOS Intel startup step",
    )
    assert workflow_step_index(job, "Build binary") < workflow_step_index(
        job,
        "Test macOS non-shell binary startup",
    )


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


def test_macos_intel_runs_non_shell_binary_startup_after_build() -> None:
    """The Intel job executes the exact generated artifact unconditionally."""
    _assert_macos_startup_step(_workflow())


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


@pytest.mark.parametrize("scope", ("job", "step"))
def test_macos_disabled_mutations_are_rejected(scope: str) -> None:
    """The Intel startup evidence cannot be disabled at job or step scope."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, "build-and-validate-macos-intel")
    step = workflow_step(job, "Test macOS non-shell binary startup")
    {"job": job, "step": step}[scope]["if"] = False

    with pytest.raises(AssertionError):
        _assert_macos_startup_step(workflow)


def test_macos_echo_replacement_mutation_is_rejected() -> None:
    """An echo cannot replace the exact frozen pytest invocation."""
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, "build-and-validate-macos-intel"),
        "Test macOS non-shell binary startup",
    )
    step["run"] = (
        'test -x "$APM_BINARY_PATH"\n'
        f"echo uv run --frozen pytest {MACOS_TEST_ID} -vv -ra --tb=short\n"
    )

    with pytest.raises(AssertionError):
        _assert_macos_startup_step(workflow)
