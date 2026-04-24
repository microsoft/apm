"""Tests for the ``_add_mcp_to_apm_yml`` writer.

Covers idempotency policy (W3 R3): replace under --force, prompt under
TTY, error in non-TTY without --force, and the dev/dependencies routing.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import click
import pytest
import yaml

from apm_cli.commands.install import _add_mcp_to_apm_yml


@pytest.fixture
def tmp_apm_yml():
    """Create a tmp dir with a minimal apm.yml and chdir into it."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            data = {
                "name": "demo",
                "version": "0.1.0",
                "description": "x",
                "author": "x",
                "dependencies": {"apm": [], "mcp": []},
                "scripts": {},
            }
            path = Path(tmp) / "apm.yml"
            with open(path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, sort_keys=False)
            yield path
        finally:
            os.chdir(cwd)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class TestNewEntry:
    def test_append_bare_string(self, tmp_apm_yml):
        status, diff = _add_mcp_to_apm_yml(
            "io.github.foo/bar", "io.github.foo/bar",
            manifest_path=tmp_apm_yml,
        )
        assert status == "added"
        assert diff is None
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"] == ["io.github.foo/bar"]

    def test_append_dict_entry(self, tmp_apm_yml):
        entry = {"name": "foo", "registry": False, "transport": "stdio",
                 "command": "npx", "args": ["-y", "srv"]}
        status, _ = _add_mcp_to_apm_yml("foo", entry, manifest_path=tmp_apm_yml)
        assert status == "added"
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"][0]["name"] == "foo"

    def test_dev_routes_to_devdependencies(self, tmp_apm_yml):
        status, _ = _add_mcp_to_apm_yml(
            "foo", "foo", dev=True, manifest_path=tmp_apm_yml,
        )
        assert status == "added"
        data = _read(tmp_apm_yml)
        assert data["devDependencies"]["mcp"] == ["foo"]
        # Original section untouched.
        assert data["dependencies"]["mcp"] == []

    def test_no_apm_yml_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "apm.yml"
            with pytest.raises(click.UsageError, match="no apm.yml"):
                _add_mcp_to_apm_yml("foo", "foo", manifest_path=missing)

    def test_multiple_sequential_adds_preserve_order(self, tmp_apm_yml):
        _add_mcp_to_apm_yml("a", "a", manifest_path=tmp_apm_yml)
        _add_mcp_to_apm_yml("b", "b", manifest_path=tmp_apm_yml)
        _add_mcp_to_apm_yml("c", "c", manifest_path=tmp_apm_yml)
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"] == ["a", "b", "c"]


class TestExistingEntry:
    def _seed(self, path, entry="foo"):
        data = _read(path)
        data["dependencies"]["mcp"] = [entry]
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)

    def test_force_replaces_silently(self, tmp_apm_yml):
        self._seed(tmp_apm_yml, "foo")  # bare string
        new_entry = {"name": "foo", "registry": False,
                     "transport": "stdio", "command": "node"}
        status, diff = _add_mcp_to_apm_yml(
            "foo", new_entry, force=True, manifest_path=tmp_apm_yml,
        )
        assert status == "replaced"
        assert diff  # non-empty
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"][0]["command"] == "node"

    def test_non_tty_without_force_errors(self, tmp_apm_yml):
        self._seed(tmp_apm_yml, "foo")
        with patch("sys.stdin.isatty", return_value=False), \
             patch("sys.stdout.isatty", return_value=False):
            with pytest.raises(click.UsageError, match="--force to replace"):
                _add_mcp_to_apm_yml(
                    "foo", {"name": "foo", "registry": False,
                            "transport": "stdio", "command": "node"},
                    manifest_path=tmp_apm_yml,
                )

    def test_tty_prompt_accept_replaces(self, tmp_apm_yml):
        self._seed(tmp_apm_yml, "foo")
        new_entry = {"name": "foo", "registry": False,
                     "transport": "stdio", "command": "node"}
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdout.isatty", return_value=True), \
             patch("click.confirm", return_value=True):
            status, _ = _add_mcp_to_apm_yml(
                "foo", new_entry, manifest_path=tmp_apm_yml,
            )
        assert status == "replaced"
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"][0]["command"] == "node"

    def test_tty_prompt_decline_skips(self, tmp_apm_yml):
        self._seed(tmp_apm_yml, "foo")
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdout.isatty", return_value=True), \
             patch("click.confirm", return_value=False):
            status, _ = _add_mcp_to_apm_yml(
                "foo",
                {"name": "foo", "registry": False, "transport": "stdio", "command": "node"},
                manifest_path=tmp_apm_yml,
            )
        assert status == "skipped"
        data = _read(tmp_apm_yml)
        # Unchanged
        assert data["dependencies"]["mcp"][0] == "foo"

    def test_identical_entry_is_skipped(self, tmp_apm_yml):
        self._seed(tmp_apm_yml, "foo")
        status, diff = _add_mcp_to_apm_yml(
            "foo", "foo", manifest_path=tmp_apm_yml,
        )
        assert status == "skipped"
        assert diff == []


class TestStructuralRobustness:
    def test_creates_dependencies_section_if_missing(self, tmp_apm_yml):
        # Strip dependencies to simulate older minimal manifests.
        data = {"name": "x", "version": "0", "description": "", "author": ""}
        with open(tmp_apm_yml, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
        _add_mcp_to_apm_yml("foo", "foo", manifest_path=tmp_apm_yml)
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"] == ["foo"]

    def test_creates_mcp_list_when_section_lacks_it(self, tmp_apm_yml):
        data = _read(tmp_apm_yml)
        data["dependencies"] = {"apm": []}
        with open(tmp_apm_yml, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
        _add_mcp_to_apm_yml("foo", "foo", manifest_path=tmp_apm_yml)
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"] == ["foo"]

    def test_rejects_when_mcp_is_not_a_list(self, tmp_apm_yml):
        data = _read(tmp_apm_yml)
        data["dependencies"]["mcp"] = "not a list"
        with open(tmp_apm_yml, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
        with pytest.raises(click.UsageError, match="must be a list"):
            _add_mcp_to_apm_yml("foo", "foo", manifest_path=tmp_apm_yml)
