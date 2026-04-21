"""Unit tests for ``apm_cli.commands.uninstall.engine`` helper functions.

Covers the pure/mostly-pure engine helpers that are not tested directly
in existing integration-style uninstall tests:
- _parse_dependency_entry
- _validate_uninstall_packages
- _dry_run_uninstall
- _remove_packages_from_disk
- _cleanup_stale_mcp
"""

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.uninstall.engine import (
    _cleanup_stale_mcp,
    _dry_run_uninstall,
    _parse_dependency_entry,
    _remove_packages_from_disk,
    _validate_uninstall_packages,
)
from apm_cli.models.dependency.reference import DependencyReference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    """Return a minimal mock logger."""
    logger = MagicMock()
    logger.error = MagicMock()
    logger.warning = MagicMock()
    logger.progress = MagicMock()
    logger.success = MagicMock()
    logger.verbose_detail = MagicMock()
    return logger


# ===========================================================================
# _parse_dependency_entry
# ===========================================================================


class TestParseDependencyEntry:
    """Tests for _parse_dependency_entry."""

    def test_passes_through_dependency_reference(self):
        """DependencyReference instances are returned as-is."""
        ref = DependencyReference.parse("org/repo")
        result = _parse_dependency_entry(ref)
        assert result is ref

    def test_parses_string_shorthand(self):
        """Plain 'org/repo' strings are parsed to DependencyReference."""
        result = _parse_dependency_entry("org/repo")
        assert isinstance(result, DependencyReference)
        assert result.repo_url == "org/repo"

    def test_parses_dict_form(self):
        """Dict-form dependency entries are parsed correctly."""
        result = _parse_dependency_entry({"git": "https://github.com/org/repo"})
        assert isinstance(result, DependencyReference)

    def test_raises_for_unsupported_type(self):
        """Unsupported types raise ValueError."""
        with pytest.raises(ValueError, match="Unsupported dependency entry type"):
            _parse_dependency_entry(42)

    def test_raises_for_list_type(self):
        """List type raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported dependency entry type"):
            _parse_dependency_entry(["org/repo"])


# ===========================================================================
# _validate_uninstall_packages
# ===========================================================================


class TestValidateUninstallPackages:
    """Tests for _validate_uninstall_packages."""

    def test_matches_simple_shorthand(self):
        """Simple 'org/repo' package matched against deps list."""
        logger = _make_logger()
        deps = ["org/repo"]
        to_remove, not_found = _validate_uninstall_packages(["org/repo"], deps, logger)
        assert "org/repo" in to_remove
        assert not_found == []
        logger.error.assert_not_called()

    def test_missing_package_goes_to_not_found(self):
        """Package not in deps ends up in not_found list."""
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(
            ["org/missing"], ["org/other"], logger
        )
        assert to_remove == []
        assert "org/missing" in not_found
        logger.warning.assert_called_once()

    def test_invalid_format_no_slash_logs_error(self):
        """Package without slash is rejected with an error message."""
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(
            ["badpackage"], ["org/repo"], logger
        )
        assert to_remove == []
        assert not_found == []
        logger.error.assert_called_once()

    def test_multiple_packages_partial_match(self):
        """Some packages matched, others not."""
        logger = _make_logger()
        deps = ["org/a", "org/b", "org/c"]
        to_remove, not_found = _validate_uninstall_packages(
            ["org/a", "org/missing"], deps, logger
        )
        assert "org/a" in to_remove
        assert len(to_remove) == 1
        assert "org/missing" in not_found

    def test_empty_packages_list(self):
        """Empty input returns empty lists."""
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages([], ["org/repo"], logger)
        assert to_remove == []
        assert not_found == []

    def test_malformed_dep_entry_falls_back_to_string_compare(self):
        """A dep entry that raises on parse falls back to string comparison."""
        logger = _make_logger()
        # Force _parse_dependency_entry to raise so the engine takes the
        # except (ValueError, TypeError, AttributeError, KeyError) branch
        # and falls back to direct string comparison against the entry.
        with patch(
            "apm_cli.commands.uninstall.engine._parse_dependency_entry",
            side_effect=ValueError("parse failed"),
        ):
            to_remove, not_found = _validate_uninstall_packages(
                ["org/repo"], ["org/repo"], logger
            )
        assert "org/repo" in to_remove
        assert not_found == []
        logger.error.assert_not_called()

    def test_dependency_reference_objects_in_deps(self):
        """DependencyReference objects in deps list are matched correctly."""
        logger = _make_logger()
        ref = DependencyReference.parse("org/repo")
        to_remove, not_found = _validate_uninstall_packages(
            ["org/repo"], [ref], logger
        )
        assert ref in to_remove
        assert not_found == []


# ===========================================================================
# _remove_packages_from_disk
# ===========================================================================


class TestRemovePackagesFromDisk:
    """Tests for _remove_packages_from_disk."""

    def test_removes_existing_package(self, tmp_path):
        """Existing package directory is removed and count returned."""
        modules = tmp_path / "apm_modules"
        pkg_dir = modules / "org" / "repo"
        pkg_dir.mkdir(parents=True)
        logger = _make_logger()

        removed = _remove_packages_from_disk(["org/repo"], modules, logger)
        assert removed == 1
        assert not pkg_dir.exists()

    def test_missing_package_logs_warning(self, tmp_path):
        """Warning is logged when package directory does not exist."""
        modules = tmp_path / "apm_modules"
        modules.mkdir()
        logger = _make_logger()

        removed = _remove_packages_from_disk(["org/repo"], modules, logger)
        assert removed == 0
        logger.warning.assert_called_once()

    def test_no_modules_dir_returns_zero(self, tmp_path):
        """Returns 0 without error when apm_modules/ does not exist."""
        modules = tmp_path / "apm_modules"
        logger = _make_logger()

        removed = _remove_packages_from_disk(["org/repo"], modules, logger)
        assert removed == 0

    def test_removes_multiple_packages(self, tmp_path):
        """Multiple packages can be removed in a single call."""
        modules = tmp_path / "apm_modules"
        for slug in ["org/a", "org/b"]:
            (modules / slug.split("/")[0] / slug.split("/")[1]).mkdir(parents=True)
        logger = _make_logger()

        removed = _remove_packages_from_disk(["org/a", "org/b"], modules, logger)
        assert removed == 2

    def test_path_traversal_is_rejected(self, tmp_path):
        """PathTraversalError during dep resolution is caught and logged."""
        from apm_cli.utils.path_security import PathTraversalError

        modules = tmp_path / "apm_modules"
        modules.mkdir()
        logger = _make_logger()

        # Inject a dep entry whose get_install_path raises PathTraversalError
        bad_ref = MagicMock()
        bad_ref.get_install_path.side_effect = PathTraversalError("traversal")

        with patch(
            "apm_cli.commands.uninstall.engine._parse_dependency_entry",
            return_value=bad_ref,
        ):
            removed = _remove_packages_from_disk(["../evil"], modules, logger)

        assert removed == 0
        logger.error.assert_called_once()

    def test_rmtree_exception_is_caught(self, tmp_path):
        """Exception during safe_rmtree is logged without crashing."""
        modules = tmp_path / "apm_modules"
        pkg_dir = modules / "org" / "repo"
        pkg_dir.mkdir(parents=True)
        logger = _make_logger()

        with patch(
            "apm_cli.commands.uninstall.engine.safe_rmtree",
            side_effect=OSError("permission denied"),
        ):
            removed = _remove_packages_from_disk(["org/repo"], modules, logger)

        assert removed == 0
        logger.error.assert_called_once()


# ===========================================================================
# _dry_run_uninstall
# ===========================================================================


class TestDryRunUninstall:
    """Tests for _dry_run_uninstall."""

    def test_logs_package_count(self, tmp_path):
        """Dry run logs number of packages that would be removed."""
        logger = _make_logger()

        with patch(
            "apm_cli.deps.lockfile.get_lockfile_path",
            return_value=tmp_path / "apm.lock.yaml",
        ), patch(
            "apm_cli.deps.lockfile.LockFile.read",
            return_value=None,
        ):
            _dry_run_uninstall(["org/repo"], tmp_path / "apm_modules", logger)

        logger.progress.assert_called()
        first_call_args = logger.progress.call_args_list[0][0][0]
        assert "1" in first_call_args

    def test_dry_run_no_actual_changes(self, tmp_path):
        """Dry run does NOT create or delete anything on disk."""
        modules = tmp_path / "apm_modules"
        pkg_dir = modules / "org" / "repo"
        pkg_dir.mkdir(parents=True)
        logger = _make_logger()

        with patch(
            "apm_cli.deps.lockfile.get_lockfile_path",
            return_value=tmp_path / "apm.lock.yaml",
        ), patch(
            "apm_cli.deps.lockfile.LockFile.read",
            return_value=None,
        ):
            _dry_run_uninstall(["org/repo"], modules, logger)

        # Package directory must still exist
        assert pkg_dir.exists()

    def test_success_message_emitted(self, tmp_path):
        """Success message is always emitted at the end of dry run."""
        logger = _make_logger()

        with patch(
            "apm_cli.deps.lockfile.get_lockfile_path",
            return_value=tmp_path / "apm.lock.yaml",
        ), patch(
            "apm_cli.deps.lockfile.LockFile.read",
            return_value=None,
        ):
            _dry_run_uninstall(["org/repo"], tmp_path / "apm_modules", logger)

        logger.success.assert_called_once()
        assert "no changes" in logger.success.call_args[0][0].lower()

    def test_orphans_listed_when_lockfile_present(self, tmp_path):
        """Transitive orphans are mentioned when lockfile has dependents."""
        from apm_cli.deps.lockfile import LockFile as _LF, LockedDependency

        lockfile = _LF()
        orphan = LockedDependency(
            repo_url="org/transitive",
            resolved_by="org/repo",
            resolved_ref="main",
            resolved_commit="abc123",
        )
        lockfile.add_dependency(orphan)

        logger = _make_logger()

        with patch(
            "apm_cli.deps.lockfile.get_lockfile_path",
            return_value=tmp_path / "apm.lock.yaml",
        ), patch(
            "apm_cli.deps.lockfile.LockFile.read",
            return_value=lockfile,
        ):
            _dry_run_uninstall(["org/repo"], tmp_path / "apm_modules", logger)

        # At least one progress call should mention the transitive dep
        all_progress_msgs = " ".join(
            call[0][0] for call in logger.progress.call_args_list
        )
        assert "org/transitive" in all_progress_msgs


# ===========================================================================
# _cleanup_stale_mcp
# ===========================================================================


class TestCleanupStaleMcp:
    """Tests for _cleanup_stale_mcp."""

    def test_noop_when_no_old_servers(self, tmp_path):
        """Does nothing when old_mcp_servers is empty."""
        apm_package = MagicMock()
        lockfile = MagicMock()
        lockfile_path = tmp_path / "apm.lock.yaml"
        # Should not raise, no MCP methods called
        _cleanup_stale_mcp(apm_package, lockfile, lockfile_path, set())

    def test_stale_servers_removed(self, tmp_path):
        """Stale servers not in remaining set are removed."""
        apm_package = MagicMock()
        apm_package.get_mcp_dependencies.return_value = []
        lockfile = MagicMock()
        lockfile_path = tmp_path / "apm.lock.yaml"
        old_servers = {"stale-server"}

        with patch(
            "apm_cli.commands.uninstall.engine.MCPIntegrator"
        ) as mock_mcp:
            mock_mcp.collect_transitive.return_value = []
            mock_mcp.deduplicate.return_value = []
            mock_mcp.get_server_names.return_value = set()
            mock_mcp.remove_stale = MagicMock()
            mock_mcp.update_lockfile = MagicMock()

            _cleanup_stale_mcp(
                apm_package, lockfile, lockfile_path, old_servers,
                modules_dir=tmp_path / "apm_modules",
            )

        mock_mcp.remove_stale.assert_called_once_with({"stale-server"}, scope=None)
        mock_mcp.update_lockfile.assert_called_once()

    def test_non_stale_server_not_removed(self, tmp_path):
        """Servers still present in remaining set are not removed."""
        apm_package = MagicMock()
        apm_package.get_mcp_dependencies.return_value = []
        lockfile = MagicMock()
        lockfile_path = tmp_path / "apm.lock.yaml"
        old_servers = {"live-server"}

        with patch(
            "apm_cli.commands.uninstall.engine.MCPIntegrator"
        ) as mock_mcp:
            mock_mcp.collect_transitive.return_value = []
            mock_mcp.deduplicate.return_value = []
            mock_mcp.get_server_names.return_value = {"live-server"}
            mock_mcp.remove_stale = MagicMock()
            mock_mcp.update_lockfile = MagicMock()

            _cleanup_stale_mcp(
                apm_package, lockfile, lockfile_path, old_servers,
                modules_dir=tmp_path / "apm_modules",
            )

        mock_mcp.remove_stale.assert_not_called()

    def test_scope_passed_to_remove_stale(self, tmp_path):
        """scope parameter is forwarded to MCPIntegrator.remove_stale."""
        apm_package = MagicMock()
        apm_package.get_mcp_dependencies.return_value = []
        lockfile = MagicMock()
        lockfile_path = tmp_path / "apm.lock.yaml"
        old_servers = {"stale"}

        with patch(
            "apm_cli.commands.uninstall.engine.MCPIntegrator"
        ) as mock_mcp:
            mock_mcp.collect_transitive.return_value = []
            mock_mcp.deduplicate.return_value = []
            mock_mcp.get_server_names.return_value = set()
            mock_mcp.remove_stale = MagicMock()
            mock_mcp.update_lockfile = MagicMock()

            _cleanup_stale_mcp(
                apm_package, lockfile, lockfile_path, old_servers,
                scope="user",
            )

        mock_mcp.remove_stale.assert_called_once_with({"stale"}, scope="user")

    def test_get_mcp_dependencies_exception_handled(self, tmp_path):
        """Exception from apm_package.get_mcp_dependencies is swallowed."""
        apm_package = MagicMock()
        apm_package.get_mcp_dependencies.side_effect = RuntimeError("boom")
        lockfile = MagicMock()
        lockfile_path = tmp_path / "apm.lock.yaml"
        old_servers = {"stale"}

        with patch(
            "apm_cli.commands.uninstall.engine.MCPIntegrator"
        ) as mock_mcp:
            mock_mcp.collect_transitive.return_value = []
            mock_mcp.deduplicate.return_value = []
            mock_mcp.get_server_names.return_value = set()
            mock_mcp.remove_stale = MagicMock()
            mock_mcp.update_lockfile = MagicMock()

            # Should not raise
            _cleanup_stale_mcp(
                apm_package, lockfile, lockfile_path, old_servers,
                modules_dir=tmp_path / "apm_modules",
            )
