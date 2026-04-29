"""Coverage gap tests for MCPIntegrator methods."""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch):
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


class TestCollectTransitive:

    def test_no_lock_file_returns_list(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        result = MCPIntegrator.collect_transitive(
            apm_modules_dir=Path("/tmp/fake_modules"),
        )
        assert isinstance(result, list)

    def test_with_logger(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.collect_transitive(
            apm_modules_dir=Path("/tmp/fake_modules"),
            logger=logger,
        )
        assert isinstance(result, list)

    def test_without_logger(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        result = MCPIntegrator.collect_transitive(
            apm_modules_dir=Path("/tmp/fake_modules"),
            logger=None,
        )
        assert isinstance(result, list)

    def test_with_lock_path(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        result = MCPIntegrator.collect_transitive(
            apm_modules_dir=Path("/tmp/fake_modules"),
            lock_path=Path("/tmp/fake.lock"),
        )
        assert isinstance(result, list)

    def test_trust_private_flag(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        result = MCPIntegrator.collect_transitive(
            apm_modules_dir=Path("/tmp/fake_modules"),
            trust_private=True,
        )
        assert isinstance(result, list)


class TestInstallForRuntime:
    """Test _install_for_runtime() error handling paths."""

    def test_unsupported_runtime_with_logger(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator._install_for_runtime(
            mcp_deps=[MagicMock()],
            runtime="nonexistent_runtime_xyz",
            logger=logger,
        )
        assert result is False

    def test_unsupported_runtime_without_logger(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        result = MCPIntegrator._install_for_runtime(
            mcp_deps=[MagicMock()],
            runtime="nonexistent_runtime_xyz",
        )
        assert result is False


class TestNullCommandLogger:
    """Verify NullCommandLogger interface matches CommandLogger."""

    def test_has_all_required_methods(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        assert hasattr(nl, 'progress')
        assert hasattr(nl, 'success')
        assert hasattr(nl, 'warning')
        assert hasattr(nl, 'error')
        assert hasattr(nl, 'verbose_detail')
        assert hasattr(nl, 'start')

    def test_verbose_is_false(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        assert nl.verbose is False

    def test_progress_does_not_crash(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        nl.progress("test message")  # Should not raise

    def test_warning_does_not_crash(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        nl.warning("test warning")

    def test_error_does_not_crash(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        nl.error("test error")

    def test_success_does_not_crash(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        nl.success("test success")

    def test_verbose_detail_discards(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        nl.verbose_detail("this should be discarded")

    def test_start_does_not_crash(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        nl.start("starting operation")

    def test_tree_item_does_not_crash(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        nl.tree_item("  item")

    def test_package_inline_warning_discards(self):
        from apm_cli.core.null_logger import NullCommandLogger
        nl = NullCommandLogger()
        nl.package_inline_warning("inline warning")


class TestLoggerForkPaths:
    """Verify both logger=None and logger=provided paths work identically."""

    def test_install_both_paths_return_same_type(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        with patch("apm_cli.integration.mcp_integrator.LockFile"):
            r1 = MCPIntegrator.install(mcp_deps=[])
            logger = MagicMock()
            logger.verbose = False
            r2 = MCPIntegrator.install(mcp_deps=[], logger=logger)
            assert type(r1) == type(r2)

    def test_collect_transitive_both_paths_return_same_type(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        r1 = MCPIntegrator.collect_transitive(apm_modules_dir=Path("/tmp/x"))
        logger = MagicMock()
        logger.verbose = False
        r2 = MCPIntegrator.collect_transitive(apm_modules_dir=Path("/tmp/x"), logger=logger)
        assert type(r1) == type(r2)

    def test_remove_stale_both_paths_return_same_type(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        r1 = MCPIntegrator.remove_stale(stale_names=set())
        logger = MagicMock()
        logger.verbose = False
        r2 = MCPIntegrator.remove_stale(stale_names=set(), logger=logger)
        assert type(r1) == type(r2)

    def test_install_for_runtime_both_paths_return_same_type(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        r1 = MCPIntegrator._install_for_runtime(
            mcp_deps=[MagicMock()], runtime="nonexistent_xyz",
        )
        logger = MagicMock()
        logger.verbose = False
        r2 = MCPIntegrator._install_for_runtime(
            mcp_deps=[MagicMock()], runtime="nonexistent_xyz", logger=logger,
        )
        assert type(r1) == type(r2)
