"""Decide what one build-release.yml run validates and whether it may publish.

Modes (written to $GITHUB_OUTPUT as ``mode``):

* ``push`` -- a push to main. Unit tests and binaries on Linux and Windows.
* ``full`` -- schedule or repository_dispatch. Every lane runs: unit,
  sharded integration, macOS platform subsets and release validation. A
  successful full run is promotion evidence for its SHA.
* ``promoted`` -- a release tag whose SHA already has evidence. Binaries are
  built and smoke-tested and release validation runs, but unit and
  integration suites are not repeated.
* ``full`` with publish -- a release tag without evidence. The tag run
  validates everything itself, then publishes.

Releasing is always "push a v* tag"; evidence only makes it faster. Nothing
publishes without a green full validation of the exact SHA, either earlier
(evidence) or in the tag run itself. A failed evidence lookup falls back to
full validation, never to promotion.

A tag is planned only when its commit is tracked: the compare API reports the
tagged SHA ``identical`` to or ``behind`` main or at least one ``release/*``
branch. Any other tag, or a compare API error, fails the plan before the
evidence lookup, so no build or publish job runs.

Evidence for a SHA is a successful full run (schedule or repository_dispatch)
of build-release.yml on main for that exact SHA whose ``Integration Tests
Summary`` job succeeded. Only a full run exercises every platform's
integration lanes, so nothing weaker promotes a tag; a green dispatch that
ran no integration (for example ``publish-existing-tag`` recovery) is not
evidence.
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

FULL_EVENTS = frozenset({"schedule", "repository_dispatch"})
RELEASE_WORKFLOW = "build-release.yml"
EVIDENCE_BRANCH = "main"
RELEASE_BRANCH_PREFIX = "release/"
TRACKED_SOURCES = "main or a release/* branch"
TRACKED_STATUSES = frozenset({"identical", "behind"})

Fetch = Callable[[str, str], Sequence[Mapping[str, object]]]
FetchJobs = Callable[[str], Sequence[Mapping[str, object]]]
Compare = Callable[[str, str], str]
ReleaseBranches = Callable[[], Sequence[str]]
INTEGRATION_SUMMARY_JOB = "Integration Tests Summary"


class UntrackedTagError(RuntimeError):
    """A release tag whose commit is not confirmed on main or a release branch."""


@dataclass(frozen=True)
class Plan:
    mode: str
    publish: bool
    evidence: str = ""

    @property
    def run_unit(self) -> bool:
        return self.mode in ("push", "full")

    @property
    def full(self) -> bool:
        return self.mode == "full"


def find_evidence(sha: str, fetch: Fetch, jobs: FetchJobs, *, current_run_id: str = "") -> str:
    """Return the URL of a green full run of sha on main, or ''."""
    for run in fetch(RELEASE_WORKFLOW, sha):
        if (
            run.get("head_sha") == sha
            and str(run.get("id")) != current_run_id
            and run.get("event") in FULL_EVENTS
            and run.get("head_branch") == EVIDENCE_BRANCH
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and any(
                job.get("name") == INTEGRATION_SUMMARY_JOB and job.get("conclusion") == "success"
                for job in jobs(str(run.get("id")))
            )
        ):
            return str(run.get("html_url", ""))
    return ""


def tracked_branch(sha: str, compare: Compare, release_branches: ReleaseBranches) -> str:
    """Return the first of main and the release branches that contains sha, or ''.

    ``compare(base, sha)`` is the compare API status of ``base...sha``: sha is
    on base when it is ``identical`` or ``behind``. API errors propagate.
    """
    for base in (EVIDENCE_BRANCH, *release_branches()):
        if compare(base, sha) in TRACKED_STATUSES:
            return base
    return ""


def require_tracked_tag(
    tag: str, sha: str, compare: Compare | None, release_branches: ReleaseBranches | None
) -> str:
    """Return the branch that contains the tagged sha; raise when none is confirmed."""
    if compare is None or release_branches is None:
        raise UntrackedTagError(
            f"Tag {tag} ({sha}): no reachability check is configured; refusing to publish."
        )
    try:
        branch = tracked_branch(sha, compare, release_branches)
    except Exception as exc:
        raise UntrackedTagError(
            f"Could not confirm that tag {tag} ({sha}) is on {TRACKED_SOURCES}: {exc}. "
            "Nothing was built or published; re-run this job."
        ) from exc
    if not branch:
        raise UntrackedTagError(
            f"Tag {tag} points at {sha}, which is not on {TRACKED_SOURCES}. "
            f"Release tags must point at a commit on {TRACKED_SOURCES}; "
            "nothing was built or published."
        )
    return branch


def decide(
    env: Mapping[str, str],
    fetch: Fetch,
    jobs: FetchJobs,
    compare: Compare | None = None,
    release_branches: ReleaseBranches | None = None,
) -> Plan:
    """Map the triggering event to a validation mode, checking tag reachability and evidence."""
    event = env.get("EVENT_NAME", "")
    if event in FULL_EVENTS:
        return Plan("full", publish=False)
    if event != "push":
        raise ValueError(f"build-release.yml does not handle event {event!r}")
    if env.get("REF_TYPE", "") != "tag":
        return Plan("push", publish=False)
    sha = env.get("SHA", "")
    tag = env.get("REF_NAME", "")
    branch = require_tracked_tag(tag, sha, compare, release_branches)
    print(f"[+] Tag {tag} ({sha}) is on {branch}.")
    try:
        evidence = find_evidence(sha, fetch, jobs, current_run_id=env.get("RUN_ID", ""))
    except Exception as exc:  # any lookup failure means "no evidence"
        print(f"::warning title=Evidence lookup failed::{exc}; validating in this run.")
        evidence = ""
    if evidence:
        return Plan("promoted", publish=True, evidence=evidence)
    return Plan(
        "full",
        publish=True,
        evidence=(
            f"none: no green schedule/repository_dispatch run of {RELEASE_WORKFLOW} on "
            f"{EVIDENCE_BRANCH} for {sha}; full validation in this run"
        ),
    )


def _gh_json(path: str) -> object:
    gh = shutil.which("gh") or "gh"
    result = subprocess.run(  # noqa: S603 - resolved gh, argument vector, no shell
        [gh, "api", path],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def github_fetch(repository: str) -> tuple[Fetch, FetchJobs, Compare, ReleaseBranches]:
    """Workflow runs for a SHA, a run's jobs, branch comparison and release branches, via gh."""

    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        query = urllib.parse.urlencode({"head_sha": sha, "per_page": "100"})
        data = _gh_json(f"repos/{repository}/actions/workflows/{workflow}/runs?{query}")
        return list(data.get("workflow_runs", []))  # type: ignore[union-attr]

    def jobs(run_id: str) -> Sequence[Mapping[str, object]]:
        query = urllib.parse.urlencode({"filter": "latest", "per_page": "100"})
        data = _gh_json(f"repos/{repository}/actions/runs/{run_id}/jobs?{query}")
        return list(data.get("jobs", []))  # type: ignore[union-attr]

    def compare(base: str, sha: str) -> str:
        basehead = f"{urllib.parse.quote(base, safe='')}...{urllib.parse.quote(sha, safe='')}"
        data = _gh_json(f"repos/{repository}/compare/{basehead}?per_page=1")
        return str(data.get("status", ""))  # type: ignore[union-attr]

    def release_branches() -> Sequence[str]:
        prefix = f"refs/heads/{RELEASE_BRANCH_PREFIX}"
        data = _gh_json(f"repos/{repository}/git/matching-refs/heads/{RELEASE_BRANCH_PREFIX}")
        refs = [str(item.get("ref", "")) for item in data]  # type: ignore[union-attr]
        return [ref.removeprefix("refs/heads/") for ref in refs if ref.startswith(prefix)]

    return fetch, jobs, compare, release_branches


def _write(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(env: Mapping[str, str]) -> int:
    try:
        plan = decide(env, *github_fetch(env.get("REPOSITORY", "")))
    except UntrackedTagError as exc:
        print(f"::error title=Release tag refused::{exc}")
        _write(env.get("GITHUB_STEP_SUMMARY"), f"### Release plan\n\nRefused: {exc}\n")
        return 1
    outputs = {
        "mode": plan.mode,
        "publish": str(plan.publish).lower(),
        "run_unit": str(plan.run_unit).lower(),
        "full": str(plan.full).lower(),
    }
    _write(env.get("GITHUB_OUTPUT"), "".join(f"{key}={value}\n" for key, value in outputs.items()))
    summary = f"### Release plan\n\nmode: `{plan.mode}`, publish: `{str(plan.publish).lower()}`\n"
    if plan.evidence:
        summary += f"\nEvidence: {plan.evidence}\n"
    _write(env.get("GITHUB_STEP_SUMMARY"), summary)
    print(f"[i] mode={plan.mode} publish={str(plan.publish).lower()} evidence={plan.evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ))
