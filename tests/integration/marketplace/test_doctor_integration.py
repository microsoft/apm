"""Integration tests for ``apm marketplace doctor``.

Strategy
--------
Tests invoke the ``doctor`` command via CliRunner and mock the subprocess
calls that probe git and network availability.  This keeps the tests
hermetic without requiring a real network or specific git version.

Scenarios covered:
- All checks pass when git is on PATH and network is reachable.
- Exit 1 when git is not on PATH.
- Auth check is informational: GITHUB_TOKEN set -> note in output.
- marketplace.yml check reports its status (informational).
- No Python tracebacks under any mocked scenario.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_git_ok(*args, **kwargs):
    """Fake subprocess.run that mimics a healthy git environment."""
    cmd = list(args[0]) if args else list(kwargs.get("args", []))
    m = MagicMock()
    m.returncode = 0
    if "git" in cmd and "--version" in cmd:
        m.stdout = "git version 2.42.0"
        m.stderr = ""
    elif "git" in cmd and "ls-remote" in cmd:
        # Network check (github.com/git/git.git HEAD)
        m.stdout = "abc123\tHEAD\n"
        m.stderr = ""
    else:
        m.stdout = ""
        m.stderr = ""
    return m


def _fake_git_not_found(*args, **kwargs):
    """Fake subprocess.run that raises FileNotFoundError (git not on PATH)."""
    raise FileNotFoundError("git not found")


def _fake_git_version_ok_network_fail(*args, **kwargs):
    """git --version succeeds; git ls-remote fails."""
    cmd = list(args[0]) if args else list(kwargs.get("args", []))
    m = MagicMock()
    if "git" in cmd and "--version" in cmd:
        m.returncode = 0
        m.stdout = "git version 2.42.0"
        m.stderr = ""
    elif "git" in cmd and "ls-remote" in cmd:
        m.returncode = 128
        m.stdout = ""
        m.stderr = "fatal: unable to access 'https://github.com/git/git.git/': timed out"
    else:
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
    return m


def _run_doctor(extra_args=(), env_overrides=None, yml_content=None, tmp_path=None):
    """Invoke doctor via CliRunner with subprocess.run patched."""
    runner = CliRunner()
    env = os.environ.copy()
    # Strip tokens so auth check is deterministic by default
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    if env_overrides:
        env.update(env_overrides)

    if tmp_path is not None and yml_content is not None:
        (tmp_path / "marketplace.yml").write_text(yml_content, encoding="utf-8")

    with runner.isolated_filesystem() as cwd:
        if tmp_path is not None and yml_content is not None:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
        with patch("subprocess.run", side_effect=_fake_git_ok):
            with patch.dict(os.environ, env, clear=True):
                result = runner.invoke(doctor, list(extra_args), catch_exceptions=False)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDoctorAllPass:
    """When git and network are available, doctor exits 0."""

    def test_exit_code_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_ok):
                with patch.dict(os.environ, {"GITHUB_TOKEN": ""}, clear=False):
                    result = runner.invoke(doctor, [], catch_exceptions=False)
        assert result.exit_code == 0

    def test_git_check_appears_in_output(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_ok):
                result = runner.invoke(doctor, [], catch_exceptions=False)
        assert "git" in result.output

    def test_network_check_appears_in_output(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_ok):
                result = runner.invoke(doctor, [], catch_exceptions=False)
        combined = result.output
        assert "network" in combined.lower() or "reachable" in combined.lower()

    def test_no_traceback(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_ok):
                result = runner.invoke(doctor, [], catch_exceptions=False)
        assert "Traceback" not in result.output


class TestDoctorGitNotFound:
    """When git is not on PATH, doctor exits 1."""

    def test_exit_code_one(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_not_found):
                result = runner.invoke(doctor, [], catch_exceptions=False)
        assert result.exit_code == 1

    def test_git_error_in_output(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_not_found):
                result = runner.invoke(doctor, [], catch_exceptions=False)
        combined = result.output
        assert "git" in combined.lower()

    def test_no_traceback_on_git_not_found(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_not_found):
                result = runner.invoke(doctor, [], catch_exceptions=False)
        assert "Traceback" not in result.output


class TestDoctorAuthCheck:
    """Auth check is informational and never fails the command."""

    def test_token_detected_note_appears(self):
        """When GITHUB_TOKEN is set, doctor notes it (does not print the value)."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_ok):
                with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test"}, clear=False):
                    result = runner.invoke(doctor, [], catch_exceptions=False)
        combined = result.output
        # Token presence should be noted
        assert "Token detected" in combined or "token" in combined.lower()
        # The token value must never appear in output
        assert "ghp_test" not in combined

    def test_no_token_note_appears(self):
        """When no token is set, doctor notes unauthenticated rate limits."""
        runner = CliRunner()
        # Remove all token env vars
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in ("GITHUB_TOKEN", "GH_TOKEN")
        }
        with runner.isolated_filesystem():
            with patch("subprocess.run", side_effect=_fake_git_ok):
                with patch.dict(os.environ, clean_env, clear=True):
                    result = runner.invoke(doctor, [], catch_exceptions=False)
        combined = result.output
        assert "auth" in combined.lower() or "token" in combined.lower()


class TestDoctorMarketplaceYml:
    """marketplace.yml check is informational."""

    def test_yml_present_and_valid_noted(self, tmp_path: Path):
        yml_content = """\
name: doc-test
description: Doctor test
version: 1.0.0
owner:
  name: Test Org
packages:
  - name: pkg
    source: org/pkg
    version: "^1.0.0"
    tags:
      - test
"""
        runner = CliRunner()
        (tmp_path / "marketplace.yml").write_text(yml_content, encoding="utf-8")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
            with patch("subprocess.run", side_effect=_fake_git_ok):
                result = runner.invoke(doctor, [], catch_exceptions=False)
        # Should mention marketplace.yml in the output table
        assert "marketplace.yml" in result.output

    def test_yml_absent_does_not_fail(self, tmp_path: Path):
        """Missing marketplace.yml is informational, not a critical failure."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch("subprocess.run", side_effect=_fake_git_ok):
                result = runner.invoke(doctor, [], catch_exceptions=False)
        # Critical checks (git, network) pass -> exit 0
        assert result.exit_code == 0
        assert "Traceback" not in result.output
