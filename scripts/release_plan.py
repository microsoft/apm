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

Evidence for a SHA is a successful full run (schedule or repository_dispatch)
of build-release.yml on main for that exact SHA. Only a full run exercises
every platform's integration lanes, so nothing weaker promotes a tag.
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

Fetch = Callable[[str, str], Sequence[Mapping[str, object]]]


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


def find_evidence(sha: str, fetch: Fetch, *, current_run_id: str = "") -> str:
    """Return the URL of a green full run of sha on main, or ''."""
    for run in fetch(RELEASE_WORKFLOW, sha):
        if (
            run.get("head_sha") == sha
            and str(run.get("id")) != current_run_id
            and run.get("event") in FULL_EVENTS
            and run.get("head_branch") == EVIDENCE_BRANCH
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
        ):
            return str(run.get("html_url", ""))
    return ""


def decide(env: Mapping[str, str], fetch: Fetch) -> Plan:
    """Map the triggering event to a validation mode, checking tag evidence."""
    event = env.get("EVENT_NAME", "")
    if event in FULL_EVENTS:
        return Plan("full", publish=False)
    if event != "push":
        raise ValueError(f"build-release.yml does not handle event {event!r}")
    if env.get("REF_TYPE", "") != "tag":
        return Plan("push", publish=False)
    sha = env.get("SHA", "")
    try:
        evidence = find_evidence(sha, fetch, current_run_id=env.get("RUN_ID", ""))
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


def github_fetch(repository: str) -> Fetch:
    """List workflow runs for a head SHA with the gh CLI (GH_TOKEN from the job)."""

    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        query = urllib.parse.urlencode({"head_sha": sha, "per_page": "100"})
        gh = shutil.which("gh") or "gh"
        result = subprocess.run(  # noqa: S603 - resolved gh, argument vector, no shell
            [gh, "api", f"repos/{repository}/actions/workflows/{workflow}/runs?{query}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return list(json.loads(result.stdout).get("workflow_runs", []))

    return fetch


def _write(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(env: Mapping[str, str]) -> int:
    plan = decide(env, github_fetch(env.get("REPOSITORY", "")))
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
