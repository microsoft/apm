"""Integration coverage for deployed-files-present gitignore filtering.

Exercises _filter_gitignored() in ci_checks.py via a real git subprocess.
These are direct-call tests (no apm binary required) so they run on every
shard without the e2e / binary preconditions.

Regression guard for #2452: absent gitignored deploy paths must not be
flagged as missing by deployed-files-present.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
]


def _init_git_repo(path: Path) -> None:
    """Initialise a bare git repo suitable for gitignore lookups."""
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _write_lockfile(project: Path, content: str) -> None:
    (project / "apm.lock.yaml").write_text(content, encoding="utf-8")


class TestDeployedFilesGitignoreFiltering:
    """Verify _filter_gitignored() is called and works correctly in E2E path."""

    def test_absent_gitignored_deploy_path_does_not_fail_check(self, tmp_path: Path) -> None:
        """A deployed path absent because it is gitignored must pass the check.

        This is the primary regression guard for #2452.  The test uses a real
        git subprocess (not mocked) so it exercises _filter_gitignored() with
        the same code path used in production.
        """
        _init_git_repo(tmp_path)
        (tmp_path / ".gitignore").write_text(".agents/\n", encoding="utf-8")
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .agents/skills/skill-a/SKILL.md
                      - .agents/skills/skill-a
            """),
        )

        from apm_cli.deps.lockfile import LockFile, get_lockfile_path
        from apm_cli.policy.ci_checks import _check_deployed_files_present

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_deployed_files_present(tmp_path, lock)

        assert result.passed, (
            "deployed-files-present must pass when absent paths are gitignored; "
            f"got: {result.message}"
        )
        assert "gitignored paths skipped" in result.message, (
            f"success message must indicate gitignored paths were skipped; got: {result.message}"
        )

    def test_absent_non_gitignored_deploy_path_fails_check(self, tmp_path: Path) -> None:
        """A deployed path absent and NOT gitignored must still fail.

        Regression guard: the gitignore filter must not suppress genuine drift.
        _filter_gitignored() should return the path unchanged when not ignored.
        """
        _init_git_repo(tmp_path)
        # .gitignore covers only apm_modules/, not the deploy path
        (tmp_path / ".gitignore").write_text("apm_modules/\n", encoding="utf-8")
        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .github/instructions/foo.instructions.md
            """),
        )

        from apm_cli.deps.lockfile import LockFile, get_lockfile_path
        from apm_cli.policy.ci_checks import _check_deployed_files_present

        lock = LockFile.read(get_lockfile_path(tmp_path))
        result = _check_deployed_files_present(tmp_path, lock)

        assert not result.passed, (
            "deployed-files-present must fail when absent paths are not gitignored"
        )

    def test_filter_falls_back_when_git_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When git is unavailable _filter_gitignored falls back gracefully.

        The function must return the original list unchanged so that missing
        files are still reported (safe-fail: avoids silent false positives).
        """
        from unittest.mock import patch

        _write_lockfile(
            tmp_path,
            textwrap.dedent("""\
                lockfile_version: '1'
                generated_at: '2025-01-01T00:00:00Z'
                dependencies:
                  - repo_url: owner/repo
                    deployed_files:
                      - .agents/skills/skill-a/SKILL.md
            """),
        )

        from apm_cli.deps.lockfile import LockFile, get_lockfile_path
        from apm_cli.policy.ci_checks import _check_deployed_files_present
        from apm_cli.utils import git_env

        with patch.object(
            git_env,
            "get_git_executable",
            side_effect=FileNotFoundError("git not found"),
        ):
            lock = LockFile.read(get_lockfile_path(tmp_path))
            result = _check_deployed_files_present(tmp_path, lock)

        assert not result.passed, (
            "deployed-files-present must fail when git is unavailable and "
            "deployed files are absent -- fallback must not silently pass"
        )
