"""Integration tests for apm uninstall engine coverage."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.uninstall.engine import (
    _build_children_index,
    _is_marketplace_ref,
    _parse_dependency_entry,
)
from apm_cli.models.dependency.reference import DependencyReference


class TestIsMarketplaceRef:
    """Tests for _is_marketplace_ref helper."""

    def test_marketplace_ref_format(self):
        """Recognizes marketplace reference format."""
        with patch("apm_cli.marketplace.resolver.parse_marketplace_ref") as mock_parse:
            mock_parse.return_value = ("plugin", "marketplace", "ref")

            result = _is_marketplace_ref("plugin@marketplace")

            assert result is True

    def test_non_marketplace_ref(self):
        """Rejects non-marketplace reference."""
        with patch("apm_cli.marketplace.resolver.parse_marketplace_ref") as mock_parse:
            mock_parse.return_value = None

            result = _is_marketplace_ref("owner/repo")

            assert result is False

    def test_github_ref_not_marketplace(self):
        """GitHub refs are not marketplace refs."""
        with patch("apm_cli.marketplace.resolver.parse_marketplace_ref") as mock_parse:
            mock_parse.return_value = None

            result = _is_marketplace_ref("owner/repo-name")

            assert result is False


class TestParseDependencyEntry:
    """Tests for _parse_dependency_entry helper."""

    def test_parse_string_entry(self):
        """Parse string-format dependency entry."""
        with patch.object(DependencyReference, "parse") as mock_parse:
            mock_dep = MagicMock(spec=DependencyReference)
            mock_parse.return_value = mock_dep

            result = _parse_dependency_entry("owner/repo")

            assert result == mock_dep
            mock_parse.assert_called_once_with("owner/repo")

    def test_parse_dependency_reference_entry(self):
        """Parse DependencyReference object directly."""
        mock_dep = MagicMock(spec=DependencyReference)

        result = _parse_dependency_entry(mock_dep)

        assert result == mock_dep

    def test_parse_dict_entry(self):
        """Parse dictionary-format dependency entry."""
        with patch.object(DependencyReference, "parse_from_dict") as mock_parse:
            mock_dep = MagicMock(spec=DependencyReference)
            mock_parse.return_value = mock_dep

            entry_dict = {"url": "https://github.com/owner/repo"}
            result = _parse_dependency_entry(entry_dict)

            assert result == mock_dep
            mock_parse.assert_called_once_with(entry_dict)

    def test_parse_unsupported_type_raises(self):
        """Unsupported type raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            _parse_dependency_entry(123)

        assert "Unsupported dependency entry type" in str(exc_info.value)

    def test_parse_list_entry_raises(self):
        """List entry raises ValueError."""
        with pytest.raises(ValueError):
            _parse_dependency_entry(["owner/repo"])


class TestBuildChildrenIndex:
    """Tests for _build_children_index helper."""

    def test_build_empty_lockfile(self):
        """Empty lockfile produces empty index."""
        mock_lockfile = MagicMock()
        mock_lockfile.get_package_dependencies.return_value = []

        result = _build_children_index(mock_lockfile)

        assert result == {}

    def test_build_with_transitive_deps(self):
        """Build index with transitive dependencies."""
        parent_url = "https://github.com/owner/parent"

        mock_dep1 = MagicMock()
        mock_dep1.resolved_by = parent_url

        mock_dep2 = MagicMock()
        mock_dep2.resolved_by = parent_url

        mock_lockfile = MagicMock()
        mock_lockfile.get_package_dependencies.return_value = [mock_dep1, mock_dep2]

        result = _build_children_index(mock_lockfile)

        assert parent_url in result
        assert len(result[parent_url]) == 2
        assert mock_dep1 in result[parent_url]
        assert mock_dep2 in result[parent_url]

    def test_build_with_multiple_parents(self):
        """Build index with multiple parent packages."""
        parent1_url = "https://github.com/owner/parent1"
        parent2_url = "https://github.com/owner/parent2"

        mock_dep1 = MagicMock()
        mock_dep1.resolved_by = parent1_url

        mock_dep2 = MagicMock()
        mock_dep2.resolved_by = parent2_url

        mock_lockfile = MagicMock()
        mock_lockfile.get_package_dependencies.return_value = [mock_dep1, mock_dep2]

        result = _build_children_index(mock_lockfile)

        assert parent1_url in result
        assert parent2_url in result
        assert mock_dep1 in result[parent1_url]
        assert mock_dep2 in result[parent2_url]

    def test_build_with_orphaned_deps(self):
        """Deps with no parent (resolved_by=None) are ignored."""
        orphan_dep = MagicMock()
        orphan_dep.resolved_by = None

        mock_lockfile = MagicMock()
        mock_lockfile.get_package_dependencies.return_value = [orphan_dep]

        result = _build_children_index(mock_lockfile)

        assert result == {}

    def test_build_single_child_per_parent(self):
        """Build index with single child per parent."""
        parent_url = "https://github.com/owner/parent"

        mock_dep = MagicMock()
        mock_dep.resolved_by = parent_url

        mock_lockfile = MagicMock()
        mock_lockfile.get_package_dependencies.return_value = [mock_dep]

        result = _build_children_index(mock_lockfile)

        assert parent_url in result
        assert len(result[parent_url]) == 1
        assert result[parent_url][0] == mock_dep


class TestUninstallEngine:
    """Tests for uninstall engine structures."""

    def test_uninstall_engine_module_imports(self):
        """Uninstall engine imports required modules."""
        from apm_cli.commands.uninstall import engine

        assert hasattr(engine, "_is_marketplace_ref")
        assert hasattr(engine, "_build_children_index")
        assert hasattr(engine, "_parse_dependency_entry")

    def test_mcp_integrator_imported(self):
        """MCPIntegrator is imported in engine."""
        from apm_cli.commands.uninstall.engine import MCPIntegrator

        assert MCPIntegrator is not None

    def test_lockfile_imported(self):
        """LockFile is imported in engine."""
        from apm_cli.commands.uninstall.engine import LockFile

        assert LockFile is not None


class TestResolveMarketplacePackages:
    """Tests for marketplace package resolution."""

    def test_resolve_marketplace_ref_format(self):
        """Recognize marketplace reference format."""
        ref = "fetch@mcp#npm-package"

        with patch("apm_cli.marketplace.resolver.parse_marketplace_ref") as mock_parse:
            mock_parse.return_value = ("fetch", "mcp", "npm-package")

            with patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin"):
                from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

                logger = MagicMock()
                result = _resolve_marketplace_packages(
                    [ref],
                    None,
                    logger,
                    dry_run=True,
                )

                # When dry_run=True, registry lookup is skipped
                assert ref in result

    def test_resolve_non_marketplace_refs_skipped(self):
        """Non-marketplace refs are skipped silently."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = MagicMock()
        result = _resolve_marketplace_packages(
            ["owner/repo"],
            None,
            logger,
            dry_run=True,
        )

        # Non-marketplace refs are not in result
        assert "owner/repo" not in result


class TestUninstallIntegration:
    """Integration tests for uninstall command."""

    def test_parse_and_build_workflow(self):
        """Integration of parsing and indexing."""
        # Create mock dep entries
        string_entry = "owner/repo"
        dict_entry = {"url": "https://github.com/owner/other"}

        # Parse string entry
        with patch.object(DependencyReference, "parse") as mock_parse:
            mock_dep = MagicMock(spec=DependencyReference)
            mock_parse.return_value = mock_dep

            result1 = _parse_dependency_entry(string_entry)
            assert result1 == mock_dep

        # Parse dict entry
        with patch.object(DependencyReference, "parse_from_dict") as mock_parse:
            mock_dep2 = MagicMock(spec=DependencyReference)
            mock_parse.return_value = mock_dep2

            result2 = _parse_dependency_entry(dict_entry)
            assert result2 == mock_dep2

    def test_marketplace_detection_workflow(self):
        """Marketplace detection workflow."""
        refs = [
            "plugin@marketplace",
            "owner/repo",
            "fetch@mcp",
        ]

        with patch("apm_cli.marketplace.resolver.parse_marketplace_ref") as mock_parse:

            def parse_side_effect(ref):
                if "@" in ref:
                    parts = ref.split("@")
                    return (parts[0], parts[1], None)
                return None

            mock_parse.side_effect = parse_side_effect

            results = [_is_marketplace_ref(ref) for ref in refs]

            assert results[0] is True  # plugin@marketplace
            assert results[1] is False  # owner/repo
            assert results[2] is True  # fetch@mcp
