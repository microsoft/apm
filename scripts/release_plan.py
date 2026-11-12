"""Decide what one build-release.yml run validates and whether it may publish.

Modes (written to $GITHUB_OUTPUT as ``mode``):

* ``push`` -- a push to main. Unit tests and binaries on Linux and Windows.
* ``full`` -- schedule or repository_dispatch. Every lane runs: unit,
  sharded integration, macOS platform subsets and release validation. A
  successful full run is promotion evidence for its SHA.
* ``promoted`` -- a release tag whose SHA already has evidence. Binaries are
  built and smoke-tested and release validation runs, but unit and
  integration suites are not repeated.

A tag without evidence fails closed. The manual override is the repository
variable ``APM_RELEASE_FULL_VALIDATION_TAG`` set to that exact tag (passed in
as FULL_VALIDATION_TAG): the re-run tag run then validates everything itself
before publishing. The override names one tag, so it cannot linger.

Evidence for a SHA is either a successful full run of build-release.yml on
the evidence branch, or a successful push run of build-release.yml plus a
successful merge-queue run of ci-integration.yml.

Rehearsing a pipeline change without a tag: add rehearsal branches to the
push trigger and set FULL_VALIDATION_BRANCHES / PROMOTION_BRANCHES (and
EVIDENCE_BRANCH / EVIDENCE_EVENTS="push") for the release-plan step. Those
branches never publish: publication requires a pushed tag. #3582 rehearsed
this way and removed the trigger before merge.
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
MERGE_QUEUE_WORKFLOW = "ci-integration.yml"

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


class MissingEvidence(RuntimeError):
    """The tag's SHA has no full-validation evidence."""


def _csv(value: str | None) -> frozenset[str]:
    return frozenset(part.strip() for part in (value or "").split(",") if part.strip())


def find_evidence(
    sha: str,
    fetch: Fetch,
    *,
    evidence_branch: str,
    evidence_events: frozenset[str],
    current_run_id: str = "",
) -> str:
    """Return the URL of a run proving sha was fully validated, or ''."""
    release_runs = [
        run
        for run in fetch(RELEASE_WORKFLOW, sha)
        if run.get("head_sha") == sha
        and str(run.get("id")) != current_run_id
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_branch") == evidence_branch
    ]
    for run in release_runs:
        if run.get("event") in evidence_events:
            return str(run.get("html_url", ""))
    pushed = [run for run in release_runs if run.get("event") == "push"]
    if pushed:
        for run in fetch(MERGE_QUEUE_WORKFLOW, sha):
            if (
                run.get("head_sha") == sha
                and run.get("event") == "merge_group"
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
            ):
                return f"{pushed[0].get('html_url', '')} + {run.get('html_url', '')}"
    return ""


def decide(env: Mapping[str, str], fetch: Fetch) -> Plan:
    """Map the triggering event to a validation mode, checking tag evidence."""
    event = env.get("EVENT_NAME", "")
    ref_name = env.get("REF_NAME", "")
    is_tag = env.get("REF_TYPE", "") == "tag"
    if event in FULL_EVENTS:
        return Plan("full", publish=False)
    if event != "push":
        raise ValueError(f"build-release.yml does not handle event {event!r}")
    if not is_tag:
        if ref_name in _csv(env.get("FULL_VALIDATION_BRANCHES")):
            return Plan("full", publish=False)
        if ref_name not in _csv(env.get("PROMOTION_BRANCHES")):
            return Plan("push", publish=False)
    if is_tag and ref_name and env.get("FULL_VALIDATION_TAG", "") == ref_name:
        return Plan("full", publish=True, evidence="manual override: full validation in this run")
    sha = env.get("SHA", "")
    evidence = find_evidence(
        sha,
        fetch,
        evidence_branch=env.get("EVIDENCE_BRANCH") or "main",
        evidence_events=_csv(env.get("EVIDENCE_EVENTS")) or FULL_EVENTS,
        current_run_id=env.get("RUN_ID", ""),
    )
    if not evidence:
        raise MissingEvidence(
            f"{ref_name} at {sha} has no full-validation evidence: no successful "
            f"schedule/repository_dispatch run of {RELEASE_WORKFLOW} on "
            f"{env.get('EVIDENCE_BRANCH') or 'main'}, and no successful push run plus "
            f"merge-queue run of {MERGE_QUEUE_WORKFLOW}, for this SHA."
        )
    return Plan("promoted", publish=is_tag, evidence=evidence)


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
    fetch = github_fetch(env.get("REPOSITORY", ""))
    try:
        plan = decide(env, fetch)
    except MissingEvidence as exc:
        ref = env.get("REF_NAME", "<tag>")
        repository = env.get("REPOSITORY", "<owner>/<repo>")
        print(f"::error title=Release promotion refused::{exc}")
        print(
            "[x] Refusing to publish an unverified SHA. Either:\n"
            "    1. produce evidence (full validation of main), then re-run this workflow:\n"
            f"         gh api repos/{repository}/dispatches -f event_type=manual-build-release\n"
            "    2. or override for this one tag (validate everything in the re-run):\n"
            f"         gh variable set APM_RELEASE_FULL_VALIDATION_TAG --body {ref}\n"
            "       then re-run this workflow, and delete the variable afterwards."
        )
        _write(env.get("GITHUB_STEP_SUMMARY"), f"### Release promotion refused\n\n{exc}\n")
        return 1
    outputs = {
        "mode": plan.mode,
        "publish": str(plan.publish).lower(),
        "run_unit": str(plan.run_unit).lower(),
        "full": str(plan.full).lower(),
        "evidence": plan.evidence,
    }
    _write(env.get("GITHUB_OUTPUT"), "".join(f"{key}={value}\n" for key, value in outputs.items()))
    summary = f"### Release plan\n\nmode: `{plan.mode}`, publish: `{str(plan.publish).lower()}`\n"
    if plan.evidence:
        summary += f"\nPromotion evidence: {plan.evidence}\n"
    _write(env.get("GITHUB_STEP_SUMMARY"), summary)
    print(f"[i] mode={plan.mode} publish={str(plan.publish).lower()} evidence={plan.evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ))
