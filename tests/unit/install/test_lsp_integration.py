"""Unit tests for ``apm_cli.install.lsp.integration.run_lsp_integration``.

Covers the high-level orchestration that wires together LSPIntegrator
calls: transitive collection, deduplication, install, stale cleanup,
and lockfile persistence.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.install.lsp.integration import run_lsp_integration
from apm_cli.models.dependency.lsp import LSPDependency

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep(name: str, **kwargs) -> LSPDependency:
    defaults = {
        "command": kwargs.pop("command", f"{name}-langserver"),
        "extension_to_language": kwargs.pop("extension_to_language", {".py": "python"}),
    }
    defaults.update(kwargs)
    return LSPDependency(name=name, **defaults)


def _mock_logger():
    logger = MagicMock()
    logger.verbose_detail = MagicMock()
    logger.progress = MagicMock()
    return logger


def _mock_lock(*, lsp_servers=None, lsp_configs=None):
    lock = MagicMock()
    lock.lsp_servers = lsp_servers or []
    lock.lsp_configs = lsp_configs or {}
    return lock


_PATCH_TARGET = "apm_cli.integration.lsp_integrator.LSPIntegrator"


# ===========================================================================
# Basic orchestration
# ===========================================================================


class TestRunLspIntegration:
    @patch(_PATCH_TARGET)
    def test_no_lsp_deps_no_old_servers(self, mock_integrator, tmp_path):
        """No LSP deps and no previous state -- nothing to do."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = []

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        assert count == 0
        mock_integrator.install.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_installs_direct_deps(self, mock_integrator, tmp_path):
        """Direct LSP deps are installed when should_install is True."""
        deps = [_make_dep("pyright")]
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = deps

        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {"pyright": {}}
        mock_integrator.collect_transitive.return_value = []

        modules = tmp_path / "apm_modules"
        modules.mkdir()

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=modules,
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        assert count == 1
        mock_integrator.install.assert_called_once()

    @patch(_PATCH_TARGET)
    def test_resolves_targets_for_install(self, mock_integrator, tmp_path):
        """Install orchestration writes only to resolved LSP targets."""
        deps = [_make_dep("pyright")]
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = deps

        mock_integrator.resolve_target_runtimes.return_value = ["copilot"]
        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {"pyright": {}}
        mock_integrator.collect_transitive.return_value = []
        logger = _mock_logger()

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=logger,
        )

        assert count == 1
        mock_integrator.resolve_target_runtimes.assert_called_once()
        mock_integrator.install.assert_called_once_with(
            deps,
            project_root=tmp_path,
            user_scope=False,
            logger=logger,
            diagnostics=None,
            target_runtimes=["copilot"],
        )

    @patch(_PATCH_TARGET)
    def test_deduplicates_transitive(self, mock_integrator, tmp_path):
        """When transitive deps exist, deduplication is applied."""
        direct = [_make_dep("pyright")]
        transitive = [_make_dep("ruff-lsp")]

        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = direct

        mock_integrator.collect_transitive.return_value = transitive
        mock_integrator.deduplicate.return_value = direct + transitive
        mock_integrator.install.return_value = 2
        mock_integrator.get_server_names.return_value = {"pyright", "ruff-lsp"}
        mock_integrator.get_server_configs.return_value = {}

        modules = tmp_path / "apm_modules"
        modules.mkdir()

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=modules,
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        assert count == 2
        mock_integrator.deduplicate.assert_called_once()


# ===========================================================================
# Stale cleanup
# ===========================================================================


class TestStaleCleanup:
    @patch(_PATCH_TARGET)
    def test_removes_stale_servers(self, mock_integrator, tmp_path):
        """Servers in old lockfile but not in new deps are removed."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = [_make_dep("pyright")]

        old_lock = _mock_lock(lsp_servers=["pyright", "old-server"])

        mock_integrator.collect_transitive.return_value = []
        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {}

        modules = tmp_path / "apm_modules"
        modules.mkdir()

        run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=modules,
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        mock_integrator.remove_stale.assert_called_once()
        stale_arg = mock_integrator.remove_stale.call_args
        assert "old-server" in stale_arg.args[0] or "old-server" in stale_arg[0][0]

    @patch(_PATCH_TARGET)
    def test_removes_all_old_when_no_deps_remain(self, mock_integrator, tmp_path):
        """When no LSP deps remain, all old servers are removed."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = []

        old_lock = _mock_lock(lsp_servers=["old-a", "old-b"])

        run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        mock_integrator.remove_stale.assert_called_once()
        stale_arg = mock_integrator.remove_stale.call_args[0][0]
        assert stale_arg == {"old-a", "old-b"}


# ===========================================================================
# --only=apm (should_install=False)
# ===========================================================================


class TestSkipInstall:
    @patch(_PATCH_TARGET)
    def test_restores_old_lockfile_when_not_installing(self, mock_integrator, tmp_path):
        """When should_install=False with old servers, lockfile is restored."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = []

        old_lock = _mock_lock(
            lsp_servers=["preserved"],
            lsp_configs={"preserved": {"name": "preserved"}},
        )

        run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=False,
            logger=_mock_logger(),
        )

        mock_integrator.update_lockfile.assert_called_once()
        mock_integrator.install.assert_not_called()
