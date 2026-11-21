"""Read a train's verdict: a merge-train.yml run on the -full branch at that exact SHA."""

from __future__ import annotations

from scripts.merge_queue.api import REPO, Api
from scripts.merge_queue.refs import parse_train_ref

WORKFLOW = "merge-train.yml"
GREEN, RED, PENDING, MISSING = "green", "red", "pending", "missing"
VIOLATION, INFRA = "violation", "infra"
GATE_JOB, GUARD_JOB = "train-gate", "guard"
# The guard job's check steps (merge-train.yml); any other failing step is setup or
# infrastructure. Pinned by tests/quality/test_ci_topology.py.
GUARD_CHECKS = frozenset(
    {
        "Lint chain",
        "Run ratchet contract tests",
        "Run remaining quality tests",
        "Verify NOTICE is up to date",
    }
)


def verdict(api: Api, branch: str, sha: str) -> tuple[str, int | None]:
    """The newest run of merge-train.yml for ``branch`` at ``sha``.

    Only a ``-full`` branch can prove a train: a guard-only run never does
    (cicd.instructions: never a train-gate lookup by SHA alone). Runs are
    ordered by creation time and id; the attempt number only orders the
    attempts of one run.
    """
    parsed = parse_train_ref(branch)
    if parsed is None or parsed.scope != "full":
        raise ValueError(f"{branch} is not a -full train ref; it cannot prove a train")
    return _newest(api, branch, sha)


def _newest(api: Api, branch: str, sha: str) -> tuple[str, int | None]:
    """The newest run at ``branch`` and ``sha``; its verdict is its ``train-gate`` job
    in the latest attempt (the run conclusion stays failure when the gate excuses
    inherited failures). A missing or cancelled gate job is red."""
    run = _newest_run(api, branch, sha)
    if run is None:
        return MISSING, None
    if run.get("status") != "completed":
        return PENDING, run["id"]
    gate = run_job(api, run["id"], GATE_JOB)
    return (GREEN if gate and gate.get("conclusion") == "success" else RED), run["id"]


def _newest_run(api: Api, branch: str, sha: str) -> dict | None:
    runs = api.get(
        f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs",
        {"branch": branch, "head_sha": sha, "per_page": 5},
    )
    candidates = [
        run
        for run in (runs or {}).get("workflow_runs", [])
        if run.get("head_sha") == sha and run.get("head_branch") == branch
    ]
    if not candidates:
        return None
    return max(
        candidates, key=lambda run: (run["created_at"], run["id"], run.get("run_attempt", 1))
    )


def run_job(api: Api, run_id: int, name: str) -> dict | None:
    """One job of a run's latest attempt, with its steps."""
    jobs = api.get(
        f"repos/{REPO}/actions/runs/{run_id}/jobs", {"filter": "latest", "per_page": 100}
    )
    return next((job for job in (jobs or {}).get("jobs", []) if job.get("name") == name), None)


def guard_result(api: Api, branch: str, sha: str) -> str:
    """The ``guard`` job of a prefix run, for red attribution only (never a proof):
    GREEN, VIOLATION (a guard check step failed), INFRA (cancelled, timed out or a
    setup step failed), PENDING or MISSING."""
    if parse_train_ref(branch) is None:
        raise ValueError(f"{branch} is not a train ref")
    run = _newest_run(api, branch, sha)
    if run is None:
        return MISSING
    job = run_job(api, run["id"], GUARD_JOB)
    if job is None or job.get("status") != "completed":
        return PENDING if run.get("status") != "completed" else INFRA
    if job.get("conclusion") == "success":
        return GREEN
    failed = {
        step["name"] for step in job.get("steps") or () if step.get("conclusion") == "failure"
    }
    return (
        VIOLATION
        if job.get("conclusion") == "failure" and failed and failed <= GUARD_CHECKS
        else INFRA
    )
