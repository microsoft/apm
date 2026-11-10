"""Integration tests for ``apm marketplace check``.

Strategy
--------
Tests write a real marketplace.yml and invoke the ``check`` command via
CliRunner with RefResolver.list_remote_refs patched.

Scenarios covered:
- All entries resolvable -> exit 0.
- One entry unreachable -> exit 1 with error in output.
- Offline mode reports cached-only error per-row.
- Missing marketplace.yml -> exit 1.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest  # noqa: F401
from click.testing import CliRunner

from apm_cli.commands.marketplace import check
from apm_cli.marketplace.errors import GitLsRemoteError, OfflineMissError
from apm_cli.marketplace.ref_resolver import RemoteRef

# ---------------------------------------------------------------------------
# YAML fixtures
# ---------------------------------------------------------------------------

_CHECK_YML = """\
name: check-test
description: Marketplace for check tests
version: 1.0.0
owner:
  name: Test Org
packages:
  - name: plugin-a
    source: org/plugin-a
    version: "^1.0.0"
    tags:
      - test
  - name: plugin-b
    source: org/plugin-b
    ref: v2.0.0
    tags:
      - test
"""

_SINGLE_ENTRY_YML = """\
name: single
description: Single-entry check
version: 1.0.0
owner:
  name: Test Org
packages:
  - name: only-plugin
    source: org/only-plugin
    version: "^1.0.0"
    tags:
      - test
"""


def _refs_ok(owner_repo: str):
    """All packages have satisfying refs."""
    return {
        "org/plugin-a": [
            RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40),
            RemoteRef(name="refs/tags/v1.2.0", sha="b" * 40),
        ],
        "org/plugin-b": [
            RemoteRef(name="refs/tags/v2.0.0", sha="c" * 40),
        ],
        "org/only-plugin": [
            RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40),
        ],
    }.get(owner_repo, [])


def _refs_plugin_a_missing(owner_repo: str):
    """plugin-a returns no refs (simulating a remote error via empty list)."""
    if owner_repo == "org/plugin-a":
        return []
    return _refs_ok(owner_repo)


def _run_check(tmp_path: Path, yml_content, extra_args=(), side_effect=None):
    if side_effect is None:
        side_effect = _refs_ok

    runner = CliRunner()
    (tmp_path / "marketplace.yml").write_text(yml_content, encoding="utf-8")

    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        import shutil

        shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            side_effect=side_effect,
        ):
            result = runner.invoke(check, list(extra_args), catch_exceptions=False)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckAllReachable:
    """When all entries are resolvable, check exits 0."""

    def test_exit_code_zero_all_ok(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=_refs_ok)
        assert result.exit_code == 0

    def test_success_message_in_output(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=_refs_ok)
        combined = result.output
        assert "OK" in combined or "[+]" in combined

    def test_package_names_appear(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=_refs_ok)
        assert "plugin-a" in result.output
        assert "plugin-b" in result.output


class TestCheckOneUnreachable:
    """When one entry is unreachable, check exits 1."""

    def _side_effect_raise(self, owner_repo: str):
        if owner_repo == "org/plugin-a":
            raise GitLsRemoteError(
                package="plugin-a",
                summary="git ls-remote failed for 'org/plugin-a'",
                hint="Check network.",
            )
        return _refs_ok(owner_repo)

    def test_exit_code_one_on_unreachable(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=self._side_effect_raise)
        assert result.exit_code == 1

    def test_error_summary_in_output(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=self._side_effect_raise)
        combined = result.output
        # Either [x] marker or "issues" text should appear
        assert "[x]" in combined or "issue" in combined.lower() or "error" in combined.lower()

    def test_error_entry_named_in_output(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=self._side_effect_raise)
        assert "plugin-a" in result.output

    def test_other_entries_still_reported(self, tmp_path: Path):
        """Entries that succeed must still appear in the output."""
        result = _run_check(tmp_path, _CHECK_YML, side_effect=self._side_effect_raise)
        assert "plugin-b" in result.output


class TestCheckOffline:
    """--offline flag reports cached-only status."""

    @staticmethod
    def _raise_offline_miss(owner_repo: str):
        """Simulate RefResolver in offline mode with an empty cache."""
        raise OfflineMissError(package="", remote=owner_repo)

    def test_offline_mode_exits_nonzero_on_empty_cache(self, tmp_path: Path):
        """Offline with empty cache must fail (OfflineMissError per entry)."""
        result = _run_check(
            tmp_path,
            _CHECK_YML,
            extra_args=["--offline"],
            side_effect=self._raise_offline_miss,
        )
        # No cache -> all entries fail; exit 1 expected
        assert result.exit_code == 1
        assert "Traceback" not in result.output

    def test_offline_mode_does_not_crash(self, tmp_path: Path):
        """Offline flag must not produce a Python traceback."""
        result = _run_check(
            tmp_path,
            _CHECK_YML,
            extra_args=["--offline"],
            side_effect=self._raise_offline_miss,
        )
        assert "Traceback" not in result.output


class TestCheckMissingYml:
    """check without marketplace.yml exits 1."""

    def test_missing_yml_exits_1(self, tmp_path: Path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(check, [], catch_exceptions=False)
        assert result.exit_code == 1


def _flat(text: str) -> str:
    """Drop all whitespace so assertions survive any terminal-width wrapping."""
    return "".join(text.split())


class TestCheckCommitShaRefRealGit:
    """#3458: a full commit SHA ``ref`` is verified against a real git remote.

    Only the remote URL builder is redirected to a local bare repository;
    ``git ls-remote`` and the commit fetch run for real.
    """

    @staticmethod
    def _git(*args: str, cwd: Path) -> str:
        import subprocess

        from apm_cli.utils.git_env import get_git_executable

        return subprocess.run(
            [
                get_git_executable(),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _remote(self, tmp_path: Path) -> tuple[Path, str]:
        work = tmp_path / "work"
        work.mkdir()
        self._git("init", "-q", "-b", "main", cwd=work)
        (work / "f.txt").write_text("v1", encoding="utf-8")
        self._git("add", "f.txt", cwd=work)
        self._git("commit", "-q", "-m", "one", cwd=work)
        pinned = self._git("rev-parse", "HEAD", cwd=work)
        self._git("tag", "v1.0.0", cwd=work)
        (work / "f.txt").write_text("v2", encoding="utf-8")
        self._git("commit", "-q", "-am", "two", cwd=work)
        (work / "f.txt").write_text("v3", encoding="utf-8")
        self._git("commit", "-q", "-am", "three", cwd=work)
        middle = self._git("rev-parse", "HEAD~1", cwd=work)
        bare = tmp_path / "remote.git"
        self._git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
        assert pinned != middle
        return bare, middle

    def _invoke(self, tmp_path: Path, monkeypatch, bare: Path, ref: str):
        yml = (
            "name: sha-check\n"
            "description: SHA check\n"
            "version: 1.0.0\n"
            "owner:\n"
            "  name: Test Org\n"
            "packages:\n"
            "  - name: pinned\n"
            "    source: org/pinned\n"
            f'    ref: "{ref}"\n'
        )
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        (project / "marketplace.yml").write_text(yml, encoding="utf-8")
        monkeypatch.chdir(project)
        # Plain one-line rows keep detail assertions independent of terminal width.
        monkeypatch.setattr("apm_cli.commands.marketplace._get_console", lambda: None)
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver._git_url_and_env",
            lambda self, owner_repo, remote_url=None: (str(bare), {}),
        ):
            return CliRunner().invoke(check, [], catch_exceptions=False)

    def test_reachable_non_tip_sha_passes(self, tmp_path: Path, monkeypatch):
        bare, middle = self._remote(tmp_path)
        result = self._invoke(tmp_path, monkeypatch, bare, middle)
        assert result.exit_code == 0, result.output
        assert _flat("All 1 entries OK") in _flat(result.output)

    def test_unreachable_sha_fails(self, tmp_path: Path, monkeypatch):
        bare, _ = self._remote(tmp_path)
        missing = "0" * 40
        result = self._invoke(tmp_path, monkeypatch, bare, missing)
        assert result.exit_code == 1
        assert _flat(f"Commit '{missing}' not reachable on remote") in _flat(result.output)

    def test_abbreviated_sha_fails_with_hint(self, tmp_path: Path, monkeypatch):
        bare, middle = self._remote(tmp_path)
        result = self._invoke(tmp_path, monkeypatch, bare, middle[:12])
        assert result.exit_code == 1
        assert _flat("full 40-char lowercase SHA") in _flat(result.output)

    def test_tagged_tree_sha_is_not_a_commit_pin(self, tmp_path: Path, monkeypatch):
        bare, _ = self._remote(tmp_path)
        tree = self._git("--git-dir", str(bare), "rev-parse", "v1.0.0^{tree}", cwd=tmp_path)
        # A lightweight tag naming a tree object, not a commit.
        self._git("--git-dir", str(bare), "tag", "tree-tag", tree, cwd=tmp_path)
        result = self._invoke(tmp_path, monkeypatch, bare, tree)
        assert result.exit_code == 1
        assert _flat(f"Commit '{tree}' not reachable on remote") in _flat(result.output)

    def test_tag_ref_still_passes(self, tmp_path: Path, monkeypatch):
        bare, _ = self._remote(tmp_path)
        result = self._invoke(tmp_path, monkeypatch, bare, "v1.0.0")
        assert result.exit_code == 0, result.output
