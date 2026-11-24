"""Count budgets: the deterministic layer of the performance promise engine.

A journey's counters (git spawns by verb, HTTP requests and connects, hashing,
files written, path calls, lockfile reads) come from
:func:`tests.fixtures.perf.counters.run_counted` and the offline world's
traffic. They are counts, so they are the same on every runner and can gate a
required check. ``tests/perf/count_budgets.json`` holds one row per journey and
counter: ``{"budget", "measured", "reason", "pr"}``, with
``budget = measured + slack(measured)``. Any counter over its budget fails, and
so does a counter with no row (an unbudgeted cost). A counter that drops below
its measured count by more than the slack yields a "tighten" note, so budgets ratchet
down in an explicit PR.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from pathlib import Path

BUDGETS_PATH = Path(__file__).resolve().parents[2] / "perf" / "count_budgets.json"
# The budgeted journeys, all at tree-10, offline mirror, rtt 0.
JOURNEYS = (
    "install-cold",
    "install-warm",
    "install-noop",
    "outdated",
    "update",
    "install-cold-auth",
    "install-warm-auth",
)
_FS_COUNTERS = (
    "files_written",
    "lockfile_reads",
    "realpath_calls",
    "stat_calls",
    "lstat_calls",
    "hash_objects",
    "hashed_bytes",
)
_HTTP_COUNTERS = ("requests", "connects")
_PR = re.compile(r"#[1-9]\d*")


def slack(measured: int) -> int:
    """The headroom a budget gives its measured count: max(1, 5%)."""
    return max(1, math.ceil(measured * 0.05))


def flatten(counts: Mapping[str, object], traffic: Mapping[str, int]) -> dict[str, int]:
    """``git.<verb>``, ``http.<counter>`` and ``fs.<counter>`` from one command's counters."""
    flat = {f"git.{verb}": int(n) for verb, n in (counts.get("git_verbs") or {}).items()}
    flat.update({f"http.{name}": int(traffic.get(name, 0)) for name in _HTTP_COUNTERS})
    fs = counts.get("fs") or {}
    flat.update({f"fs.{name}": int(fs.get(name, 0)) for name in _FS_COUNTERS})
    return dict(sorted(flat.items()))


def load_budgets(path: Path = BUDGETS_PATH) -> dict[str, dict[str, dict]]:
    return json.loads(path.read_text(encoding="utf-8"))["journeys"]


def compare(
    journey: str, observed: Mapping[str, int], rows: Mapping[str, Mapping[str, object]]
) -> tuple[list[str], list[str]]:
    """(over-budget lines, tighten notes) for *journey*'s *observed* counters."""
    over, tighten = [], []
    for counter in sorted(set(observed) | set(rows)):
        value = observed.get(counter, 0)
        row = rows.get(counter)
        budget = int(row["budget"]) if row else 0
        if value > budget:
            label = "unbudgeted" if row is None else f"budget {budget}"
            over.append(
                f"{journey} {counter}: {label}, observed {value} (+{value - budget}); "
                "a regression, or a justified change that needs a budget row with reason and pr"
            )
        elif row and value < int(row["measured"]) - slack(int(row["measured"])):
            # Below the measured baseline by more than its slack: a real drop, not jitter.
            tighten.append(
                f"{journey} {counter}: observed {value}, budget {budget}: tighten to "
                f"{value + slack(value)} in a PR"
            )
    return over, tighten


def row_problems(journeys: Mapping[str, Mapping[str, Mapping[str, object]]]) -> list[str]:
    """Every reason the budget table is malformed (missing journey, reason, pr or slack)."""
    problems = [f"journey {name} has no budget rows" for name in JOURNEYS if name not in journeys]
    problems += [
        f"journey {name} is not budgeted by any check" for name in journeys if name not in JOURNEYS
    ]
    for journey, rows in sorted(journeys.items()):
        for counter, row in sorted(rows.items()):
            where = f"{journey} {counter}"
            if not str(row.get("reason") or "").strip():
                problems.append(f"{where}: no reason")
            if not _PR.fullmatch(str(row.get("pr") or "")):
                problems.append(f"{where}: pr must be '#<number>', got {row.get('pr')!r}")
            budget, measured = row.get("budget"), row.get("measured")
            if not isinstance(budget, int) or not isinstance(measured, int) or measured < 0:
                problems.append(f"{where}: budget and measured must be non-negative integers")
            elif budget < measured:
                problems.append(f"{where}: budget {budget} is below measured {measured}")
            elif budget > measured + slack(measured):
                problems.append(
                    f"{where}: budget {budget} exceeds measured {measured} + slack "
                    f"{slack(measured)}; budgets only grow with a re-measured row"
                )
    return problems
