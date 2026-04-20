"""Tests for package-type classification observability in install/sources.

Covers the helpers that surface detection results to users: the label
table that feeds ``CommandLogger.package_type_info`` and the near-miss
warning that fires when a Hook Package classification disagrees with
directory contents.

Regression suite for microsoft/apm#780.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.install.sources import (
    _format_package_type_label,
    _warn_if_classification_near_miss,
)
from apm_cli.models.apm_package import PackageType


class TestFormatPackageTypeLabel:
    def test_all_classifiable_types_have_labels(self):
        """Every classifiable PackageType must have a human label.

        Missing entries make classification silent (the bug class behind
        microsoft/apm#780).  ``INVALID`` is excluded -- it short-circuits
        installation upstream with a dedicated error path.
        """
        for pkg_type in PackageType:
            if pkg_type == PackageType.INVALID:
                continue
            assert _format_package_type_label(pkg_type) is not None, (
                f"{pkg_type.name} has no human-readable label"
            )

    def test_hook_package_label_includes_format_hint(self):
        label = _format_package_type_label(PackageType.HOOK_PACKAGE)
        assert "hooks/*.json" in label

    def test_marketplace_plugin_label_mentions_dirs(self):
        """Label must reflect that classification fires on plugin.json
        OR on agents/skills/commands directories alone."""
        label = _format_package_type_label(PackageType.MARKETPLACE_PLUGIN)
        assert "agents" in label and "skills" in label and "commands" in label


class TestWarnIfClassificationNearMiss:
    def test_no_warning_when_not_hook_package(self, tmp_path):
        logger = MagicMock()
        _warn_if_classification_near_miss(
            tmp_path, PackageType.MARKETPLACE_PLUGIN, logger
        )
        logger.package_type_warn.assert_not_called()

    def test_no_warning_when_logger_none(self, tmp_path):
        # Should not raise.
        _warn_if_classification_near_miss(
            tmp_path, PackageType.HOOK_PACKAGE, None
        )

    def test_no_warning_for_pure_hooks_only_package(self, tmp_path):
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text("{}")
        logger = MagicMock()
        _warn_if_classification_near_miss(
            tmp_path, PackageType.HOOK_PACKAGE, logger
        )
        logger.package_type_warn.assert_not_called()

    def test_warns_when_agents_dir_also_present(self, tmp_path):
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text("{}")
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "foo.md").write_text("agent")
        logger = MagicMock()
        _warn_if_classification_near_miss(
            tmp_path, PackageType.HOOK_PACKAGE, logger
        )
        logger.package_type_warn.assert_called_once()
        msg = logger.package_type_warn.call_args[0][0]
        assert "agents" in msg
        assert "Hook Package" in msg

    def test_warns_when_claude_plugin_dir_present_without_plugin_json(self, tmp_path):
        """A .claude-plugin/ folder without plugin.json suggests a
        misshapen Claude plugin -- exactly the silent-drop scenario."""
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text("{}")
        (tmp_path / ".claude-plugin").mkdir()
        logger = MagicMock()
        _warn_if_classification_near_miss(
            tmp_path, PackageType.HOOK_PACKAGE, logger
        )
        logger.package_type_warn.assert_called_once()
        msg = logger.package_type_warn.call_args[0][0]
        assert ".claude-plugin/" in msg

    def test_warns_lists_all_extras_in_canonical_order(self, tmp_path):
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text("{}")
        for d in ("agents", "skills", "commands"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "foo.md").write_text("x")
        logger = MagicMock()
        _warn_if_classification_near_miss(
            tmp_path, PackageType.HOOK_PACKAGE, logger
        )
        msg = logger.package_type_warn.call_args[0][0]
        # Canonical order from _PLUGIN_DIRS in validation.py.
        agents_idx = msg.index("agents")
        skills_idx = msg.index("skills")
        commands_idx = msg.index("commands")
        assert agents_idx < skills_idx < commands_idx
