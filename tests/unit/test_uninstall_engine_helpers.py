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
    _build_children_index,
    _cleanup_stale_mcp,
    _dry_run_uninstall,
    _parse_dependency_entry,
    _remove_packages_from_disk,
    _validate_uninstall_packages,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
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
        to_remove, not_found = _validate_uninstall_packages(["org/missing"], ["org/other"], logger)
        assert to_remove == []
        assert "org/missing" in not_found
        logger.warning.assert_called_once()

    def test_invalid_format_no_slash_logs_error(self):
        """Package without slash is rejected with an error message and tracked in not_found."""
        logger = _make_logger()
        to_remove, not_found = _validate_uninstall_packages(["badpackage"], ["org/repo"], logger)
        assert to_remove == []
        assert "badpackage" in not_found
        logger.error.assert_called_once()

    def test_multiple_packages_partial_match(self):
        """Some packages matched, others not."""
        logger = _make_logger()
        deps = ["org/a", "org/b", "org/c"]
        to_remove, not_found = _validate_uninstall_packages(["org/a", "org/missing"], deps, logger)
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
            to_remove, not_found = _validate_uninstall_packages(["org/repo"], ["org/repo"], logger)
        assert "org/repo" in to_remove
        assert not_found == []
        logger.error.assert_not_called()

    def test_dependency_reference_objects_in_deps(self):
        """DependencyReference objects in deps list are matched correctly."""
        logger = _make_logger()
        ref = DependencyReference.parse("org/repo")
        to_remove, not_found = _validate_uninstall_packages(["org/repo"], [ref], logger)
        assert ref in to_remove
        assert not_found == []

    def test_windows_absolute_path_not_rejected_as_invalid_format(self):
        r"""Regression for v0.14.1 Windows release failure (PR #1413).

        Pre-fix, ``_validate_uninstall_packages`` rejected any package arg
        that did not contain a forward slash with "Invalid package format".
        Windows absolute paths like ``C:\Users\runner\AppData\...\my-pkg``
        have no ``/`` and were silently dropped, so the corresponding
        copilot-app DB row leaked across uninstall. Both Windows-style and
        relative ``.\`` paths must round-trip through the validator without
        triggering the marketplace-format error.
        """
        logger = _make_logger()
        win_path = r"C:\Users\runneradmin\AppData\Local\Temp\my-pkg"
        rel_win = r".\local-pkg"
        # Deps list is empty so we only assert the validator does NOT
        # mistake the path for a malformed marketplace ref.
        to_remove, not_found = _validate_uninstall_packages([win_path, rel_win], [], logger)
        assert to_remove == []
        # Both arguments are reported "not found in apm.yml" (warning) rather
        # than "Invalid package format" (error). The latter would leave the
        # copilot-app integration cleanup with nothing to clean.
        logger.error.assert_not_called()
        assert win_path in not_found
        assert rel_win in not_found


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

        with (
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=None,
            ),
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

        with (
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=None,
            ),
        ):
            _dry_run_uninstall(["org/repo"], modules, logger)

        # Package directory must still exist
        assert pkg_dir.exists()

    def test_success_message_emitted(self, tmp_path):
        """Success message is always emitted at the end of dry run."""
        logger = _make_logger()

        with (
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=None,
            ),
        ):
            _dry_run_uninstall(["org/repo"], tmp_path / "apm_modules", logger)

        logger.success.assert_called_once()
        assert "no changes" in logger.success.call_args[0][0].lower()

    def test_orphans_listed_when_lockfile_present(self, tmp_path):
        """Transitive orphans are mentioned when lockfile has dependents."""
        from apm_cli.deps.lockfile import LockedDependency
        from apm_cli.deps.lockfile import LockFile as _LF

        lockfile = _LF()
        orphan = LockedDependency(
            repo_url="org/transitive",
            resolved_by="org/repo",
            resolved_ref="main",
            resolved_commit="abc123",
        )
        lockfile.add_dependency(orphan)

        logger = _make_logger()

        with (
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=lockfile,
            ),
        ):
            _dry_run_uninstall(["org/repo"], tmp_path / "apm_modules", logger)

        # At least one progress call should mention the transitive dep
        all_progress_msgs = " ".join(call[0][0] for call in logger.progress.call_args_list)
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

        with patch("apm_cli.commands.uninstall.engine.MCPIntegrator") as mock_mcp:
            mock_mcp.collect_transitive.return_value = []
            mock_mcp.deduplicate.return_value = []
            mock_mcp.get_server_names.return_value = set()
            mock_mcp.remove_stale = MagicMock()
            mock_mcp.update_lockfile = MagicMock()

            _cleanup_stale_mcp(
                apm_package,
                lockfile,
                lockfile_path,
                old_servers,
                modules_dir=tmp_path / "apm_modules",
            )

        mock_mcp.remove_stale.assert_called_once_with(
            {"stale-server"},
            project_root=None,
            user_scope=False,
            scope=None,
        )
        mock_mcp.update_lockfile.assert_called_once()

    def test_non_stale_server_not_removed(self, tmp_path):
        """Servers still present in remaining set are not removed."""
        apm_package = MagicMock()
        apm_package.get_mcp_dependencies.return_value = []
        lockfile = MagicMock()
        lockfile_path = tmp_path / "apm.lock.yaml"
        old_servers = {"live-server"}

        with patch("apm_cli.commands.uninstall.engine.MCPIntegrator") as mock_mcp:
            mock_mcp.collect_transitive.return_value = []
            mock_mcp.deduplicate.return_value = []
            mock_mcp.get_server_names.return_value = {"live-server"}
            mock_mcp.remove_stale = MagicMock()
            mock_mcp.update_lockfile = MagicMock()

            _cleanup_stale_mcp(
                apm_package,
                lockfile,
                lockfile_path,
                old_servers,
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

        with patch("apm_cli.commands.uninstall.engine.MCPIntegrator") as mock_mcp:
            mock_mcp.collect_transitive.return_value = []
            mock_mcp.deduplicate.return_value = []
            mock_mcp.get_server_names.return_value = set()
            mock_mcp.remove_stale = MagicMock()
            mock_mcp.update_lockfile = MagicMock()

            _cleanup_stale_mcp(
                apm_package,
                lockfile,
                lockfile_path,
                old_servers,
                scope="user",
            )

        mock_mcp.remove_stale.assert_called_once_with(
            {"stale"},
            project_root=None,
            user_scope=False,
            scope="user",
        )

    def test_get_mcp_dependencies_exception_handled(self, tmp_path):
        """Exception from apm_package.get_mcp_dependencies is swallowed."""
        apm_package = MagicMock()
        apm_package.get_mcp_dependencies.side_effect = RuntimeError("boom")
        lockfile = MagicMock()
        lockfile_path = tmp_path / "apm.lock.yaml"
        old_servers = {"stale"}

        with patch("apm_cli.commands.uninstall.engine.MCPIntegrator") as mock_mcp:
            mock_mcp.collect_transitive.return_value = []
            mock_mcp.deduplicate.return_value = []
            mock_mcp.get_server_names.return_value = set()
            mock_mcp.remove_stale = MagicMock()
            mock_mcp.update_lockfile = MagicMock()

            # Should not raise
            _cleanup_stale_mcp(
                apm_package,
                lockfile,
                lockfile_path,
                old_servers,
                modules_dir=tmp_path / "apm_modules",
            )


# ===========================================================================
# _build_children_index
# ===========================================================================


class TestBuildChildrenIndex:
    """Tests for _build_children_index."""

    def test_basic_parent_child_mapping(self):
        """Index maps parent URLs to their child dependency objects."""
        lockfile = LockFile()
        dep_a = LockedDependency(repo_url="org/a", resolved_commit="aaa")
        dep_b = LockedDependency(
            repo_url="org/b",
            resolved_by="org/a",
            resolved_commit="bbb",
        )
        dep_c = LockedDependency(
            repo_url="org/c",
            resolved_by="org/b",
            resolved_commit="ccc",
        )
        lockfile.add_dependency(dep_a)
        lockfile.add_dependency(dep_b)
        lockfile.add_dependency(dep_c)

        index = _build_children_index(lockfile)

        assert "org/a" in index
        assert len(index["org/a"]) == 1
        assert index["org/a"][0].repo_url == "org/b"

        assert "org/b" in index
        assert len(index["org/b"]) == 1
        assert index["org/b"][0].repo_url == "org/c"

        # dep_a has no parent, dep_c has no children
        assert "org/c" not in index

    def test_empty_lockfile_returns_empty_dict(self):
        """Empty lockfile produces an empty index."""
        lockfile = LockFile()

        index = _build_children_index(lockfile)

        assert index == {}

    def test_deps_without_resolved_by_are_not_indexed(self):
        """Dependencies with no resolved_by field are excluded from index."""
        lockfile = LockFile()
        dep_a = LockedDependency(repo_url="org/a", resolved_commit="aaa")
        dep_b = LockedDependency(repo_url="org/b", resolved_commit="bbb")
        lockfile.add_dependency(dep_a)
        lockfile.add_dependency(dep_b)

        index = _build_children_index(lockfile)

        assert index == {}

    def test_multiple_children_same_parent(self):
        """Parent with multiple children collects all of them."""
        lockfile = LockFile()
        dep_root = LockedDependency(repo_url="org/root", resolved_commit="rrr")
        dep_x = LockedDependency(
            repo_url="org/x",
            resolved_by="org/root",
            resolved_commit="xxx",
        )
        dep_y = LockedDependency(
            repo_url="org/y",
            resolved_by="org/root",
            resolved_commit="yyy",
        )
        lockfile.add_dependency(dep_root)
        lockfile.add_dependency(dep_x)
        lockfile.add_dependency(dep_y)

        index = _build_children_index(lockfile)

        assert len(index["org/root"]) == 2
        child_urls = {d.repo_url for d in index["org/root"]}
        assert child_urls == {"org/x", "org/y"}


# ===========================================================================
# _resolve_marketplace_packages
# ===========================================================================


class TestResolveMarketplacePackages:
    """Tests for _resolve_marketplace_packages."""

    def test_lockfile_first_resolution(self):
        """Lockfile entry with matching provenance is used without a network call."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="acme/my-plugin",
            resolved_commit="abc123",
            discovered_via="official",
            marketplace_plugin_name="my-plugin",
        )
        lockfile.add_dependency(dep)
        logger = _make_logger()

        result = _resolve_marketplace_packages(["my-plugin@official"], lockfile, logger)

        assert result["my-plugin@official"] == "acme/my-plugin"
        logger.error.assert_not_called()

    def test_lockfile_first_ignores_wrong_marketplace(self):
        """Provenance mismatch emits a warning and still resolves via the lockfile entry."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="acme/my-plugin",
            resolved_commit="abc123",
            discovered_via="other-marketplace",
            marketplace_plugin_name="my-plugin",
        )
        lockfile.add_dependency(dep)
        logger = _make_logger()

        result = _resolve_marketplace_packages(["my-plugin@official"], lockfile, logger)

        # Provenance mismatch: warning emitted, registry NOT called, canonical still resolved
        logger.warning.assert_called_once()
        assert "installed via other-marketplace" in logger.warning.call_args[0][0]
        assert result["my-plugin@official"] == "acme/my-plugin"

    def test_registry_fallback_when_not_in_lockfile(self):
        """Registry canonical not present in the lockfile is refused by the supply-chain guard."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = LockFile()  # empty lockfile — registry result won't be in it
        logger = _make_logger()

        with patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin") as mock_resolve:
            mock_resolve.return_value = MagicMock(canonical="acme/resolved-plugin")
            result = _resolve_marketplace_packages(["resolved-plugin@official"], lockfile, logger)

        # Registry was called, but supply-chain guard refused the result
        mock_resolve.assert_called_once_with("resolved-plugin", "official", auth_resolver=None)
        assert result["resolved-plugin@official"] is None
        logger.error.assert_called_once()
        assert "could not be resolved" in logger.error.call_args[0][0]

    def test_no_lockfile_goes_directly_to_registry(self):
        """When lockfile is None, resolution proceeds directly to registry."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()

        with patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin") as mock_resolve:
            mock_resolve.return_value = MagicMock(canonical="acme/my-plugin")
            result = _resolve_marketplace_packages(["my-plugin@official"], None, logger)

        mock_resolve.assert_called_once_with("my-plugin", "official", auth_resolver=None)
        assert result["my-plugin@official"] == "acme/my-plugin"

    def test_network_error_logs_error_and_maps_to_none(self):
        """Registry failure logs a marketplace-specific error and returns None."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()

        with patch(
            "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
            side_effect=RuntimeError("network failure"),
        ):
            result = _resolve_marketplace_packages(["my-plugin@official"], None, logger)

        assert result["my-plugin@official"] is None
        logger.error.assert_called_once()
        assert "could not be resolved" in logger.error.call_args[0][0]

    def test_non_marketplace_refs_are_skipped(self):
        """Strings without @ are not included in the returned dict."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()
        result = _resolve_marketplace_packages(["org/repo"], None, logger)

        assert result == {}
        logger.error.assert_not_called()

    def test_batch_continues_after_single_failure(self):
        """A failing package does not prevent resolution of subsequent packages."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="acme/ok-plugin",
            resolved_commit="abc123",
            discovered_via="official",
            marketplace_plugin_name="ok-plugin",
        )
        lockfile.add_dependency(dep)
        logger = _make_logger()

        with patch(
            "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
            side_effect=RuntimeError("network failure"),
        ):
            result = _resolve_marketplace_packages(
                ["fail-plugin@official", "ok-plugin@official"], lockfile, logger
            )

        # fail-plugin has no lockfile entry and registry fails -> None
        assert result["fail-plugin@official"] is None
        # ok-plugin found in lockfile -> resolved without touching registry
        assert result["ok-plugin@official"] == "acme/ok-plugin"
        # Error logged once for the failing package only
        logger.error.assert_called_once()

    def test_dry_run_skips_registry(self):
        """When dry_run=True, Stage 2 registry call is skipped entirely."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()

        with patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin") as mock_resolve:
            result = _resolve_marketplace_packages(
                ["my-plugin@official"], None, logger, dry_run=True
            )

        mock_resolve.assert_not_called()
        assert result["my-plugin@official"] is None
        # Dry-run emits a warning (not error) since no operation was attempted.
        logger.warning.assert_called_once()
        warn_msg = logger.warning.call_args[0][0]
        assert "dry-run" in warn_msg.lower()
        logger.error.assert_not_called()

    def test_supply_chain_guard_refuses_canonical_not_in_lockfile(self):
        """Registry canonical absent from lockfile is refused; result is None."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = LockFile()  # empty -- registry result won't be present
        logger = _make_logger()

        with patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin") as mock_resolve:
            mock_resolve.return_value = MagicMock(canonical="acme/injected-plugin")
            result = _resolve_marketplace_packages(["injected-plugin@official"], lockfile, logger)

        assert result["injected-plugin@official"] is None
        # Warning (not verbose_detail) must surface the refusal at normal verbosity.
        logger.warning.assert_called_once()
        warn_msg = logger.warning.call_args[0][0]
        assert "acme/injected-plugin" in warn_msg
        assert "apm.lock.yaml" in warn_msg
        logger.error.assert_called_once()

    def test_provenance_mismatch_warns_and_resolves(self):
        """Lockfile entry via a different marketplace emits a warning but still resolves."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="acme/cross-plugin",
            resolved_commit="abc123",
            discovered_via="other-marketplace",
            marketplace_plugin_name="cross-plugin",
        )
        lockfile.add_dependency(dep)
        logger = _make_logger()

        result = _resolve_marketplace_packages(["cross-plugin@official"], lockfile, logger)

        logger.warning.assert_called_once()
        warn_msg = logger.warning.call_args[0][0]
        assert "installed via other-marketplace" in warn_msg
        assert result["cross-plugin@official"] == "acme/cross-plugin"
        logger.error.assert_not_called()


# ===========================================================================
# _validate_uninstall_packages -- marketplace ref extensions
# ===========================================================================


class TestValidateUninstallPackagesMarketplace:
    """Tests for marketplace-ref support in _validate_uninstall_packages."""

    def test_marketplace_ref_matched_via_lockfile(self):
        """Marketplace ref resolved via lockfile is matched against current deps."""
        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="acme/my-plugin",
            resolved_commit="abc123",
            discovered_via="official",
            marketplace_plugin_name="my-plugin",
        )
        lockfile.add_dependency(dep)
        logger = _make_logger()

        to_remove, not_found = _validate_uninstall_packages(
            ["my-plugin@official"], ["acme/my-plugin"], logger, lockfile
        )

        assert "acme/my-plugin" in to_remove
        assert not_found == []
        # Progress message must reference both the marketplace ref and canonical form
        progress_msg = logger.progress.call_args[0][0]
        assert "my-plugin@official" in progress_msg
        assert "acme/my-plugin" in progress_msg

    def test_marketplace_ref_resolved_but_not_in_deps(self):
        """Resolved marketplace ref not present in apm.yml uses marketplace warning."""
        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="acme/my-plugin",
            resolved_commit="abc123",
            discovered_via="official",
            marketplace_plugin_name="my-plugin",
        )
        lockfile.add_dependency(dep)
        logger = _make_logger()

        to_remove, not_found = _validate_uninstall_packages(
            ["my-plugin@official"], ["org/other"], logger, lockfile
        )

        assert to_remove == []
        assert "my-plugin@official" in not_found
        warning_msg = logger.warning.call_args[0][0]
        # Marketplace-specific not-found wording contains both ref and canonical
        assert "my-plugin@official" in warning_msg
        assert "acme/my-plugin" in warning_msg

    def test_marketplace_ref_resolution_fails_is_skipped(self):
        """When resolution returns None, the package is recorded as not_found."""
        logger = _make_logger()

        with patch(
            "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
            side_effect=RuntimeError("fail"),
        ):
            to_remove, not_found = _validate_uninstall_packages(
                ["my-plugin@official"], ["org/repo"], logger
            )

        assert to_remove == []
        # Unresolvable marketplace refs MUST appear in packages_not_found so callers
        # report accurate "N package(s) not found" counts (API contract).
        assert "my-plugin@official" in not_found
        logger.error.assert_called_once()

    def test_canonical_ref_behaviour_unchanged(self):
        """Canonical 'owner/repo' refs are still matched exactly as before."""
        logger = _make_logger()

        to_remove, not_found = _validate_uninstall_packages(["org/repo"], ["org/repo"], logger)

        assert "org/repo" in to_remove
        assert not_found == []
        # Progress message must NOT show a parenthesised canonical
        progress_msg = logger.progress.call_args[0][0]
        assert "(as " not in progress_msg

    def test_invalid_format_no_slash_no_at_still_errors(self):
        """A bare word with neither slash nor @ still triggers an error."""
        logger = _make_logger()

        to_remove, not_found = _validate_uninstall_packages(["badpackage"], ["org/repo"], logger)

        assert to_remove == []
        # Invalid-format inputs MUST appear in packages_not_found (API contract).
        assert "badpackage" in not_found
        logger.error.assert_called_once()
        err_msg = logger.error.call_args[0][0]
        assert "owner/repo" in err_msg
        # Error must surface marketplace notation symmetrically with install.
        assert "plugin-name@marketplace" in err_msg

    def test_mixed_canonical_and_marketplace_refs(self):
        """Batch mixing canonical and marketplace refs processes both correctly."""
        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="acme/mkt-plugin",
            resolved_commit="abc123",
            discovered_via="official",
            marketplace_plugin_name="mkt-plugin",
        )
        lockfile.add_dependency(dep)
        logger = _make_logger()

        to_remove, not_found = _validate_uninstall_packages(
            ["org/canonical", "mkt-plugin@official"],
            ["org/canonical", "acme/mkt-plugin"],
            logger,
            lockfile,
        )

        assert len(to_remove) == 2
        assert not_found == []

    def test_lockfile_none_falls_back_to_registry(self):
        """When lockfile is None, marketplace refs fall through to registry."""
        logger = _make_logger()

        with patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin") as mock_resolve:
            mock_resolve.return_value = MagicMock(canonical="acme/my-plugin")
            to_remove, _not_found = _validate_uninstall_packages(
                ["my-plugin@official"], ["acme/my-plugin"], logger, None
            )

        assert "acme/my-plugin" in to_remove
        mock_resolve.assert_called_once_with("my-plugin", "official", auth_resolver=None)
