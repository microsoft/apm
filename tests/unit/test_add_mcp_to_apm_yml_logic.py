"""Tests for the ``_add_mcp_to_apm_yml`` writer (pure logic cases)."""

import pytest

from apm_cli.install.mcp.writer import _diff_entry

pytestmark = pytest.mark.unit


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
