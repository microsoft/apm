"""Rule H4 gate: a PR closing an issue a strict known cell cites must flip the cell (#4565)."""

from __future__ import annotations

import pytest

from tests.quality import known_cell_closing
from tests.quality.known_cell_closing import cells_closed_by, closed_issues

RATCHET_TEST_SCOPE = "repository"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_CELLS = {
    "machine:install.fresh": "#4703",
    "source:tests/unit/test_x.py:12": "#4742",
}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Fixes #4703", {4703}),
        ("closes #1, resolved #2 and FIXED: #3", {1, 2, 3}),
        ("Close #7\nfix #8\nresolve #9\nCLOSED #10\nResolves #11", {7, 8, 9, 10, 11}),
        ("Fixes microsoft/apm#4703 and fixes Microsoft/APM#5", {4703, 5}),
        ("Refs #4703; see #4742; related to #1", set()),
        ("Fixes other/repo#4703; hotfix #4742; prefixes #3", set()),
        ("<!-- Fixes #4703 -->\n```\nfixes #4742\n```\n`closes #1`", set()),
        ("``closes #1 ` x``\n\n~~~~\nfixes #2\n~~~~\n\n    resolves #3\n", set()),
        ("**Fixes** #4 and [fixes #5](https://example.com)", {4, 5}),
        ("Issue: Fixes #N | Refs #N", set()),
    ],
)
def test_closed_issues_reads_closing_keywords_only(body: str, expected: set[int]) -> None:
    assert closed_issues(body) == expected


def test_cells_closed_by_names_only_cells_citing_a_closed_issue() -> None:
    assert cells_closed_by({4703, 1}, _CELLS) == {"machine:install.fresh": "#4703"}
    assert cells_closed_by({1}, _CELLS) == {}


def _run(body: str, tmp_path, monkeypatch, capsys) -> tuple[int, str]:
    monkeypatch.setattr(known_cell_closing, "known_cells", lambda: dict(_CELLS))
    path = tmp_path / "body.md"
    path.write_text(body, encoding="utf-8")
    code = known_cell_closing.main([str(path)])
    return code, capsys.readouterr().out


def test_pr_closing_a_cited_issue_fails_naming_the_cell(tmp_path, monkeypatch, capsys) -> None:
    code, out = _run("Fixes #4742\nRefs #4703", tmp_path, monkeypatch, capsys)
    assert code == 1
    assert "source:tests/unit/test_x.py:12 -> #4742" in out
    assert "machine:install.fresh" not in out
    assert "python -m tests.quality.known_cell_issues --write" in out


def test_pr_closing_an_uncited_issue_passes(tmp_path, monkeypatch, capsys) -> None:
    code, out = _run("Fixes #99\nRefs #4703", tmp_path, monkeypatch, capsys)
    assert code == 0
    assert out.startswith("[+] H4")


def test_commit_message_closing_a_cited_issue_fails_despite_refs_body(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(known_cell_closing, "known_cells", lambda: dict(_CELLS))
    body = tmp_path / "body.md"
    body.write_text("Refs #4703", encoding="utf-8")
    commits = tmp_path / "commits.txt"
    commits.write_text("fix: route files\n\nFixes #4703\n", encoding="utf-8")
    assert known_cell_closing.main([str(body), str(commits)]) == 1
    assert "machine:install.fresh -> #4703" in capsys.readouterr().out


def test_gate_reads_the_body_from_stdin(monkeypatch, capsys) -> None:
    import io

    monkeypatch.setattr(known_cell_closing, "known_cells", lambda: dict(_CELLS))
    monkeypatch.setattr("sys.stdin", io.StringIO("closes microsoft/apm#4703"))
    assert known_cell_closing.main([]) == 1
    assert "machine:install.fresh -> #4703" in capsys.readouterr().out
