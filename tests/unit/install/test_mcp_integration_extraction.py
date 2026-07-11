"""Unit tests for ``run_mcp_integration`` extraction parity (#2077).

``run_mcp_integration`` (``apm_cli.install.mcp.integration``) was extracted
from the MCP block that used to live inline in
``commands/install.py::_install_apm_packages`` so ``apm update`` could call
the same reconciliation logic. These tests verify the extracted function
preserves the original branch-by-branch behaviour:

* ``install`` / ``remove_stale`` / ``update_lockfile`` wiring when MCP deps
  are present.
* Cleanup (``remove_stale`` + empty ``update_lockfile``) when the manifest
  has no MCP deps but old servers exist.
* ``--only=apm``-style restore (``should_install=False``) when old servers
  exist.
* ``PolicyBlockError`` propagates to the caller uncaught (the caller is
  responsible for reporting + exiting), mirroring the
  ``TestTransitiveMCPBlock`` behaviour in ``test_transitive_mcp_policy.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.install.mcp import run_mcp_integration
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.install_preflight import PolicyBlockError
from apm_cli.policy.parser import load_policy

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "policy"
MCP_POLICY_FIXTURE = FIXTURE_DIR / "apm-policy-mcp.yml"

_PATCH_TARGET = "apm_cli.integration.mcp_integrator.MCPIntegrator"


def _make_apm_package(*, scripts=None, targets=None, target=None, allow_executables=None):
    pkg = MagicMock()
    pkg.scripts = scripts
    pkg.targets = targets
    pkg.target = target
    pkg.allow_executables = allow_executables
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
        project_root=Path("/fake/project"),
        user_scope=False,
        should_install=True,
        logger=_make_logger(),
    )
    kwargs.update(overrides)
    return kwargs


class TestRunMcpIntegrationInstallBranch:
    """``should_install=True`` and ``mcp_deps`` non-empty."""

    @patch(_PATCH_TARGET)
    def test_installs_and_updates_lockfile(self, mock_mcp, tmp_path: Path):
        dep = MCPDependency(name="io.github.acme/server", transport="stdio")
        mock_mcp.deduplicate.side_effect = lambda x: x
        mock_mcp.install.return_value = 1
        mock_mcp.get_server_names.return_value = {"io.github.acme/server"}
        mock_mcp.get_server_configs.return_value = {"io.github.acme/server": {"name": "x"}}
        mock_mcp.get_server_provenance.return_value = {
            "io.github.acme/server": "io.github.acme/package"
        }

        count, apm_config = run_mcp_integration(
            **_base_kwargs(mcp_deps=[dep], project_root=tmp_path)
        )

        assert count == 1
        assert apm_config == {"scripts": {}}
        mock_mcp.install.assert_called_once()
        installed_deps = mock_mcp.install.call_args.args[0]
        assert installed_deps == [dep]
        mock_mcp.update_lockfile.assert_called_once_with(
            {"io.github.acme/server"},
            Path("/nonexistent/apm.lock.yaml"),
            mcp_configs={"io.github.acme/server": {"name": "x"}},
            mcp_config_provenance={"io.github.acme/server": "io.github.acme/package"},
        )
        mock_mcp.remove_stale.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_removes_stale_servers_no_longer_declared(self, mock_mcp, tmp_path: Path):
        dep = MCPDependency(name="io.github.acme/new-server", transport="stdio")
        mock_mcp.deduplicate.side_effect = lambda x: x
        mock_mcp.install.return_value = 1
        mock_mcp.get_server_names.return_value = {"io.github.acme/new-server"}
        mock_mcp.get_server_configs.return_value = {}
        mock_mcp.get_server_provenance.return_value = {}

        run_mcp_integration(
            **_base_kwargs(
                mcp_deps=[dep],
                old_mcp_servers={"io.github.acme/orphan-server"},
                old_mcp_configs={"io.github.acme/orphan-server": {"name": "orphan"}},
                project_root=tmp_path,
            )
        )

        mock_mcp.remove_stale.assert_called_once()
        stale_arg = mock_mcp.remove_stale.call_args.args[0]
        assert stale_arg == {"io.github.acme/orphan-server"}

    @patch(_PATCH_TARGET)
    def test_forwards_apm_config_targets_key_when_declared(self, mock_mcp, tmp_path: Path):
        """#1335: only the targets-key the user actually declared is
        forwarded, matching the original inline block's behaviour."""
        dep = MCPDependency(name="io.github.acme/server", transport="stdio")
        mock_mcp.deduplicate.side_effect = lambda x: x
        mock_mcp.install.return_value = 1
        mock_mcp.get_server_names.return_value = {"io.github.acme/server"}
        mock_mcp.get_server_configs.return_value = {}
        mock_mcp.get_server_provenance.return_value = {}

        pkg = _make_apm_package(scripts={"build": "echo hi"}, targets=["claude", "copilot"])
        _count, apm_config = run_mcp_integration(
            **_base_kwargs(apm_package=pkg, mcp_deps=[dep], project_root=tmp_path)
        )

        assert apm_config == {"scripts": {"build": "echo hi"}, "targets": ["claude", "copilot"]}
        install_kwargs = mock_mcp.install.call_args.kwargs
        assert install_kwargs["apm_config"] == apm_config

    @patch(_PATCH_TARGET)
    def test_forwards_singular_target_key_when_declared(self, mock_mcp, tmp_path: Path):
        dep = MCPDependency(name="io.github.acme/server", transport="stdio")
        mock_mcp.deduplicate.side_effect = lambda x: x
        mock_mcp.install.return_value = 1
        mock_mcp.get_server_names.return_value = {"io.github.acme/server"}
        mock_mcp.get_server_configs.return_value = {}
        mock_mcp.get_server_provenance.return_value = {}

        pkg = _make_apm_package(target="claude")
        _count, apm_config = run_mcp_integration(
            **_base_kwargs(apm_package=pkg, mcp_deps=[dep], project_root=tmp_path)
        )

        assert apm_config == {"scripts": {}, "target": "claude"}
        assert "targets" not in apm_config


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
            )
        )

        assert count == 0
        mock_mcp.remove_stale.assert_called_once()
        assert mock_mcp.remove_stale.call_args.args[0] == {"io.github.acme/orphan"}
        mock_mcp.update_lockfile.assert_called_once_with(
            set(),
            Path("/nonexistent/apm.lock.yaml"),
            mcp_configs={},
            mcp_config_provenance={},
        )


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
            )
        )

        mock_mcp.update_lockfile.assert_called_once_with(
            {"io.github.acme/kept"},
            Path("/nonexistent/apm.lock.yaml"),
            mcp_configs={"io.github.acme/kept": {"name": "kept"}},
            mcp_config_provenance={"io.github.acme/kept": "io.github.acme/pkg"},
        )
        mock_mcp.install.assert_not_called()
        mock_mcp.remove_stale.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_no_op_when_not_installing_and_no_old_servers(self, mock_mcp):
        run_mcp_integration(**_base_kwargs(mcp_deps=[], should_install=False))

        mock_mcp.update_lockfile.assert_not_called()
        mock_mcp.install.assert_not_called()


class TestRunMcpIntegrationPolicyBlock:
    """PolicyBlockError must propagate uncaught; the caller reports + exits."""

    def _fetch_result(self):
        policy, _warnings = load_policy(MCP_POLICY_FIXTURE)
        return PolicyFetchResult(
            policy=policy, source="org:test-org/.github", cached=False, outcome="found"
        )

    @patch(_PATCH_TARGET)
    def test_policy_block_raises_and_does_not_call_install(self, mock_mcp):
        mock_mcp.deduplicate.side_effect = lambda x: x
        evil_dep = MCPDependency(name="io.github.untrusted/evil-transitive", transport="stdio")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=self._fetch_result(),
        ):
            with pytest.raises(PolicyBlockError):
                run_mcp_integration(**_base_kwargs(mcp_deps=[evil_dep]))

        mock_mcp.install.assert_not_called()
        mock_mcp.update_lockfile.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_no_policy_flag_skips_preflight_and_installs(self, mock_mcp):
        mock_mcp.deduplicate.side_effect = lambda x: x
        mock_mcp.install.return_value = 1
        mock_mcp.get_server_names.return_value = {"io.github.untrusted/evil-transitive"}
        mock_mcp.get_server_configs.return_value = {}
        mock_mcp.get_server_provenance.return_value = {}
        evil_dep = MCPDependency(name="io.github.untrusted/evil-transitive", transport="stdio")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=self._fetch_result(),
        ):
            run_mcp_integration(**_base_kwargs(mcp_deps=[evil_dep], no_policy=True))

        mock_mcp.install.assert_called_once()
