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
from apm_cli.install.mcp.writer import _diff_entry


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
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class TestNewEntry:
    def test_append_bare_string(self, tmp_apm_yml):
        status, diff = _add_mcp_to_apm_yml(
            "io.github.foo/bar",
            "io.github.foo/bar",
            manifest_path=tmp_apm_yml,
        )
        assert status == "added"
        assert diff is None
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"] == ["io.github.foo/bar"]

    def test_append_dict_entry(self, tmp_apm_yml):
        entry = {
            "name": "foo",
            "registry": False,
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "srv"],
        }
        status, _ = _add_mcp_to_apm_yml("foo", entry, manifest_path=tmp_apm_yml)
        assert status == "added"
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"][0]["name"] == "foo"

    def test_dev_routes_to_devdependencies(self, tmp_apm_yml):
        status, _ = _add_mcp_to_apm_yml(
            "foo",
            "foo",
            dev=True,
            manifest_path=tmp_apm_yml,
        )
        assert status == "added"
        data = _read(tmp_apm_yml)
        assert data["devDependencies"]["mcp"] == ["foo"]
        # Original section untouched.
        assert data["dependencies"]["mcp"] == []

    def test_no_apm_yml_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "apm.yml"
            with pytest.raises(click.UsageError, match="no apm.yml"):  # noqa: RUF043
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
        new_entry = {"name": "foo", "registry": False, "transport": "stdio", "command": "node"}
        status, diff = _add_mcp_to_apm_yml(
            "foo",
            new_entry,
            force=True,
            manifest_path=tmp_apm_yml,
        )
        assert status == "replaced"
        assert diff  # non-empty
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"][0]["command"] == "node"

    def test_non_tty_without_force_errors(self, tmp_apm_yml):
        self._seed(tmp_apm_yml, "foo")
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stdout.isatty", return_value=False),
        ):
            with pytest.raises(click.UsageError, match="--force to replace"):
                _add_mcp_to_apm_yml(
                    "foo",
                    {"name": "foo", "registry": False, "transport": "stdio", "command": "node"},
                    manifest_path=tmp_apm_yml,
                )

    def test_tty_prompt_accept_replaces(self, tmp_apm_yml):
        self._seed(tmp_apm_yml, "foo")
        new_entry = {"name": "foo", "registry": False, "transport": "stdio", "command": "node"}
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("click.confirm", return_value=True),
        ):
            status, _ = _add_mcp_to_apm_yml(
                "foo",
                new_entry,
                manifest_path=tmp_apm_yml,
            )
        assert status == "replaced"
        data = _read(tmp_apm_yml)
        assert data["dependencies"]["mcp"][0]["command"] == "node"

    def test_tty_prompt_decline_skips(self, tmp_apm_yml):
        self._seed(tmp_apm_yml, "foo")
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("click.confirm", return_value=False),
        ):
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
            "foo",
            "foo",
            manifest_path=tmp_apm_yml,
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


# ---------------------------------------------------------------------------
# _diff_entry unit tests (line 31 coverage: different-string path)
# ---------------------------------------------------------------------------


class TestDiffEntry:
    """Direct tests for the private _diff_entry helper."""

    def test_equal_strings_returns_empty(self):
        assert _diff_entry("foo", "foo") == []

    def test_different_strings_returns_arrow_line(self):
        """Line 31: both old and new are strings but differ."""
        result = _diff_entry("old-srv", "new-srv")
        assert len(result) == 1
        assert "old-srv" in result[0]
        assert "new-srv" in result[0]
        assert "->" in result[0]

    def test_old_str_new_dict_uses_dict_diff(self):
        """Old is a bare string; new is a dict with matching name but extra keys."""
        result = _diff_entry("foo", {"name": "foo", "transport": "stdio"})
        # transport key absent in old_d → diff should mention it
        assert any("transport" in line for line in result)

    def test_old_dict_new_str_uses_dict_diff(self):
        result = _diff_entry({"name": "foo", "transport": "sse"}, "foo")
        assert any("transport" in line for line in result)

    def test_both_none_returns_empty(self):
        assert _diff_entry(None, None) == []

    def test_old_none_new_dict_returns_diff(self):
        result = _diff_entry(None, {"name": "foo"})
        # name key absent in old ({}) but present in new
        assert any("name" in line for line in result)

    def test_same_dict_returns_empty(self):
        entry = {"name": "foo", "transport": "stdio"}
        assert _diff_entry(entry, entry) == []

    def test_dict_with_changed_key_returns_diff(self):
        old = {"name": "foo", "transport": "stdio"}
        new = {"name": "foo", "transport": "sse"}
        result = _diff_entry(old, new)
        assert len(result) == 1
        assert "transport" in result[0]
        assert "stdio" in result[0]
        assert "sse" in result[0]


class TestExistingEntryStringReplace:
    """String-to-different-string replacement path exercises _diff_entry line 31."""

    def _seed(self, path: Path, entry: str | dict = "old-srv") -> None:
        data = _read(path)
        data["dependencies"]["mcp"] = [entry]
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)

    def test_force_replace_string_to_string(self, tmp_apm_yml):
        """_diff_entry("old-srv", "new-srv") is called; line 31 runs."""
        self._seed(tmp_apm_yml, "old-srv")
        status, diff = _add_mcp_to_apm_yml(
            "old-srv",
            "new-srv",
            force=True,
            manifest_path=tmp_apm_yml,
        )
        # We're adding under the name "old-srv" but the entry value is "new-srv".
        # Actually name lookup matches "old-srv" == item (str) so existing_idx is found.
        # Since diff is non-empty (they differ) and force=True, status is "replaced".
        assert status == "replaced"
        assert diff is not None
        assert len(diff) == 1

    def test_tty_prompt_string_to_string_accepted(self, tmp_apm_yml):
        """string→string replacement under interactive TTY (line 31 path)."""
        self._seed(tmp_apm_yml, "old-srv")
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("click.confirm", return_value=True),
        ):
            status, diff = _add_mcp_to_apm_yml(
                "old-srv",
                "new-srv",
                manifest_path=tmp_apm_yml,
            )
        assert status == "replaced"
        assert diff is not None
