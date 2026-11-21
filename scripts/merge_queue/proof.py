"""Read a train's verdict: a merge-train.yml run on the -full branch at that exact SHA."""

from __future__ import annotations

from scripts.merge_queue.api import REPO, Api

WORKFLOW = "merge-train.yml"
GREEN, RED, PENDING, MISSING = "green", "red", "pending", "missing"


def verdict(api: Api, branch: str, sha: str) -> tuple[str, int | None]:
    """The newest run of merge-train.yml for ``branch`` at ``sha``.

    A proof is a successful run on the -full branch at the exact commit
    (cicd.instructions: never a train-gate lookup by SHA alone).
    """
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
    newest = max(candidates, key=lambda run: (run.get("run_attempt", 1), run["created_at"]))
    if newest.get("status") != "completed":
        return PENDING, newest["id"]
    return (GREEN if newest.get("conclusion") == "success" else RED), newest["id"]
