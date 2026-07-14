"""Subprocess contracts for the rendered CLI documentation checker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from apm_cli.cli import cli
from scripts.check_cli_docs import public_top_level_commands

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_cli_docs.py"


def _render_page(dist: Path, name: str) -> None:
    page = dist / "reference" / "cli" / name / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<p>rendered</p>\n", encoding="utf-8")


def _render_public_pages(dist: Path, *, omit: set[str] | None = None) -> None:
    omitted = omit or set()
    for name in public_top_level_commands(cli) - omitted:
        _render_page(dist, name)


def _run_checker(dist: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(dist)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_checker_cli_reports_missing_rendered_page_on_stderr(tmp_path: Path) -> None:
    """A missing page exits 1, names the command, and explains recovery."""
    _render_public_pages(tmp_path, omit={"doctor"})

    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "executable commands missing rendered pages: doctor" in result.stderr
    assert "npm run build" in result.stderr
    assert "scripts/check_cli_docs.py" in result.stderr


def test_checker_cli_reports_orphan_rendered_page_on_stderr(tmp_path: Path) -> None:
    """An orphan page exits 1, names the route, and explains recovery."""
    _render_public_pages(tmp_path)
    _render_page(tmp_path, "not-a-command")

    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "rendered pages missing executable commands: not-a-command" in result.stderr
    assert "npm run build" in result.stderr
    assert "scripts/check_cli_docs.py" in result.stderr


def test_checker_cli_reserves_stdout_for_success(tmp_path: Path) -> None:
    """A matching rendered tree emits only successful evidence on stdout."""
    _render_public_pages(tmp_path)

    result = _run_checker(tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    assert "public CLI commands match" in result.stdout
