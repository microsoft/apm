"""train-gate's inherited baseline (merge-queue v2 step 5, design section 6).

Runs inside merge-train.yml's train-gate job, standard library plus ``gh`` only:

1. read this run's jobs; collect failing pytest node ids from each failed job's log
   (pytest's short summary prints ``FAILED <node id>`` / ``ERROR <node id>``);
2. read ``train-failures.json`` from the newest completed base probe run
   (``queue/b<base12>/p0-full`` at the base SHA): the inherited baseline;
3. residual = failing ids minus the baseline;
4. ``inherited_only`` is true when jobs failed, every failure is a known node id and
   the residual is empty (no cancelled, timed-out or id-less failure);
5. write the new ``train-failures.json`` and a step summary.

It never fails the job itself: any error means ``inherited_only=false``, so the
gate's job-result check decides alone.

python3 scripts/merge_queue/gate.py --run-id ID --base-sha SHA --out PATH
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "microsoft/apm")
ARTIFACT = "train-failures"
WORKFLOW = "merge-train.yml"
_NODE = re.compile(r"(?:^|\s)(?:FAILED|ERROR) (tests/\S+::\S+)")
_SELF = ("train-gate", "cleanup")
# Jobs that only aggregate others (``if: always()`` plus ``needs``) in the workflows a
# full train calls. One fails whenever a job it needs fails, without node ids of its
# own; tests/unit/scripts/merge_queue/test_gate.py pins this set to the workflows.
AGGREGATES = frozenset(
    {
        "Coverage Combine (Linux)",
        "Integration Tests (Linux)",
        "Lifecycle Smoke (Linux)",
        "Owner Surface Check",
        "Spec conformance gate",
        "Test Architecture Ratchets",
        "Windows Compatibility Gate",
    }
)
_INFRA = ("cancelled", "timed_out", "startup_failure")

Gh = Callable[[list[str]], str]


def gh(args: list[str]) -> str:
    return subprocess.run(  # noqa: S603
        ["gh", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def failing_ids(log: str) -> set[str]:
    """Node ids from pytest's short test summary lines in a job log."""
    return {match.group(1) for match in _NODE.finditer(log)}


def run_jobs(run: Gh, run_id: str) -> list[dict]:
    out = run(
        [
            "api",
            "--paginate",
            f"repos/{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
            "--jq",
            ".jobs[] | {id, name, conclusion}",
        ]
    )
    jobs = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [job for job in jobs if not job["name"].endswith(_SELF)]


def baseline(run: Gh, base_sha: str) -> tuple[set[str], str]:
    """The inherited failing ids and where they came from ('' when no probe exists)."""
    branch = f"queue/b{base_sha[:12]}/p0-full"
    out = run(
        [
            "api",
            f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs?branch={branch}&per_page=20",
            "--jq",
            f'[.workflow_runs[] | select(.status == "completed" and .head_sha == "{base_sha}")]'
            " | sort_by(.created_at) | last | .id // empty",
        ]
    ).strip()
    if not out:
        return set(), ""
    with tempfile.TemporaryDirectory() as directory:
        run(["run", "download", out, "--repo", REPO, "-n", ARTIFACT, "-D", directory])
        report = json.loads((Path(directory) / f"{ARTIFACT}.json").read_text(encoding="utf-8"))
    return set(report.get("failing", [])), f"run {out}"


def evaluate(jobs: Iterable[dict], logs: dict[int, str], inherited: set[str]) -> dict[str, object]:
    """The report train-gate publishes; ``inherited_only`` is its exemption."""
    failing: set[str] = set()
    unidentified: list[str] = []
    derived: list[str] = []
    failed_jobs = []
    for job in jobs:
        if job["conclusion"] in _INFRA:
            unidentified.append(job["name"])
        elif job["conclusion"] == "failure":
            failed_jobs.append(job["name"])
            ids = failing_ids(logs.get(job["id"], ""))
            if ids:
                failing |= ids
            elif job["name"].rpartition(" / ")[2] in AGGREGATES:
                derived.append(job["name"])
            else:
                unidentified.append(job["name"])
    if derived and len(derived) == len(failed_jobs) and not unidentified:
        unidentified, derived = derived, []
    residual = sorted(failing - inherited)
    return {
        "failing": sorted(failing),
        "inherited": sorted(failing & inherited),
        "residual": residual,
        "unidentified_jobs": sorted(unidentified),
        "derived_jobs": sorted(derived),
        "failed_jobs": sorted(failed_jobs),
        "inherited_only": bool(failed_jobs) and not unidentified and not residual,
    }


def summary(report: dict[str, object], source: str) -> str:
    lines = [
        "### inherited baseline",
        "",
        f"baseline: {source or 'none (no base probe for this base)'}",
        f"inherited_only: {str(report['inherited_only']).lower()}",
    ]
    for key in ("residual", "inherited", "unidentified_jobs"):
        values = report[key]
        lines.append(f"{key} ({len(values)}): " + (", ".join(values[:50]) or "-"))
    return "\n".join(lines) + "\n"


def main(argv: list[str], run: Gh = gh) -> int:
    parser = argparse.ArgumentParser(prog="gate.py")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        jobs = run_jobs(run, args.run_id)
        logs = {
            job["id"]: run(["api", f"repos/{REPO}/actions/jobs/{job['id']}/logs"])
            for job in jobs
            if job["conclusion"] == "failure"
        }
        inherited, source = baseline(run, args.base_sha)
        report = evaluate(jobs, logs, inherited)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
        report, source = {**evaluate([], {}, set()), "error": str(error)}, ""
    report["base"] = args.base_sha
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary(report, source))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"inherited_only={str(report['inherited_only']).lower()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
