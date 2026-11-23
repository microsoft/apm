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


def test_a_known_cell_whose_fixture_breaks_for_another_reason_fails(
    pytester: pytest.Pytester,
) -> None:
    """A setup failure never stands in for the tracked defect the test body would hit."""
    pytester.makeini("[pytest]")
    pytester.makepyfile(
        """
        import pytest

        from tests.lifecycle.known import KnownFailure

        CELL = KnownFailure("#3703", "user scope keeps the surface", r"still deployed")


        @pytest.fixture
        def broken_world():
            raise AssertionError("world template failed to build")


        @CELL.mark()
        def test_body_never_runs(broken_world):
            assert False, "I4: p/hook still deployed but not allowed"
        """
    )
    result = pytester.runpytest_inprocess("-q", "-o", "addopts=", "-p", "tests.lifecycle.known")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*failed for a different reason*"])


def _stub(states: dict[int, str], calls: list[str]):
    """A GraphQL transport answering *states*; it records each query it receives."""

    def transport(query: str) -> dict:
        calls.append(query)
        found = {f"i{n}": {"state": state} for n, state in states.items()}
        return {"data": {"repository": found}}

    return transport


def test_check_live_fails_naming_a_cell_whose_issue_closed(capsys) -> None:
    from tests.quality.known_cell_issues import check_live

    cells = {"source:tests/lifecycle/a.py:10": "#11", "source:tests/lifecycle/b.py:20": "#12"}
    calls: list[str] = []

    code = check_live(cells, transport=_stub({11: "OPEN", 12: "CLOSED"}, calls))

    out = capsys.readouterr().out
    assert code == 1, out
    assert "[x] source:tests/lifecycle/b.py:20 cites #12 (CLOSED)" in out, out
    assert "flip it to required" in out and "--write" in out, out
    assert "a.py" not in out, out
    assert len(calls) == 1 and "i11:" in calls[0] and "i12:" in calls[0], calls


def test_check_live_passes_when_every_issue_is_open(capsys) -> None:
    from tests.quality.known_cell_issues import check_live

    cells = {"machine:G1|x": "#11", "generated:pair|y": "#11"}

    code = check_live(cells, transport=_stub({11: "OPEN"}, []))

    assert code == 0
    assert "[+] 2 known cells cite 1 open issues" in capsys.readouterr().out


def test_check_live_reports_an_issue_github_does_not_return(capsys) -> None:
    from tests.quality.known_cell_issues import check_live

    code = check_live({"rule:r": "#13"}, transport=_stub({}, []))

    assert code == 1
    assert "[x] rule:r cites #13 (MISSING)" in capsys.readouterr().out


def test_check_live_fails_when_github_cannot_be_read(capsys) -> None:
    from tests.quality.known_cell_issues import check_live

    def broken(query: str) -> dict:
        raise RuntimeError("HTTP 401")

    assert check_live({"rule:r": "#13"}, transport=broken) == 2
    assert "could not read issue state: HTTP 401" in capsys.readouterr().out


def _gh_reply(monkeypatch, returncode: int, stdout: str) -> None:
    """``gh api graphql`` exits *returncode* and prints *stdout*."""
    import subprocess

    def run(args, **_kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="gh: error")

    monkeypatch.setattr("subprocess.run", run)


def test_check_live_reports_a_nonexistent_issue_from_ghs_nonzero_reply(monkeypatch, capsys) -> None:
    """gh exits 1 on a per-issue NOT_FOUND but still prints the data: MISSING, exit 1."""
    from tests.quality.known_cell_issues import check_live

    reply = {
        "data": {"repository": {"i11": {"state": "OPEN"}, "i13": None}},
        "errors": [
            {
                "type": "NOT_FOUND",
                "path": ["repository", "i13"],
                "message": "Could not resolve to an issue or pull request with the number of 13.",
            }
        ],
    }
    _gh_reply(monkeypatch, 1, json.dumps(reply))

    code = check_live({"rule:a": "#11", "rule:b": "#13"})

    out = capsys.readouterr().out
    assert code == 1, out
    assert "[x] rule:b cites #13 (MISSING)" in out, out
    assert "rule:a" not in out, out


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (
            1,
            '{"data": {"repository": null}, "errors": [{"type": "FORBIDDEN", '
            '"message": "Resource not accessible by integration"}]}',
        ),
        (1, '{"data": {"repository": {"i13": null}}, "errors": [{"type": "RATE_LIMITED"}]}'),
        (0, "not json"),
        (0, "[]"),
    ],
    ids=["forbidden", "rate-limited", "unreadable", "not-an-object"],
)
def test_check_live_exits_2_when_github_cannot_be_read(
    monkeypatch, capsys, returncode, stdout
) -> None:
    from tests.quality.known_cell_issues import check_live

    _gh_reply(monkeypatch, returncode, stdout)

    code = check_live({"rule:r": "#13"})

    out = capsys.readouterr().out
    assert code == 2, out
    assert "could not read issue state" in out and "MISSING" not in out, out


def test_check_live_exits_2_when_gh_is_not_installed(monkeypatch, capsys) -> None:
    from tests.quality.known_cell_issues import check_live

    def missing(args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr("subprocess.run", missing)

    code = check_live({"rule:r": "#13"})

    out = capsys.readouterr().out
    assert code == 2, out
    assert "could not read issue state: gh api graphql could not run" in out, out
