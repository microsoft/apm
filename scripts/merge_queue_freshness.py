"""Fail when main moves without passing through the merge queue.

merge-gate.yml's merge_group EXPECTED_CHECKS is the merge-time gate: full
integration (ci-integration.yml), the binary build, release validation and
the global coverage floor block a merge only there. A ruleset bypass or a
queue that is switched off lets commits reach main without any of it, and
nothing turns red. This check does: when main received commits in the last
WINDOW_DAYS days but no merge_group run of ci-integration.yml completed in
that window, it fails and names the last merge_group run and the main
commits that landed since.

Run by .github/workflows/merge-queue-freshness.yml on a schedule. Inputs
(environment): GH_TOKEN, REPOSITORY (owner/repo), BRANCH (default main),
WINDOW_DAYS (default 3).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

INTEGRATION_WORKFLOW = "ci-integration.yml"
MERGE_GROUP_EVENT = "merge_group"
COMMIT_PAGE_SIZE = 100

Fetch = Callable[[str], object]


@dataclass(frozen=True)
class MergeGroupRun:
    run_id: int
    url: str
    completed_at: datetime


@dataclass(frozen=True)
class Verdict:
    fresh: bool
    lines: tuple[str, ...]


def _timestamp(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_merge_group_run(fetch: Fetch, repository: str) -> MergeGroupRun | None:
    """The most recent completed merge_group run of ci-integration.yml."""
    query = urllib.parse.urlencode(
        {"event": MERGE_GROUP_EVENT, "status": "completed", "per_page": "1"}
    )
    data = fetch(f"repos/{repository}/actions/workflows/{INTEGRATION_WORKFLOW}/runs?{query}")
    runs = data.get("workflow_runs", []) if isinstance(data, Mapping) else []
    if not runs:
        return None
    run = runs[0]
    return MergeGroupRun(
        run_id=int(run["id"]),
        url=str(run.get("html_url", "")),
        completed_at=_timestamp(run.get("updated_at") or run["created_at"]),
    )


def main_commits_since(
    fetch: Fetch, repository: str, branch: str, since: datetime
) -> tuple[str, ...]:
    """SHAs that reached the branch since a moment (newest first, one page)."""
    query = urllib.parse.urlencode(
        {"sha": branch, "since": _iso(since), "per_page": str(COMMIT_PAGE_SIZE)}
    )
    data = fetch(f"repos/{repository}/commits?{query}")
    commits = data if isinstance(data, list) else []
    return tuple(str(commit["sha"]) for commit in commits)


def evaluate(
    fetch: Fetch, repository: str, branch: str, window_days: int, now: datetime
) -> Verdict:
    """Fresh when a merge_group run completed in the window or main did not move."""
    window_start = now - timedelta(days=window_days)
    last_run = last_merge_group_run(fetch, repository)
    if last_run is not None and last_run.completed_at >= window_start:
        return Verdict(
            True,
            (
                f"[+] merge_group run {last_run.run_id} completed {_iso(last_run.completed_at)} "
                f"(within {window_days} days): {last_run.url}",
            ),
        )
    if not main_commits_since(fetch, repository, branch, window_start):
        return Verdict(True, (f"[+] {branch} received no commits in the last {window_days} days.",))

    since = last_run.completed_at if last_run is not None else window_start
    landed = main_commits_since(fetch, repository, branch, since)
    if last_run is None:
        last = f"no completed merge_group run of {INTEGRATION_WORKFLOW} was found"
    else:
        last = (
            f"last merge_group run {last_run.run_id} completed "
            f"{_iso(last_run.completed_at)}: {last_run.url}"
        )
    truncated = " (newest page only)" if len(landed) == COMMIT_PAGE_SIZE else ""
    return Verdict(
        False,
        (
            f"[x] {branch} received commits but no merge_group run of {INTEGRATION_WORKFLOW} "
            f"completed in the last {window_days} days: merges are bypassing the merge-time gate.",
            f"[i] {last}",
            f"[i] {len(landed)} {branch} commit(s) since {_iso(since)}{truncated}:",
            *(f"    {sha}" for sha in landed),
        ),
    )


def _gh_api(path: str) -> object:
    gh = shutil.which("gh") or "gh"
    result = subprocess.run(  # noqa: S603 - resolved gh, argument vector, no shell
        [gh, "api", path],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def main(env: Mapping[str, str], fetch: Fetch = _gh_api, now: datetime | None = None) -> int:
    repository = env.get("REPOSITORY", "")
    branch = env.get("BRANCH", "main")
    window_days = int(env.get("WINDOW_DAYS", "3"))
    verdict = evaluate(fetch, repository, branch, window_days, now or datetime.now(UTC))
    for line in verdict.lines:
        print(line)
    if not verdict.fresh:
        print(f"::error title=Merge queue is not gating {branch}::{verdict.lines[0][4:]}")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                "### Merge queue freshness\n\n```\n" + "\n".join(verdict.lines) + "\n```\n"
            )
    return 0 if verdict.fresh else 1


def _entrypoint(argv: Sequence[str]) -> int:
    if len(argv) > 1:
        print("merge_queue_freshness.py takes no arguments; configure it with env vars.")
        return 2
    return main(os.environ)


if __name__ == "__main__":
    sys.exit(_entrypoint(sys.argv))
