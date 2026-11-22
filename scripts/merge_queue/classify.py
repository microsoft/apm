"""Classify a red train and choose the action (merge-queue v2 step 5, design section 6).

Order:
1. OWN-guard: the earliest red ``p<k>-guard`` prefix names PR k. Eject it.
2. INFRA: a job was cancelled, timed out or failed without node ids. Rerun the
   failed jobs once on the same SHA; a second infra red HALTs the queue.
3. OWN-attributed: a residual node's test file, or the source file it tests (same
   stem), is touched by exactly one member. Eject that member.
4. Otherwise rerun once (a FLAKE passes on the rerun and lands; the runner logs it).
   Still red: OWN-unattributed. Bisect by halves; a single member is ejected.

Inherited failures never reach this module: train-gate already subtracted them.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scripts.merge_queue.api import REPO

RERUN, EJECT, BISECT, HALT = "rerun", "eject", "bisect", "halt"
REPORT = "train-failures"


@dataclass(frozen=True)
class Decision:
    klass: str
    action: str
    culprit: int | None = None
    first_half: tuple[int, ...] = ()
    evidence: tuple[str, ...] = ()


def _stem(path: str) -> str:
    return PurePosixPath(path).stem


def attribute(residual: Sequence[str], members: Sequence[tuple[int, Sequence[str]]]) -> int | None:
    """The one member touching a failing node's test file, or the file it tests."""
    for node in residual:
        test_file = node.split("::", 1)[0]
        subject = _stem(test_file).removeprefix("test_")
        touching = {
            pr
            for pr, files in members
            if test_file in files or any(_stem(f) == subject for f in files if f.endswith(".py"))
        }
        if len(touching) == 1:
            return touching.pop()
    return None


def classify(
    report: dict,
    members: Sequence[tuple[int, Sequence[str]]],
    red_guard: int | None,
    reruns: int,
) -> Decision:
    if red_guard is not None:
        return Decision("own-guard", EJECT, red_guard, evidence=(f"p{red_guard}-guard red",))
    unidentified = tuple(report.get("unidentified_jobs") or ())
    if not report:
        unidentified = ("no train-failures report",)
    elif report.get("error"):
        # gate.py could not collect its evidence: never blame a member for that.
        unidentified = (f"evidence collection failed: {report['error']}", *unidentified)
    if unidentified:
        return Decision("infra", RERUN if reruns == 0 else HALT, evidence=unidentified)
    residual = tuple(report.get("residual") or ())
    culprit = attribute(residual, members)
    if culprit is not None:
        return Decision("own-attributed", EJECT, culprit, evidence=residual)
    if reruns == 0:
        return Decision("flake-candidate", RERUN, evidence=residual)
    prs = [pr for pr, _ in members]
    if len(prs) == 1:
        return Decision("own-unattributed", EJECT, prs[0], evidence=residual)
    return Decision(
        "own-unattributed", BISECT, first_half=tuple(prs[: len(prs) // 2]), evidence=residual
    )


def read_report(run_id: int) -> dict:
    """train-gate's train-failures.json for a run ({} when it cannot be read)."""
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(  # noqa: S603
            ["gh", "run", "download", str(run_id), "--repo", REPO, "-n", REPORT, "-D", directory],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        path = Path(directory) / f"{REPORT}.json"
        if result.returncode != 0 or not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
