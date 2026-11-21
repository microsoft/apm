"""Every strict known cell cites an open issue and its own failure signature (#4565).

On 2026-11-21, 21 strict known cells cited closed issues: their fixes had
landed, yet the cells kept failing for other reasons that a bare strict xfail
could not see. A known cell is a lifecycle known cell or any strict xfail
in ``tests/``; a deferred design (``KnownFailure(kind="deferred-design")``)
also needs its tracker open. This gate is offline. It checks the tracking
issues against
``known_cell_issue_states.json`` (``python -m tests.quality.known_cell_issues
--write`` refreshes it). It also requires every strict xfail in
``tests/lifecycle`` to come from ``KnownFailure`` (``tests/lifecycle/known.py``),
whose signature the lifecycle conftest enforces.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from tests.lifecycle.known import TRACKING, KnownFailure
from tests.quality.known_cell_issues import (
    ROOT,
    cited_issues,
    closing_issues,
    known_cells,
    load_snapshot,
    pr_closing_issues,
    unflipped_cells,
)

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


def test_a_pr_that_closes_a_cited_issue_flips_its_cells() -> None:
    """In CI, a PR whose body closes an issue must flip every strict cell citing it.

    Outside a pull_request event (local runs, push, merge_group) nothing closes.
    """
    closing = pr_closing_issues(os.environ.get("GITHUB_EVENT_PATH"))
    remedies = unflipped_cells(closing)
    assert not remedies, "\n".join(remedies)


def test_the_flip_check_reads_closing_keywords_only(tmp_path: Path) -> None:
    """Negative control: closing a cited issue without flipping its cell fails."""
    cells = {
        "generated:G|marketplace-install|org-deny|bin": "#3703",
        "source:tests/lifecycle/scenarios/test_x.py:61": "#4594",
        "journey:E01:e01-sarif-audit-prints-no-advice": "#4570",
    }
    assert closing_issues("Refs #3703. See #4594 and microsoft/apm#4570.") == set()
    assert closing_issues("This fixed 3 bugs; closes the gap.") == set()
    assert closing_issues("Closes https://github.com/microsoft/apm/issues/4570") == {4570}
    body = "Fixes #3703\nThis closes microsoft/apm#4594; resolved: #9"
    assert closing_issues(body) == {3703, 4594, 9}
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"body": body}}), encoding="utf-8")
    assert unflipped_cells(pr_closing_issues(str(event)), cells) == [
        "PR closes #3703 but strict cell G|marketplace-install|org-deny|bin in "
        "tests/quality/generated_obligations.json still cites it: flip it to required "
        "in this PR.",
        "PR closes #4594 but strict cell line 61 in tests/lifecycle/scenarios/test_x.py "
        "still cites it: flip it to required in this PR.",
    ]
    event.write_text(json.dumps({"merge_group": {"head_sha": "abc"}}), encoding="utf-8")
    assert pr_closing_issues(str(event)) == set()


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


        @CELL.mark()
        def test_known_and_new_cause_in_one_oracle_failure():
            raise AssertionError(
                "after install:\\n"
                "I4 (never widen): p/hook still deployed but not allowed\\n"
                "I1 (lock matches disk): deployed file is not in the lock: x\\n"
                "args=('apm', 'install') rc=0"
            )


        @CELL.mark()
        def test_only_known_causes_in_one_oracle_failure():
            raise AssertionError(
                "after install:\\n"
                "I4 (never widen): p/hook still deployed but not allowed\\n"
                "I4 (never widen): p/mcp still deployed but not allowed\\n"
                "args=('apm', 'install') rc=0"
            )
        """
    )
    result = pytester.runpytest_inprocess("-q", "-o", "addopts=", "-p", "tests.lifecycle.known")
    result.assert_outcomes(xfailed=2, failed=2)
    result.stdout.fnmatch_lines(["*failed for a different reason*"])
    result.stdout.fnmatch_lines(["*test_new_cause*"])
    result.stdout.fnmatch_lines(["*test_known_and_new_cause_in_one_oracle_failure*"])
    with pytest.raises(AssertionError):
        KnownFailure("#0", "unfiled", "x")
    with pytest.raises(AssertionError):
        KnownFailure("#4543", "deferred", "x", kind="wontfix")
