"""Every strict known cell cites an open issue and its own failure signature (#4565).

On 2026-11-21, 21 strict known cells cited closed issues: their fixes had
landed, yet the cells kept failing for other reasons that a bare strict xfail
could not see. This gate is offline. It checks the tracking issues against
``known_cell_issue_states.json`` (``python -m tests.quality.known_cell_issues
--write`` refreshes it). It also requires every strict xfail in
``tests/lifecycle`` to come from ``KnownFailure`` (``tests/lifecycle/known.py``),
whose signature the lifecycle conftest enforces.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.lifecycle.known import TRACKING, KnownFailure
from tests.quality.known_cell_issues import ROOT, cited_issues, known_cells, load_snapshot

RATCHET_TEST_SCOPE = "repository"

pytest_plugins = ["pytester"]

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_REGEN = "python -m tests.quality.known_cell_issues --write"
_KNOWN_MODULE = ROOT / "tests" / "lifecycle" / "known.py"


def test_every_known_cell_cites_a_filed_issue() -> None:
    cells = known_cells()
    assert cells, "no known cells found: the discovery is broken"
    bad = {label: tracking for label, tracking in cells.items() if not TRACKING.fullmatch(tracking)}
    assert not bad, f"strict known cells must cite '#<issue>' (n > 0): {bad}"


def test_snapshot_records_exactly_the_cited_issues() -> None:
    recorded = sorted(int(n) for n in load_snapshot())
    assert recorded == cited_issues(), f"known_cell_issue_states.json is stale; run {_REGEN}"


def test_no_known_cell_cites_a_closed_issue() -> None:
    snapshot = load_snapshot()
    closed = {
        label: tracking
        for label, tracking in known_cells().items()
        if snapshot.get(tracking.lstrip("#")) != "OPEN"
    }
    assert not closed, (
        "strict known cells cite closed issues: rerun each cell; move it to required if it "
        f"passes, report a product bug or re-key it to an open issue if not: {closed}"
    )


def _raw_xfail_uses(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "xfail"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    ]


def test_lifecycle_strict_xfails_come_from_known_failure() -> None:
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{line}"
        for path in sorted((ROOT / "tests" / "lifecycle").rglob("*.py"))
        if path != _KNOWN_MODULE
        for line in _raw_xfail_uses(path)
    ]
    assert not offenders, f"use tests.lifecycle.known.KnownFailure(...).mark(): {offenders}"


def test_a_known_cell_failing_for_another_reason_fails(pytester: pytest.Pytester) -> None:
    pytester.makeini("[pytest]")
    pytester.makepyfile(
        """
        from tests.lifecycle.known import KnownFailure

        CELL = KnownFailure("#3703", "user scope keeps the surface", r"still deployed")


        @CELL.mark()
        def test_same_cause():
            assert False, "I4: p/hook still deployed but not allowed"


        @CELL.mark()
        def test_new_cause():
            assert False, "output lacks /denied by pattern/"
        """
    )
    result = pytester.runpytest_inprocess("-q", "-o", "addopts=", "-p", "tests.lifecycle.known")
    result.assert_outcomes(xfailed=1, failed=1)
    result.stdout.fnmatch_lines(["*failed for a different reason*", "*test_new_cause*"])
    with pytest.raises(AssertionError):
        KnownFailure("#0", "unfiled", "x")
