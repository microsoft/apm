"""Tests for shared CLI helper functions in apm_cli.commands._helpers.

Focuses on the I/O helpers (_atomic_write, _update_gitignore_for_apm_modules),
config helpers (_load_apm_config, _get_default_script, _list_available_scripts),
and update notification helper (_check_and_notify_updates).
"""

import os
import tempfile  # noqa: F401
from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest
import yaml  # noqa: F401

from apm_cli.commands._helpers import (
    _atomic_write,
    _check_and_notify_updates,
    _get_default_script,
    _list_available_scripts,
    _load_apm_config,
    _scan_installed_packages,
    _update_gitignore_for_apm_modules,
)

# ---------------------------------------------------------------------------
# _atomic_write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """Tests for _atomic_write."""

    def test_writes_content_to_file(self, tmp_path):
        """Normal write creates file with expected content."""
        target = tmp_path / "output.txt"
        _atomic_write(target, "hello world")
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_overwrites_existing_file(self, tmp_path):
        """Atomic write replaces existing file content."""
        target = tmp_path / "output.txt"
        target.write_text("old content", encoding="utf-8")
        _atomic_write(target, "new content")
        assert target.read_text(encoding="utf-8") == "new content"

    def test_writes_empty_string(self, tmp_path):
        """Empty string can be written atomically."""
        target = tmp_path / "empty.txt"
        _atomic_write(target, "")
        assert target.read_text(encoding="utf-8") == ""

    def test_writes_unicode_content(self, tmp_path):
        """Unicode content is written correctly."""
        target = tmp_path / "unicode.txt"
        text = "hello 🚀 world\n日本語"
        _atomic_write(target, text)
        assert target.read_text(encoding="utf-8") == text

    def test_cleans_up_temp_file_on_write_error(self, tmp_path):
        """Temporary file is removed when write fails."""
        target = tmp_path / "output.txt"
        # Patch os.replace to raise so we hit the cleanup path
        with patch("os.replace", side_effect=OSError("replace failed")):
            with pytest.raises(OSError, match="replace failed"):
                _atomic_write(target, "data")
        # No stale temp file should remain in tmp_path. Prefix-agnostic so
        # the assertion does not silently pass if the temp prefix changes.
        leftover = [f for f in tmp_path.iterdir() if f != target]
        assert leftover == [], f"Temp file not cleaned up: {leftover}"


# ---------------------------------------------------------------------------
# _update_gitignore_for_apm_modules
# ---------------------------------------------------------------------------


class TestUpdateGitignoreForApmModules:
    """Tests for _update_gitignore_for_apm_modules."""

    def test_creates_gitignore_when_absent(self, tmp_path, monkeypatch):
        """Creates .gitignore with apm_modules/ when file doesn't exist."""
        monkeypatch.chdir(tmp_path)
        _update_gitignore_for_apm_modules()
        content = (tmp_path / ".gitignore").read_text()
        assert "apm_modules/" in content

    def test_skips_when_already_present(self, tmp_path, monkeypatch):
        """Does not modify .gitignore when apm_modules/ is already listed."""
        monkeypatch.chdir(tmp_path)
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("node_modules/\napm_modules/\n")
        mtime_before = gitignore.stat().st_mtime  # noqa: F841
        _update_gitignore_for_apm_modules()
        # File should not have been modified
        assert gitignore.read_text() == "node_modules/\napm_modules/\n"

    def test_appends_to_existing_gitignore(self, tmp_path, monkeypatch):
        """Appends apm_modules/ to an existing .gitignore that lacks it."""
        monkeypatch.chdir(tmp_path)
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("node_modules/\n*.pyc\n")
        _update_gitignore_for_apm_modules()
        content = gitignore.read_text()
        assert "apm_modules/" in content
        assert "node_modules/" in content  # existing entries preserved

    def test_adds_comment_header(self, tmp_path, monkeypatch):
        """Includes APM comment before the apm_modules/ entry."""
        monkeypatch.chdir(tmp_path)
        _update_gitignore_for_apm_modules()
        content = (tmp_path / ".gitignore").read_text()
        assert "# APM dependencies" in content

    def test_handles_read_error_gracefully(self, tmp_path, monkeypatch):
        """Does not raise when .gitignore cannot be read."""
        monkeypatch.chdir(tmp_path)
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("existing\n")
        with patch("builtins.open", side_effect=OSError("permission denied")):
            # Should not raise
            _update_gitignore_for_apm_modules()


# ---------------------------------------------------------------------------
# _load_apm_config / _get_default_script / _list_available_scripts
# ---------------------------------------------------------------------------


class TestLoadApmConfig:
    """Tests for _load_apm_config."""

    def test_returns_none_when_no_apm_yml(self, tmp_path, monkeypatch):
        """Returns None when apm.yml is absent."""
        monkeypatch.chdir(tmp_path)
        result = _load_apm_config()
        assert result is None

    def test_returns_parsed_config(self, tmp_path, monkeypatch):
        """Returns parsed dict when apm.yml exists."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: my-project\nversion: 1.0.0\n", encoding="utf-8")
        result = _load_apm_config()
        assert result == {"name": "my-project", "version": "1.0.0"}

    def test_returns_config_with_scripts(self, tmp_path, monkeypatch):
        """Config with scripts section is returned intact."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: proj\nscripts:\n  start: apm run\n  build: make\n",
            encoding="utf-8",
        )
        result = _load_apm_config()
        assert result["scripts"]["start"] == "apm run"


class TestGetDefaultScript:
    """Tests for _get_default_script."""

    def test_returns_none_when_no_apm_yml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _get_default_script() is None

    def test_returns_none_when_no_start_script(self, tmp_path, monkeypatch):
        """Returns None when scripts section has no 'start' key."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: p\nscripts:\n  build: make\n")
        assert _get_default_script() is None

    def test_returns_start_when_present(self, tmp_path, monkeypatch):
        """Returns 'start' string when start script is defined."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: p\nscripts:\n  start: apm compile\n")
        assert _get_default_script() == "start"


class TestListAvailableScripts:
    """Tests for _list_available_scripts."""

    def test_returns_empty_dict_when_no_apm_yml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _list_available_scripts() == {}

    def test_returns_empty_dict_when_no_scripts_key(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: proj\n")
        assert _list_available_scripts() == {}

    def test_returns_all_scripts(self, tmp_path, monkeypatch):
        """Returns the full scripts dict."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: p\nscripts:\n  start: run\n  test: pytest\n")
        scripts = _list_available_scripts()
        assert scripts == {"start": "run", "test": "pytest"}


# ---------------------------------------------------------------------------
# _scan_installed_packages
# ---------------------------------------------------------------------------


class TestScanInstalledPackages:
    """Tests for _scan_installed_packages."""

    def test_returns_empty_when_dir_absent(self, tmp_path):
        """Returns empty list when apm_modules directory doesn't exist."""
        result = _scan_installed_packages(tmp_path / "apm_modules")
        assert result == []

    def test_finds_github_style_2level_packages(self, tmp_path):
        """Detects packages at owner/repo (2-level) depth."""
        pkg = tmp_path / "owner" / "repo"
        pkg.mkdir(parents=True)
        (pkg / "apm.yml").write_text("name: repo")
        result = _scan_installed_packages(tmp_path)
        assert "owner/repo" in result

    def test_finds_ado_style_3level_packages(self, tmp_path):
        """Detects packages at org/project/repo (3-level) depth."""
        pkg = tmp_path / "org" / "project" / "repo"
        pkg.mkdir(parents=True)
        (pkg / ".apm").write_text("")
        result = _scan_installed_packages(tmp_path)
        found = [p for p in result if "org/project/repo" in p]
        assert len(found) >= 1

    def test_ignores_dot_named_directories(self, tmp_path):
        """Directories whose own name starts with '.' are skipped."""
        # A directory named '.hidden' at top-level is skipped by name check.
        dot_dir = tmp_path / ".hidden"
        dot_dir.mkdir()
        (dot_dir / "apm.yml").write_text("name: hidden")
        result = _scan_installed_packages(tmp_path)
        # rel_parts of ".hidden" has length 1, so it can't produce an owner/repo key
        assert not any(p == ".hidden" for p in result)

    def test_ignores_dirs_without_apm_marker(self, tmp_path):
        """Directories without apm.yml or .apm are not returned."""
        no_marker = tmp_path / "owner" / "plain"
        no_marker.mkdir(parents=True)
        (no_marker / "README.md").write_text("# no marker")
        result = _scan_installed_packages(tmp_path)
        assert result == []

    def test_returns_empty_for_empty_dir(self, tmp_path):
        """Empty apm_modules directory returns empty list."""
        (tmp_path / "apm_modules").mkdir()
        result = _scan_installed_packages(tmp_path / "apm_modules")
        assert result == []


# ---------------------------------------------------------------------------
# _check_and_notify_updates
# ---------------------------------------------------------------------------


class TestCheckAndNotifyUpdates:
    """Tests for _check_and_notify_updates."""

    def test_skips_when_self_update_disabled(self):
        """Returns immediately when distribution disables self-update."""
        with patch("apm_cli.commands._helpers.is_self_update_enabled", return_value=False):
            with patch("apm_cli.commands._helpers.check_for_updates") as mock_check:
                _check_and_notify_updates()
                mock_check.assert_not_called()

    def test_skips_in_e2e_test_mode(self):
        """Returns immediately when APM_E2E_TESTS=1 is set."""
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}):
            with patch("apm_cli.commands._helpers.check_for_updates") as mock_check:
                _check_and_notify_updates()
                mock_check.assert_not_called()

    def test_skips_for_unknown_version(self):
        """Returns immediately when current version is 'unknown' (dev)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_E2E_TESTS", None)
            with patch("apm_cli.commands._helpers.get_version", return_value="unknown"):
                with patch("apm_cli.commands._helpers.check_for_updates") as mock_check:
                    _check_and_notify_updates()
                    mock_check.assert_not_called()

    def test_no_output_when_up_to_date(self):
        """Does not warn when check_for_updates returns None."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_E2E_TESTS", None)
            with patch("apm_cli.commands._helpers.get_version", return_value="1.0.0"):
                with patch("apm_cli.commands._helpers.check_for_updates", return_value=None):
                    with patch("apm_cli.commands._helpers._rich_warning") as mock_warn:
                        _check_and_notify_updates()
                        mock_warn.assert_not_called()

    def test_warns_when_update_available(self):
        """Calls _rich_warning when a newer version is found."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_E2E_TESTS", None)
            with patch("apm_cli.commands._helpers.get_version", return_value="1.0.0"):
                with patch("apm_cli.commands._helpers.check_for_updates", return_value="1.1.0"):
                    with patch("apm_cli.commands._helpers._rich_warning") as mock_warn:
                        _check_and_notify_updates()
                        mock_warn.assert_called_once()
                        call_args = mock_warn.call_args[0][0]
                        assert "1.1.0" in call_args

    def test_silently_ignores_check_exception(self):
        """Does not raise when check_for_updates throws."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_E2E_TESTS", None)
            with patch("apm_cli.commands._helpers.get_version", return_value="1.0.0"):
                with patch(
                    "apm_cli.commands._helpers.check_for_updates",
                    side_effect=RuntimeError("network error"),
                ):
                    # Should not raise
                    _check_and_notify_updates()
