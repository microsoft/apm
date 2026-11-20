"""run mcp integration empty-deps and restore branches."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.core.target_detection import EffectiveTargetDecision
from apm_cli.install.mcp import run_mcp_integration

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-mcp-declaration-scope")]


_PATCH_TARGET = "apm_cli.integration.mcp_integrator.MCPIntegrator"


def _make_apm_package(
    *, scripts=None, targets=None, target=None, allow_executables=None, mcp_deps=None
):
    pkg = MagicMock()
    pkg.scripts = scripts
    pkg.targets = targets
    pkg.target = target
    pkg.allow_executables = allow_executables
    pkg.package_path = Path("/fake/project")
    pkg.get_all_mcp_dependencies.return_value = list(mcp_deps or [])
    return pkg


def _make_logger() -> InstallLogger:
    return InstallLogger(verbose=False)


def _base_kwargs(**overrides):
    kwargs = dict(
        apm_package=_make_apm_package(),
        mcp_deps=[],
        apm_modules_path=Path("/nonexistent/apm_modules"),
        lock_path=Path("/nonexistent/apm.lock.yaml"),
        old_mcp_servers=set(),
        old_mcp_configs={},
        old_mcp_provenance={},
        old_mcp_target_servers={},
        project_root=Path("/fake/project"),
        user_scope=False,
        should_install=True,
        logger=_make_logger(),
    )
    kwargs.update(overrides)
    if "apm_package" not in overrides:
        kwargs["apm_package"] = _make_apm_package(mcp_deps=kwargs["mcp_deps"])
    elif "mcp_deps" in overrides:
        kwargs["apm_package"].get_all_mcp_dependencies.return_value = list(kwargs["mcp_deps"])
    return kwargs


class TestRunMcpIntegrationEmptyDepsBranch:
    """``should_install=True`` and ``mcp_deps`` empty."""

    @patch(_PATCH_TARGET)
    def test_no_deps_no_old_servers_is_a_noop(self, mock_mcp):
        run_mcp_integration(**_base_kwargs(mcp_deps=[]))

        mock_mcp.remove_stale.assert_not_called()
        mock_mcp.update_lockfile.assert_not_called()
        mock_mcp.install.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_no_deps_with_old_servers_prunes_them(self, mock_mcp):
        """Regression for #2077: orphaned servers must be removed and the
        lockfile cleared even when the manifest declares zero MCP deps."""
        count, _apm_config = run_mcp_integration(
            **_base_kwargs(
                mcp_deps=[],
                old_mcp_servers={"io.github.acme/orphan"},
                old_mcp_configs={"io.github.acme/orphan": {"name": "orphan"}},
                old_mcp_target_servers={"copilot": {"io.github.acme/orphan"}},
            )
        )

        assert count == 0
        mock_mcp.remove_stale.assert_called_once()
        assert mock_mcp.remove_stale.call_args.args[0] == {"io.github.acme/orphan"}
        mock_mcp.update_lockfile.assert_called_once_with(
            set(),
            Path("/nonexistent/apm.lock.yaml"),
            mcp_configs={},
            mcp_target_servers={},
            mcp_config_provenance={},
            logger=ANY,
            fail_on_write_error=True,
            stamp_version=False,
        )

    @patch(_PATCH_TARGET)
    def test_cleanup_is_scoped_to_effective_target(self, mock_mcp):
        run_mcp_integration(
            **_base_kwargs(
                mcp_deps=[],
                old_mcp_servers={"stale"},
                old_mcp_configs={"stale": {"name": "stale"}},
                old_mcp_target_servers={"claude": {"stale"}},
                target_decision=EffectiveTargetDecision("claude", "apm config target"),
            )
        )

        assert mock_mcp.remove_stale.call_count == 1
        assert mock_mcp.remove_stale.call_args.args[1] == "claude"

    @patch(_PATCH_TARGET)
    def test_no_deps_with_explicit_empty_ownership_skips_cleanup(self, mock_mcp):
        with patch(
            "apm_cli.install.mcp.ownership.adopt_legacy_mcp_target_servers",
            return_value={"copilot": {"same-name-user-entry"}},
        ) as adopt:
            run_mcp_integration(
                **_base_kwargs(
                    mcp_deps=[],
                    old_mcp_servers={"same-name-user-entry"},
                    old_mcp_configs={"same-name-user-entry": {"name": "same-name-user-entry"}},
                    old_mcp_target_servers={},
                    old_mcp_target_servers_present=True,
                )
            )

        adopt.assert_not_called()
        mock_mcp.remove_stale.assert_not_called()
        mock_mcp.update_lockfile.assert_called_once()


class TestRunMcpIntegrationRestoreBranch:
    """``should_install=False`` (``--only=apm``-style)."""

    @patch(_PATCH_TARGET)
    def test_restores_old_servers_when_not_installing_mcp(self, mock_mcp):
        run_mcp_integration(
            **_base_kwargs(
                mcp_deps=[],
                should_install=False,
                old_mcp_servers={"io.github.acme/kept"},
                old_mcp_configs={"io.github.acme/kept": {"name": "kept"}},
                old_mcp_provenance={"io.github.acme/kept": "io.github.acme/pkg"},
                old_mcp_target_servers={"copilot": ["io.github.acme/kept"]},
            )
        )

        mock_mcp.update_lockfile.assert_called_once_with(
            {"io.github.acme/kept"},
            Path("/nonexistent/apm.lock.yaml"),
            mcp_configs={"io.github.acme/kept": {"name": "kept"}},
            mcp_target_servers={"copilot": ["io.github.acme/kept"]},
            mcp_config_provenance={"io.github.acme/kept": "io.github.acme/pkg"},
            logger=ANY,
            fail_on_write_error=True,
        )
        mock_mcp.install.assert_not_called()
        mock_mcp.remove_stale.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_no_op_when_not_installing_and_no_old_servers(self, mock_mcp):
        run_mcp_integration(**_base_kwargs(mcp_deps=[], should_install=False))

        mock_mcp.update_lockfile.assert_not_called()
        mock_mcp.install.assert_not_called()
