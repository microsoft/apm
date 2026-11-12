"""Keep the privileged fixture isolated, exact-head, bounded and fail-closed."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflow_contracts import load_workflow, workflow_job, workflow_step, workflow_step_index

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
JOB = "windows-native-standard-user-symlink-gate"
SCRIPT = "./scripts/windows_native_standard_user_symlink_gate.ps1"
EXACT_HEAD = "${{ github.event.pull_request.head.sha || github.sha }}"


def test_native_job_is_unconditional_bounded_and_read_only() -> None:
    job = workflow_job(load_workflow(WORKFLOW), JOB)
    assert job["runs-on"] == "windows-latest"
    assert job["permissions"] == {"contents": "read"}
    assert 0 < job["timeout-minutes"] <= 15
    assert "if" not in job
    assert not job.get("continue-on-error", False)
    checkout = job["steps"][0]
    assert checkout["uses"].split("@")[0] == "actions/checkout"
    assert checkout["env"] == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.autocrlf",
        "GIT_CONFIG_VALUE_0": "false",
    }
    assert checkout["with"] == {"ref": EXACT_HEAD, "persist-credentials": False}
    assert workflow_step(job, "Install dependencies")["run"] == "uv sync --frozen --extra dev"


def test_native_cleanup_and_evidence_survive_subject_failure() -> None:
    job = workflow_job(load_workflow(WORKFLOW), JOB)
    subject = workflow_step(job, "Prove native standard-user fallback")
    assert subject["env"] == {"APM_NATIVE_EXPECTED_HEAD": EXACT_HEAD}
    assert subject["run"] == f"{SCRIPT} -ExpectedHead $env:APM_NATIVE_EXPECTED_HEAD"
    assert subject["shell"] == "pwsh"
    assert 0 < subject["timeout-minutes"] <= 10
    assert not subject.get("continue-on-error", False)
    cleanup = workflow_step(job, "Restore owned native fixture")
    assert cleanup["if"] == "always()"
    assert cleanup["run"] == f"{SCRIPT} -CleanupOnly"
    assert cleanup["shell"] == "pwsh"
    assert 0 < cleanup["timeout-minutes"] <= 2
    assert not cleanup.get("continue-on-error", False)
    upload = workflow_step(job, "Retain native proof and restoration evidence")
    assert upload["if"] == "always()"
    assert upload["uses"].split("@")[0] == "actions/upload-artifact"
    assert upload["with"]["path"] == "${{ runner.temp }}/apm-native-symlink-evidence/"
    assert upload["with"]["if-no-files-found"] == "error"
    assert workflow_step_index(job, subject["name"]) < workflow_step_index(job, cleanup["name"])
    assert workflow_step_index(job, cleanup["name"]) < workflow_step_index(job, upload["name"])


def test_native_script_is_in_lint_format_and_duplication_checks() -> None:
    lint = workflow_job(load_workflow(WORKFLOW), "lint")
    for name in ("Ruff lint", "Ruff format check", "Code duplication guardrail (pylint R0801)"):
        assert "scripts/windows_native_symlink_probe_entry.py" in workflow_step(lint, name)["run"]
