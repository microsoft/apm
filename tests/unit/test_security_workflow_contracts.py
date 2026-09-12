"""Trust-boundary contracts for privileged GitHub Actions workflows."""

from pathlib import Path

from tests.workflow_contracts import (
    assert_unconditional,
    load_workflow,
    workflow_job,
    workflow_step,
)

ROOT = Path(__file__).resolve().parents[2]


def test_codeql_covers_merge_queue_with_existing_analysis_configurations() -> None:
    """Required scans must run on the queue SHA, not just the earlier PR SHA."""
    workflow = load_workflow(ROOT / ".github" / "workflows" / "codeql.yml")
    assert workflow["on"]["merge_group"] == {
        "branches": ["main"],
        "types": ["checks_requested"],
    }
    for event in ("pull_request", "push"):
        assert workflow["on"][event] == {"branches": ["main"]}

    analyze = workflow_job(workflow, "analyze")
    assert analyze["env"]["CODEQL_ACTION_DIFF_INFORMED_QUERIES"] == "false"
    assert analyze["strategy"]["matrix"]["language"] == [
        "python",
        "actions",
        "javascript-typescript",
    ]
    init = workflow_step(analyze, "Initialize CodeQL")
    assert init["with"]["languages"] == "${{ matrix.language }}"
    upload = workflow_step(analyze, "Perform CodeQL Analysis")
    assert upload.get("with", {}).get("upload", True) is True
    assert upload["with"]["upload-database"] is True
    assert upload["with"]["wait-for-processing"] is True
    assert upload["with"].get("skip-queries", False) is False
    assert "category" not in upload["with"]
    assert workflow["on"]["schedule"]
    assert upload["id"] == "analyze"
    evidence = workflow_step(analyze, "Record CodeQL upload identity")
    assert evidence["env"]["SARIF_ID"] == "${{ steps.analyze.outputs.sarif-id }}"
    artifact = workflow_step(analyze, "Upload CodeQL policy evidence")
    assert (
        artifact["with"]["name"] == "codeql-policy-${{ matrix.language }}-${{ github.run_attempt }}"
    )
    assert artifact["with"]["if-no-files-found"] == "error"
    assert artifact["with"]["retention-days"] == 14
    for node, label in (
        (analyze, "CodeQL matrix"),
        (init, "CodeQL initialization"),
        (upload, "CodeQL analysis upload"),
        (evidence, "CodeQL upload identity"),
        (artifact, "CodeQL evidence artifact"),
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
    assert workflow["permissions"]["actions"] == "read"
    assert workflow["permissions"]["security-events"] == "read"
    script = (ROOT / ".github/scripts/ci/merge_gate_wait.sh").read_text(encoding="utf-8")
    assert script.count('python3 "$(dirname "$0")/codeql_policy.py" --deadline "$deadline"') == 2
    assert script.index("codeql_policy.py") < script.index('echo "[merge-gate] waiting')


def test_codeql_policy_is_in_the_ci_lint_scope() -> None:
    workflow = load_workflow(ROOT / ".github/workflows/ci.yml")
    lint = workflow_job(workflow, "lint")
    for name in ("Ruff lint", "Ruff format check", "Code duplication guardrail (pylint R0801)"):
        assert ".github/scripts/ci/codeql_policy.py" in workflow_step(lint, name)["run"]


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
