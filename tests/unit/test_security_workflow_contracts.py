"""Trust-boundary contracts for privileged GitHub Actions workflows."""

from pathlib import Path

import pytest

from tests.workflow_contracts import (
    assert_unconditional,
    load_workflow,
    workflow_job,
    workflow_step,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "filename",
    [
        "ci.yml",
        "merge-gate.yml",
        "notice-drift.yml",
        "codeql.yml",
        "spec-conformance.yml",
        "devcontainer-feature-test.yml",
        "crlf-invariance.yml",
        "docs.yml",
    ],
)
def test_pr_checks_cover_stacked_bases(filename: str) -> None:
    """An upper PR needs checks on its own head before its base reaches main."""
    workflow = load_workflow(ROOT / ".github" / "workflows" / filename)
    events = workflow["on"]
    assert "pull_request" in events
    assert "pull_request_target" not in events
    filters = events["pull_request"] or {}
    assert not {"branches", "branches-ignore"}.intersection(filters)
    if filename in {"ci.yml", "merge-gate.yml", "notice-drift.yml"}:
        assert not {"paths", "paths-ignore"}.intersection(filters)


def test_codeql_covers_merge_queue_with_existing_analysis_configurations() -> None:
    """Required scans must run on the queue SHA, not just the earlier PR SHA."""
    workflow = load_workflow(ROOT / ".github" / "workflows" / "codeql.yml")
    assert workflow["on"]["merge_group"] == {
        "branches": ["main"],
        "types": ["checks_requested"],
    }
    assert workflow["on"]["push"] == {"branches": ["main"]}

    analyze = workflow_job(workflow, "analyze")
    assert analyze["strategy"]["matrix"]["language"] == ["python", "actions"]
    init = workflow_step(analyze, "Initialize CodeQL")
    assert init["with"]["languages"] == "${{ matrix.language }}"
    upload = workflow_step(analyze, "Perform CodeQL Analysis")
    assert upload.get("with", {}).get("upload", True) is True
    for node, label in (
        (analyze, "CodeQL matrix"),
        (init, "CodeQL initialization"),
        (upload, "CodeQL analysis upload"),
    ):
        assert_unconditional(node, label=label)


def test_merge_gate_executes_base_commit_script() -> None:
    workflow = load_workflow(ROOT / ".github" / "workflows" / "merge-gate.yml")
    assert "workflow_dispatch" not in workflow["on"]
    gate = workflow_job(workflow, "gate")
    checkout = workflow_step(gate, "Checkout trusted gate implementation")
    assert checkout["with"]["ref"] == "${{ steps.sha.outputs.trusted_sha }}"
    wait = workflow_step(gate, "Wait for all required checks")
    assert wait["env"]["SHA"] == "${{ steps.sha.outputs.target_sha }}"


def test_secret_bearing_manual_runs_use_default_branch_dispatch() -> None:
    for name, event_type in (
        ("build-release.yml", "manual-build-release"),
        ("ci-runtime.yml", "manual-runtime-inference"),
    ):
        workflow = load_workflow(ROOT / ".github" / "workflows" / name)
        assert "workflow_dispatch" not in workflow["on"]
        assert workflow["on"]["repository_dispatch"]["types"] == [event_type]


def test_pypi_publisher_only_downloads_and_publishes() -> None:
    workflow = load_workflow(ROOT / ".github" / "workflows" / "build-release.yml")
    builder = workflow_job(workflow, "build-pypi-distributions")
    assert builder["permissions"] == {"contents": "read"}
    sync = workflow_step(builder, "Install locked build dependencies")
    assert sync["run"] == "uv sync --frozen --extra dev"
    build = workflow_step(builder, "Build Python package")
    assert build["run"] == "uv build --no-build-isolation --no-sources"
    assert all("uvx" not in str(step.get("run", "")) for step in builder["steps"])

    publisher = workflow_job(workflow, "publish-pypi")
    assert publisher["permissions"] == {"actions": "read", "id-token": "write"}
    steps = publisher["steps"]
    assert [step["name"] for step in steps] == [
        "Download Python distributions",
        "Publish to PyPI",
    ]
    assert all("run" not in step for step in steps)
