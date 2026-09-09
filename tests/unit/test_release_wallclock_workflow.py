"""The observed PR clock must include one complete, read-only production DAG."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import load_workflow, workflow_job, workflow_step

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci-release-wallclock.yml"


def _assert_single_world(workflow: dict) -> None:
    assert workflow["on"] == {
        "pull_request": {"types": ["labeled"]},
        "push": {"branches": ["danielmeppiel-release-wallclock-proof"]},
    }
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    jobs = workflow["jobs"]
    assert set(jobs) == {
        "plan",
        "source-checks",
        "platforms",
        "candidate-ready",
        "docs",
        "wheels",
        "verify",
    }
    assert jobs["plan"]["if"] == (
        "github.event_name == 'push' || github.event.label.name == 'ci-release-wallclock'"
    )
    for job in jobs.values():
        assert "secrets" not in job
        assert "environment" not in job
        assert not job.get("continue-on-error", False)
        assert set(job.get("permissions", {}).values()) <= {"read", "none"}
        for step in job.get("steps", []):
            assert "secrets." not in str(step)
            assert "action-gh-release" not in step.get("uses", "")
            assert "deploy-pages" not in step.get("uses", "")
            assert not step.get("continue-on-error", False)

    plan = workflow_step(jobs["plan"], "Select all five production catalog platforms")
    assert "GITHUB_RUN_ATTEMPT) !== 1" in plan["with"]["script"]
    assert "scripts/release-platforms.json" in plan["with"]["script"]
    assert "JSON.stringify({include: catalog})" in plan["with"]["script"]

    platforms = jobs["platforms"]
    assert platforms["uses"] == "./.github/workflows/release-platform.yml"
    assert platforms["needs"] == ["plan"]
    assert platforms["strategy"]["fail-fast"] is False
    assert platforms["strategy"]["matrix"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
    assert platforms["with"]["full-validation"] is True
    assert platforms["with"]["wallclock-evidence"] is True
    assert (
        not {
            "unit-performance-probe",
            "integration-performance-probe",
            "performance-workers",
        }
        & platforms["with"].keys()
    )
    for key in (
        "integration-markers",
        "integration-shard-count",
        "integration-xdist-workers",
        "integration-splitting-algorithm",
    ):
        assert platforms["with"][key] == "${{ matrix." + key.replace("-", "_") + " }}"

    for name, reusable in (
        ("source-checks", "ci"),
        ("docs", "docs-build"),
        ("wheels", "pypi-distributions"),
    ):
        assert jobs[name]["needs"] == ["plan"]
        assert jobs[name]["uses"] == f"./.github/workflows/{reusable}.yml"
    assert jobs["source-checks"]["with"] == {"suppress-pr-only-checks": True}

    ready = workflow_job(workflow, "candidate-ready")
    assert ready["needs"] == ["plan", "platforms", "source-checks"]
    record = workflow_step(
        ready, "Capture native bytes and successful authorities without qualification"
    )["with"]["script"]
    assert "collectArchiveInventory({" in record
    assert "selectRequiredJobs(jobs, catalog)" in record
    assert "kind: 'release-wallclock-native-index', promotable: false" in record
    assert "await qualify(" not in record
    assert "native_ids" in ready["outputs"] and "evidence_id" in ready["outputs"]
    verify = workflow_job(workflow, "verify")
    assert verify["name"] == "Verify wall-clock artifacts"
    assert set(verify["needs"]) == set(jobs) - {"verify"}
    assert "always()" in verify["if"] and "!cancelled()" in verify["if"]
    authorities = workflow_step(
        verify, "Require every production authority and record artifact identities"
    )["with"]["script"]
    assert "selectRequiredJobs(jobs, catalog)" in authorities
    assert "results[name]?.result !== 'success'" in authorities
    assert (
        "['plan', 'source-checks', 'platforms', 'candidate-ready', 'docs', 'wheels']" in authorities
    )
    assert "listJobsForWorkflowRunAttempt" in authorities
    assert "matches.length !== 1" in authorities
    assert "!artifact.expired" in authorities
    assert "nativeIds !== process.env.NATIVE_IDS" in authorities
    for suffix in ("core", "unit", "integration-shard-${index + 1}"):
        assert f"test-results-${{attempt}}-${{row.binary_name}}-{suffix}" in authorities
    assert "row.integration_shard_count" in authorities
    assert "core.setOutput('observation_ids'" in authorities
    observations = workflow_step(verify, "Download exact native runner observations")
    assert observations["with"] == {
        "artifact-ids": "${{ steps.authorities.outputs.observation_ids }}",
        "path": "wallclock-inputs/native/observations",
    }
    assert verify["steps"].index(observations) < verify["steps"].index(
        workflow_step(verify, "Verify built bytes without granting release qualification")
    )
    assert (
        workflow_step(verify, "Download exact current-run native artifacts")["with"]["artifact-ids"]
        == "${{ needs.candidate-ready.outputs.native_ids }}"
    )
    assert (
        "verifyLocalArchiveBytes({"
        in workflow_step(verify, "Recheck indexed native bytes and prepare non-promotable assets")[
            "with"
        ]["script"]
    )
    native_upload = workflow_step(verify, "Upload verified native bytes without publishing")
    assert native_upload["with"]["compression-level"] == 0
    assert native_upload["with"]["name"] == (
        "wallclock-proposed-verified-native-${{ github.run_attempt }}"
    )
    check = workflow_step(verify, "Verify built bytes without granting release qualification")
    assert "scripts.release_wallclock verify" in check["run"]
    assert '--side proposed --source-root . --source-sha "$GITHUB_SHA"' in check["run"]
    assert "--native-root" in check["run"]
    assert "--docs-root" in check["run"]
    assert "--python-root" in check["run"]
    assert "release_rehearsal compare" not in str(workflow)
    upload = workflow_step(verify, "Preserve non-promotable wall-clock evidence")
    assert upload["with"]["name"] == "wallclock-proposed-proof-${{ github.run_attempt }}"


def test_wallclock_executes_one_full_proposed_dag_before_its_terminal_clock() -> None:
    """Real elapsed time ends after every native/source/build-artifact authority."""
    _assert_single_world(load_workflow(WORKFLOW))


def test_pr_binary_smoke_remains_default_but_can_be_excluded_from_release_measurement() -> None:
    """A reusable caller must not accidentally inherit the PR-only binary job."""
    ci = load_workflow(ROOT / ".github/workflows/ci.yml")
    assert ci["on"]["workflow_call"]["inputs"]["suppress-pr-only-checks"] == {
        "type": "boolean",
        "default": False,
    }
    assert ci["jobs"]["pr-binary-smoke"]["if"] == (
        "github.event_name == 'pull_request' && !inputs.suppress-pr-only-checks"
    )
    for job_id, job in ci["jobs"].items():
        if job_id != "pr-binary-smoke":
            assert "suppress-pr-only-checks" not in job.get("if", "")


def test_windows_wallclock_keeps_public_dependency_validation_with_the_job_token() -> None:
    """Select public checks explicitly, without aliasing the job token into a PAT."""
    native = load_workflow(ROOT / ".github/workflows/release-platform.yml")
    step = workflow_step(
        workflow_job(native, "release-validation"), "Run release validation tests (Windows)"
    )
    assert step["env"]["GITHUB_API_TOKEN"] == "${{ github.token }}"
    assert step["env"]["GITHUB_APM_PAT"] == (
        "${{ !inputs.wallclock-evidence && secrets.GH_CLI_PAT || '' }}"
    )
    assert "-PublicApiOnly:('${{ inputs.wallclock-evidence }}' -eq 'true')" in step["run"]
    assert "GITHUB_TOKEN" not in step["env"]
    assert "GITHUB_MODELS_KEY" not in step["env"]
    assert "GH_MODELS_PAT" not in str(step)
    assert "APM_RUN_INFERENCE_TESTS" not in step["env"]


@pytest.mark.parametrize(
    ("filename", "job_id", "step_name", "output"),
    [
        ("release-platform", "build", "build", "build"),
        ("release-unit", "unit-tests", "unit", "unit"),
        ("release-integration", "integration-tests", "integration", "integration"),
    ],
)
def test_runner_observation_is_opt_in_and_records_the_executing_source(
    filename: str, job_id: str, step_name: str, output: str
) -> None:
    """Default release behavior is unchanged; measurements identify actual hosts."""
    workflow = load_workflow(ROOT / f".github/workflows/{filename}.yml")
    assert workflow["on"]["workflow_call"]["inputs"]["wallclock-evidence"] == {
        "type": "boolean",
        "default": False,
    }
    step = workflow_step(
        workflow_job(workflow, job_id),
        f"Record actual {step_name} runner for wall-clock proof",
    )
    assert step["if"] == "inputs.wallclock-evidence"
    assert step["shell"] == "bash"
    assert step["run"] == (
        "python -m scripts.release_wallclock record-job "
        '--side proposed --source-root . --source-sha "$GITHUB_SHA" '
        f"--output test-results/{output}-environment.json"
    )
    assert not step.get("continue-on-error", False)


@pytest.mark.parametrize(
    "fault",
    [
        "publish",
        "secret",
        "cancel",
        "synchronize",
        "partial",
        "no-observer",
        "both-worlds",
        "retuned-workers",
        "late-docs",
        "dropped-docs",
        "dropped-source",
        "waived-failure",
        "bypassed-authorities",
        "old-artifacts",
        "fake-clock",
        "collapsed-native-index",
        "pr-only-job",
        "broad-push-trigger",
        "missing-observation-download",
    ],
)
def test_wallclock_cannot_silently_change_the_measured_work(fault: str) -> None:
    """Reject changes that weaken controls or measure a different pipeline."""
    workflow = deepcopy(load_workflow(WORKFLOW))
    jobs = workflow["jobs"]
    if fault == "publish":
        workflow["permissions"]["contents"] = "write"
    elif fault == "missing-observation-download":
        jobs["verify"]["steps"].remove(
            workflow_step(jobs["verify"], "Download exact native runner observations")
        )
    elif fault == "secret":
        jobs["platforms"]["secrets"] = "inherit"
    elif fault == "cancel":
        workflow["concurrency"]["cancel-in-progress"] = True
    elif fault == "synchronize":
        workflow["on"]["pull_request"]["types"].append("synchronize")
    elif fault == "partial":
        jobs["platforms"]["with"]["full-validation"] = False
    elif fault == "no-observer":
        jobs["platforms"]["with"]["wallclock-evidence"] = False
    elif fault == "both-worlds":
        jobs["platforms"]["with"]["integration-performance-probe"] = True
    elif fault == "retuned-workers":
        jobs["platforms"]["with"]["integration-xdist-workers"] = 8
    elif fault == "late-docs":
        jobs["docs"]["needs"] = ["platforms"]
    elif fault == "dropped-docs":
        jobs["verify"]["needs"].remove("docs")
    elif fault == "dropped-source":
        jobs["source-checks"]["uses"] = "./.github/workflows/ci-source-performance.yml"
    elif fault == "waived-failure":
        jobs["platforms"]["continue-on-error"] = True
    elif fault == "bypassed-authorities":
        step = workflow_step(
            jobs["verify"], "Require every production authority and record artifact identities"
        )
        step["with"]["script"] = step["with"]["script"].replace(
            "selectRequiredJobs(jobs, catalog)", "[]"
        )
    elif fault == "old-artifacts":
        step = workflow_step(jobs["verify"], "Preserve non-promotable wall-clock evidence")
        step["with"]["name"] = "release-candidate-evidence"
    elif fault == "collapsed-native-index":
        del jobs["candidate-ready"]
    elif fault == "pr-only-job":
        jobs["source-checks"]["with"]["suppress-pr-only-checks"] = False
    elif fault == "broad-push-trigger":
        workflow["on"]["push"]["branches"] = ["**"]
    else:
        step = workflow_step(
            jobs["verify"], "Verify built bytes without granting release qualification"
        )
        step["run"] = "python -m scripts.release_rehearsal compare"
    with pytest.raises(AssertionError):
        _assert_single_world(workflow)
