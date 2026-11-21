"""Read a train's verdict: a merge-train.yml run on the -full branch at that exact SHA."""

from __future__ import annotations

from scripts.merge_queue.api import REPO, Api
from scripts.merge_queue.refs import parse_train_ref

WORKFLOW = "merge-train.yml"
GREEN, RED, PENDING, MISSING = "green", "red", "pending", "missing"


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


def prefix_verdict(api: Api, branch: str, sha: str) -> tuple[str, int | None]:
    """A guard or full prefix's run, for red attribution only: never proof of a train."""
    if parse_train_ref(branch) is None:
        raise ValueError(f"{branch} is not a train ref")
    return _newest(api, branch, sha)


def _newest(api: Api, branch: str, sha: str) -> tuple[str, int | None]:
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
        return MISSING, None
    newest = max(
        candidates, key=lambda run: (run["created_at"], run["id"], run.get("run_attempt", 1))
    )
    if newest.get("status") != "completed":
        return PENDING, newest["id"]
    return (GREEN if newest.get("conclusion") == "success" else RED), newest["id"]
