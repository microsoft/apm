"""tests for apm_cli.bundle.plugin_exporter (pure logic cases)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# _sanitize_bundle_name
# ---------------------------------------------------------------------------


class TestSanitizeBundleName:
    def test_slashes_replaced(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("org/repo")
        assert "/" not in result
        assert result == "org-repo"

    def test_backslash_replaced(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("org\\repo")
        assert result == "org-repo"

    def test_dotdot_replaced(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("../evil")
        # Any dots-only segments become safe
        assert ".." not in result

    def test_empty_after_strip_returns_unnamed(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("---")
        # Strips all hyphens -> empty -> unnamed
        assert result == "unnamed"

    def test_normal_name_unchanged(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("my-plugin")
        assert result == "my-plugin"


# ---------------------------------------------------------------------------
# _collect_hooks_from_root
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _collect_mcp
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _get_dev_dependency_urls
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _find_or_synthesize_plugin_json
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _update_plugin_json_paths
# ---------------------------------------------------------------------------


class TestUpdatePluginJsonPaths:
    def test_strips_schema_invalid_keys_no_logger(self):
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json = {
            "name": "test",
            "agents": ["agents/test.md"],
            "skills": ["skills/test/SKILL.md"],
        }
        with patch("apm_cli.bundle.plugin_exporter._rich_warning") as mock_warn:
            result = _update_plugin_json_paths(plugin_json, [], logger=None)
        assert "agents" not in result
        assert "skills" not in result
        mock_warn.assert_called_once()

    def test_strips_schema_invalid_keys_with_logger(self):
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json = {
            "name": "test",
            "commands": ["commands/foo.md"],
            "instructions": ["instructions/bar.md"],
        }
        mock_logger = MagicMock()
        result = _update_plugin_json_paths(plugin_json, [], logger=mock_logger)
        assert "commands" not in result
        mock_logger.warning.assert_called_once()

    def test_no_strip_when_no_matching_keys(self):
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json = {"name": "test", "version": "1.0.0"}
        with patch("apm_cli.bundle.plugin_exporter._rich_warning") as mock_warn:
            result = _update_plugin_json_paths(plugin_json, [], logger=None)
        assert result == plugin_json
        mock_warn.assert_not_called()


# ---------------------------------------------------------------------------
# _merge_file_map
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# export_plugin_bundle
# ---------------------------------------------------------------------------
