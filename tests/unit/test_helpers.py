"""Tests for helper utility functions."""

import json
import sys
import unittest
from pathlib import Path

import pytest

from apm_cli.utils.helpers import (
    find_plugin_json,
    get_available_package_managers,
    is_tool_available,
)

pytestmark = pytest.mark.component


class TestFindPluginJson(unittest.TestCase):
    """Test cases for find_plugin_json deterministic location check."""

    def test_finds_root_plugin_json(self, tmp_path=None):
        """Root plugin.json is returned when present."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pj = root / "plugin.json"
            pj.write_text(json.dumps({"name": "test"}))
            assert find_plugin_json(root) == pj

    def test_finds_github_plugin_json(self):
        """plugin.json under .github/plugin/ is found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".github" / "plugin" / "plugin.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"name": "gh"}))
            assert find_plugin_json(root) == target

    def test_finds_claude_plugin_json(self):
        """plugin.json under .claude-plugin/ is found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".claude-plugin" / "plugin.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"name": "claude"}))
            assert find_plugin_json(root) == target

    def test_finds_cursor_plugin_json(self):
        """plugin.json under .cursor-plugin/ is found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".cursor-plugin" / "plugin.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"name": "cursor"}))
            assert find_plugin_json(root) == target

    def test_priority_order(self):
        """Root wins over .github/plugin/ which wins over .claude-plugin/ which wins over .cursor-plugin/."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for sub in [
                "plugin.json",
                ".github/plugin/plugin.json",
                ".claude-plugin/plugin.json",
                ".cursor-plugin/plugin.json",
            ]:
                p = root / sub
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"name": sub}))
            assert find_plugin_json(root) == root / "plugin.json"

    def test_cursor_plugin_found_when_only_option(self):
        """When only .cursor-plugin/ has plugin.json, it is found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".cursor-plugin" / "plugin.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"name": "cursor-only"}))
            # No root, .github, or .claude-plugin plugin.json
            assert find_plugin_json(root) == target

    def test_ignores_unrelated_locations(self):
        """plugin.json buried in node_modules or other dirs is NOT found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            hidden = root / "node_modules" / "evil" / "plugin.json"
            hidden.parent.mkdir(parents=True)
            hidden.write_text(json.dumps({"name": "evil"}))
            assert find_plugin_json(root) is None

    def test_returns_none_when_absent(self):
        """None is returned when no plugin.json exists anywhere."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            assert find_plugin_json(Path(d)) is None


if __name__ == "__main__":
    unittest.main()


# ── Extended coverage tests ──────────────────────────────────────────


class TestIsToolAvailableEdgeCases:
    """Tests covering Windows path and exception fallback in is_tool_available."""

    def test_exception_in_subprocess_returns_false(self, monkeypatch):
        """subprocess.run raising an exception returns False."""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        import subprocess

        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("no subprocess"))
        )
        assert is_tool_available("no-such-tool-xyz") is False

    def test_windows_path_returns_true_on_zero_returncode(self, monkeypatch):
        """Simulate win32 with subprocess returning rc=0."""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "platform", "win32")

        mock_result = type("R", (), {"returncode": 0})()
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        assert is_tool_available("some-tool") is True

    def test_windows_path_returns_false_on_nonzero_returncode(self, monkeypatch):
        """Simulate win32 with subprocess returning rc=1."""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "platform", "win32")

        mock_result = type("R", (), {"returncode": 1})()
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        assert is_tool_available("nonexistent") is False


class TestGetAvailablePackageManagersBranches:
    """Tests covering specific package manager availability paths."""

    def test_pipx_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            if name == "pipx":
                return "/usr/bin/pipx"
            return None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "pipx" in managers

    def test_apt_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/usr/bin/apt" if name == "apt" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "apt" in managers

    def test_yum_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/usr/bin/yum" if name == "yum" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "yum" in managers

    def test_dnf_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/usr/bin/dnf" if name == "dnf" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "dnf" in managers

    def test_apk_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/sbin/apk" if name == "apk" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "apk" in managers

    def test_pacman_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/usr/bin/pacman" if name == "pacman" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "pacman" in managers

    def test_no_tools_returns_empty(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        import subprocess

        mock_result = type("R", (), {"returncode": 1})()
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        managers = get_available_package_managers()
        assert managers == {}


# ── core/operations.py coverage ──────────────────────────────────────
