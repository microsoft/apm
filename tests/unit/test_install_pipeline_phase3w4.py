"""Unit tests for apm_cli.install.pipeline -- phase 3 w4.

Covers missing lines/branches in pipeline.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _run_phase
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline -- empty deps short-circuit
# ---------------------------------------------------------------------------


class TestRunInstallPipelineShortCircuit:
    def _make_apm_package(self, deps=None, dev_deps=None):
        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = deps or []
        pkg.get_dev_apm_dependencies.return_value = dev_deps or []
        pkg.get_mcp_dependencies.return_value = []
        return pkg

    def test_no_deps_no_local_returns_install_result(self, tmp_path):
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_apm_package()

        with (
            patch(
                "apm_cli.core.scope.get_deploy_root",
                return_value=tmp_path,
            ),
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=tmp_path,
            ),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=None,
            ),
        ):
            result = run_install_pipeline(pkg)

        assert isinstance(result, InstallResult)

    def test_resolve_phase_returns_no_deps_calls_nothing_to_install(self, tmp_path):
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_apm_package(deps=[MagicMock()])  # has deps but resolve finds nothing
        logger = MagicMock()

        mock_ctx = MagicMock()
        mock_ctx.deps_to_install = []
        mock_ctx.root_has_local_primitives = False
        mock_ctx.tui = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_deploy_root",
                return_value=tmp_path,
            ),
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=tmp_path,
            ),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.install.context.InstallContext", return_value=mock_ctx),
            patch("apm_cli.utils.install_tui.InstallTui", return_value=MagicMock()),
            patch("apm_cli.install.phases.resolve.run") as _resolve_run,
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            result = run_install_pipeline(pkg, logger=logger, target="claude")

        assert isinstance(result, InstallResult)
        logger.nothing_to_install.assert_called_once()


# ---------------------------------------------------------------------------
# run_install_pipeline -- plan_callback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline -- exception wrapping
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check
# ---------------------------------------------------------------------------
