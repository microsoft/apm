"""CHANGELOG fragments from concurrent PRs merge in either order; [Unreleased] edits are caught."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from apm_cli.utils.git_env import get_git_executable, git_subprocess_env

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-ci-validation-topology")]

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / ".apm/skills/cut-release/scripts/changelog_fragments.py"
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Legacy fix. (#10)\n\n## [0.1.0] - 2026-01-01\n"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cut_release_changelog_fragments_merge", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fragments = _load()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [get_git_executable(), *args],
        cwd=repo,
        env=git_subprocess_env(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "changelog.d").mkdir(parents=True)
    (repo / "changelog.d/README.md").write_text("# fragments\n", encoding="ascii")
    (repo / "CHANGELOG.md").write_text(CHANGELOG, encoding="ascii")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    _commit(repo, "base")
    return repo


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _branch(repo: Path, name: str, path: str, text: str) -> None:
    _git(repo, "checkout", "-q", "-b", name, "main")
    (repo / path).write_text(text, encoding="ascii")
    _commit(repo, name)
    _git(repo, "checkout", "-q", "main")


@pytest.mark.parametrize("first", ["pr-21", "pr-22"])
def test_two_prs_adding_fragments_merge_in_either_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], first: str
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "pr-21", "changelog.d/21.fixed.md", "- Fix one. (#21)\n")
    _branch(repo, "pr-22", "changelog.d/22.fixed.md", "- Fix two. (#22)\n")

    second = "pr-22" if first == "pr-21" else "pr-21"
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", first)
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", second)

    assert sorted(path.name for path in (repo / "changelog.d").iterdir()) == [
        "21.fixed.md",
        "22.fixed.md",
        "README.md",
    ]
    assert fragments.main(["--root", str(repo), "render"]) == 0
    assert capsys.readouterr().out == (
        "### Fixed\n\n- Legacy fix. (#10)\n- Fix one. (#21)\n- Fix two. (#22)\n"
    )


@pytest.mark.parametrize(
    ("path", "text", "status"),
    [
        ("changelog.d/30.added.md", "- New thing. (#30)\n", 0),
        ("CHANGELOG.md", CHANGELOG.replace("(#10)", "(#10, #30)"), 0),
        ("CHANGELOG.md", CHANGELOG.replace("(#10)\n", "(#10)\n- New fix. (#30)\n"), 1),
    ],
    ids=["fragment", "edit-existing-bullet", "new-unreleased-bullet"],
)
def test_check_unreleased_rejects_only_new_bullets_since_the_merge_base(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str, text: str, status: int
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "pr", path, text)
    (repo / "changelog.d/40.fixed.md").write_text("- Main moved on. (#40)\n", encoding="ascii")
    _commit(repo, "main moves on")
    _git(repo, "checkout", "-q", "pr")

    assert fragments.main(["--root", str(repo), "check-unreleased", "--base-ref", "main"]) == status

    out = capsys.readouterr().out
    assert ("- New fix. (#30)" in out) == bool(status)
