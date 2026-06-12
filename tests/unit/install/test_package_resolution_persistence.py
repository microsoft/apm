"""Tests for dependency-list persistence messaging."""

from __future__ import annotations

from unittest.mock import Mock, patch

from apm_cli.install.package_resolution import persist_dependency_list_if_changed


def test_persist_dependency_list_reports_generic_manifest_update():
    """Manifest rewrites should not claim every change is marketplace-specific."""
    logger = Mock()
    data = {"dependencies": {"apm": []}}
    current_deps = ["danielmeppiel/genesis#v0.4.0"]

    with patch("apm_cli.utils.yaml_io.dump_yaml") as dump_yaml:
        persist_dependency_list_if_changed(
            dependencies_changed=True,
            data=data,
            dep_section="dependencies",
            current_deps=current_deps,
            apm_yml_path="apm.yml",
            apm_yml_filename="apm.yml",
            logger=logger,
            rich_error=Mock(),
            sys_exit=Mock(),
        )

    dump_yaml.assert_called_once_with(data, "apm.yml")
    logger.success.assert_called_once_with("Updated apm.yml dependency entries")
