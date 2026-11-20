"""Unit tests for ``apm_cli.commands.uninstall.engine`` helper functions (filesystem, process and CLI boundary cases)."""

import errno
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.uninstall.engine import (
    MCPUninstallCleanupError,
    _cleanup_stale_mcp,
    _cleanup_transitive_orphans,
    _dry_run_uninstall,
    _remove_packages_from_disk,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.path_security import PathTraversalError
from apm_cli.utils.path_security import safe_rmtree as real_safe_rmtree

pytestmark = pytest.mark.component

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


# ===========================================================================
# _validate_uninstall_packages
# ===========================================================================


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
        """Path traversal stops removal before the caller can release ownership."""
        from apm_cli.utils.path_security import PathTraversalError

        modules = tmp_path / "apm_modules"
        modules.mkdir()
        logger = _make_logger()

        # Inject a dep entry whose get_install_path raises PathTraversalError
        bad_ref = MagicMock()
        bad_ref.get_install_path.side_effect = PathTraversalError("traversal")

        with (
            patch(
                "apm_cli.commands.uninstall.engine._parse_dependency_entry",
                return_value=bad_ref,
            ),
            pytest.raises(PathTraversalError, match="traversal"),
        ):
            _remove_packages_from_disk(["../evil"], modules, logger)

        logger.error.assert_called_once()

    def test_deletion_containment_refusal_names_package(self, tmp_path: Path) -> None:
        """The deletion boundary must retain the operand in its diagnostic."""
        modules = tmp_path / "apm_modules"
        (modules / "org" / "repo").mkdir(parents=True)
        logger = _make_logger()
        with (
            patch(
                "apm_cli.commands.uninstall.engine.safe_rmtree",
                side_effect=PathTraversalError("containment refused"),
            ),
            pytest.raises(PathTraversalError, match="containment refused"),
        ):
            _remove_packages_from_disk(["org/repo"], modules, logger)
        logger.error.assert_called_once_with(
            "Refusing to remove org/repo from apm_modules/: containment refused"
        )

    def test_rmtree_exception_is_propagated(self, tmp_path):
        """A deletion error is logged and stops the caller's ownership release."""
        modules = tmp_path / "apm_modules"
        pkg_dir = modules / "org" / "repo"
        pkg_dir.mkdir(parents=True)
        logger = _make_logger()

        with (
            patch(
                "apm_cli.commands.uninstall.engine.safe_rmtree",
                side_effect=OSError("permission denied"),
            ),
            pytest.raises(OSError, match="permission denied"),
        ):
            _remove_packages_from_disk(["org/repo"], modules, logger)

        assert pkg_dir.exists()
        logger.error.assert_called_once()


@pytest.mark.parametrize("at_resolution", [True, False], ids=["resolve", "delete"])
def test_orphan_containment_refusal_has_no_fallback(tmp_path: Path, at_resolution: bool) -> None:
    """Containment refusals are not parse errors eligible for key-derived deletion."""
    modules = tmp_path / "apm_modules"
    orphan_path = modules / "org" / "orphan"
    orphan_path.mkdir(parents=True)
    payload = orphan_path / "payload"
    payload.write_bytes(b"retain ownership")
    logger = _make_logger()
    lockfile = MagicMock()
    reference = lockfile.get_dependency.return_value.to_dependency_ref.return_value
    reference.get_install_path.return_value = orphan_path
    if at_resolution:
        reference.get_install_path.side_effect = PathTraversalError("containment refused")
    with (
        patch(
            "apm_cli.commands.uninstall.engine._project_transitive_orphans",
            return_value=({"org/orphan"}, {}),
        ),
        patch(
            "apm_cli.commands.uninstall.engine.safe_rmtree",
            side_effect=PathTraversalError("containment refused"),
        ) as remove,
        pytest.raises(PathTraversalError, match="containment refused"),
    ):
        _cleanup_transitive_orphans(lockfile, ["org/parent"], modules, None, logger)
    assert payload.read_bytes() == b"retain ownership"
    assert remove.call_count == (0 if at_resolution else 1)
    logger.error.assert_called_once_with(
        "Refusing to remove transitive dep org/orphan: containment refused"
    )


@pytest.mark.parametrize("removal_kind", ["direct", "orphan"])
def test_partial_failure_cleans_deleted_package_parents_for_retry(
    tmp_path: Path, removal_kind: str
) -> None:
    """A later deletion failure must not strand an earlier package's owner directory."""
    modules = tmp_path / "apm_modules"
    first = modules / "org1" / "a"
    second = modules / "org2" / "b"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    logger = _make_logger()
    packages = ["org1/a", "org2/b"]
    lockfile = LockFile()
    for package in packages:
        lockfile.add_dependency(
            LockedDependency(repo_url=package, resolved_commit=f"{package}-commit")
        )

    def remove_with_second_failure(path: Path, root: Path) -> None:
        if path == second:
            raise PermissionError(errno.EACCES, "permission denied", path)
        real_safe_rmtree(path, root)

    def remove() -> None:
        if removal_kind == "direct":
            _remove_packages_from_disk(packages, modules, logger)
        else:
            _cleanup_transitive_orphans(lockfile, ["org/parent"], modules, None, logger)

    with (
        patch(
            "apm_cli.commands.uninstall.engine._project_transitive_orphans",
            return_value=(packages, {}),
        ),
        patch(
            "apm_cli.commands.uninstall.engine.safe_rmtree",
            side_effect=remove_with_second_failure,
        ),
        pytest.raises(PermissionError, match="permission denied"),
    ):
        remove()

    assert not (modules / "org1").exists()
    assert second.exists()

    with patch(
        "apm_cli.commands.uninstall.engine._project_transitive_orphans",
        return_value=(packages, {}),
    ):
        remove()

    assert modules.exists()
    assert not (modules / "org1").exists()
    assert not (modules / "org2").exists()


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
        # The preview decides like the real run, which needs installed packages.
        (tmp_path / "apm_modules").mkdir()

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
        lockfile.mcp_target_servers = {"claude": ["stale-server"]}
        lockfile._mcp_target_servers_present = True
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
            runtime="claude",
            project_root=None,
            user_scope=False,
            scope=None,
            fail_on_write_error=True,
            stored_configs=lockfile.mcp_configs,
            native_hashes={},
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
        lockfile.mcp_target_servers = {"claude": ["stale"]}
        lockfile._mcp_target_servers_present = True
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
            runtime="claude",
            project_root=None,
            user_scope=False,
            scope="user",
            fail_on_write_error=True,
            stored_configs=lockfile.mcp_configs,
            native_hashes={},
        )

    def test_target_ownership_limits_cleanup_to_owning_runtime(self, tmp_path):
        """A server name owned in one runtime must not be removed from all runtimes."""
        lockfile = LockFile(
            mcp_servers=["shared-name"],
            mcp_target_servers={"hermes": ["shared-name"]},
        )

        with patch("apm_cli.commands.uninstall.engine.MCPIntegrator") as mock_mcp:
            mock_mcp.get_server_names.return_value = set()
            _cleanup_stale_mcp(
                MagicMock(),
                lockfile,
                tmp_path / "apm.lock.yaml",
                {"shared-name"},
                modules_dir=tmp_path / "apm_modules",
                persist=False,
            )

        mock_mcp.remove_stale.assert_called_once_with(
            {"shared-name"},
            runtime="hermes",
            project_root=None,
            user_scope=False,
            scope=None,
            fail_on_write_error=True,
            stored_configs={},
            native_hashes={},
        )

    def test_partial_target_ownership_retains_unmapped_aggregate_server(self, tmp_path):
        """A partial runtime map must not discard legacy aggregate ownership."""
        lockfile = LockFile(
            mcp_servers=["mapped", "legacy-unmapped"],
            mcp_target_servers={"hermes": ["mapped"]},
        )

        with patch("apm_cli.commands.uninstall.engine.MCPIntegrator") as mock_mcp:
            mock_mcp.get_server_names.return_value = set()
            _cleanup_stale_mcp(
                MagicMock(),
                lockfile,
                tmp_path / "apm.lock.yaml",
                {"mapped", "legacy-unmapped"},
                modules_dir=tmp_path / "apm_modules",
                persist=False,
            )

        mock_mcp.remove_stale.assert_called_once_with(
            {"mapped"},
            runtime="hermes",
            project_root=None,
            user_scope=False,
            scope=None,
            fail_on_write_error=True,
            stored_configs={},
            native_hashes={},
        )
        assert lockfile.mcp_servers == ["legacy-unmapped"]
        assert lockfile.mcp_target_servers == {}

    @pytest.mark.windows_compat
    def test_cleanup_failure_retains_lock_ownership(self, tmp_path):
        """Native cleanup must succeed before in-memory ownership is dropped."""
        from apm_cli.install.errors import RequiredIntegrationError

        apm_package = MagicMock()
        lockfile = LockFile(
            mcp_servers=["stale"],
            mcp_target_servers={"hermes": ["stale"]},
        )
        before_servers = list(lockfile.mcp_servers)
        before_targets = dict(lockfile.mcp_target_servers)

        with patch("apm_cli.commands.uninstall.engine.MCPIntegrator") as mock_mcp:
            mock_mcp.get_server_names.return_value = set()
            mock_mcp.remove_stale.side_effect = RequiredIntegrationError("cleanup failed")
            with pytest.raises(
                MCPUninstallCleanupError,
                match=r"MCP cleanup failed.*cleanup failed",
            ):
                _cleanup_stale_mcp(
                    apm_package,
                    lockfile,
                    tmp_path / "apm.lock.yaml",
                    {"stale"},
                    modules_dir=tmp_path / "apm_modules",
                    persist=False,
                )

        assert lockfile.mcp_servers == before_servers
        assert lockfile.mcp_target_servers == before_targets

    @pytest.mark.windows_compat
    @pytest.mark.parametrize("user_scope", [False, True])
    def test_claude_atomic_failure_retains_live_bytes_and_ownership(self, tmp_path, user_scope):
        """A failed Claude replace cannot precede lock ownership removal."""
        from types import SimpleNamespace

        from apm_cli.core.scope import InstallScope

        if not user_scope:
            (tmp_path / ".claude").mkdir()
        config_path = tmp_path / (".claude.json" if user_scope else ".mcp.json")
        original = (
            b'{"mcpServers":{"stale":{"type":"stdio","command":"old","args":[]}},"theme":"dark"}\n'
        )
        config_path.write_bytes(original)
        lockfile = LockFile(
            mcp_servers=["stale"],
            mcp_target_servers={"claude": ["stale"]},
            mcp_configs={
                "stale": {
                    "name": "stale",
                    "registry": False,
                    "transport": "stdio",
                    "command": "old",
                }
            },
        )
        before_servers = list(lockfile.mcp_servers)
        before_targets = dict(lockfile.mcp_target_servers)

        with (
            patch(
                "apm_cli.integration.mcp_config_view.CurrentMcpConfigView.derive",
                return_value=SimpleNamespace(
                    dependencies=[], configs={}, provenance={}, bundle_owned=frozenset()
                ),
            ),
            patch(
                "apm_cli.utils.atomic_io._replace_atomic_file",
                side_effect=OSError("simulated crash"),
            ) as replace_file,
            patch("pathlib.Path.home", return_value=tmp_path),
            pytest.raises(MCPUninstallCleanupError, match=r"MCP cleanup failed"),
        ):
            _cleanup_stale_mcp(
                MagicMock(),
                lockfile,
                tmp_path / "apm.lock.yaml",
                {"stale"},
                modules_dir=tmp_path / "apm_modules",
                project_root=tmp_path,
                user_scope=user_scope,
                scope=InstallScope.USER if user_scope else None,
                persist=False,
            )

        assert config_path.read_bytes() == original
        replace_file.assert_called_once()
        assert lockfile.mcp_servers == before_servers
        assert lockfile.mcp_target_servers == before_targets
        assert list(tmp_path.glob("apm-atomic-*")) == []

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


# ===========================================================================
# _resolve_marketplace_packages
# ===========================================================================


# ===========================================================================
# _resolve_marketplace_packages -- multi-entry first-match regression
# ===========================================================================


# ===========================================================================
# _resolve_marketplace_packages -- single-values-call performance contract
# ===========================================================================


# ===========================================================================
# _validate_uninstall_packages -- marketplace ref extensions
# ===========================================================================
