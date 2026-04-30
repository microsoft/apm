"""Tests for package-type classification observability in install/sources.

Covers the label table that feeds ``CommandLogger.package_type_info``
so every classifiable ``PackageType`` has a human-readable label.

Regression suite for microsoft/apm#780.
"""

from __future__ import annotations

from apm_cli.install.sources import _format_package_type_label
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
