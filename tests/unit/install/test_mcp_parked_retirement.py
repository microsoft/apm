"""Parked MCP servers are retired only with this installation's deployment proof."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.install.mcp import run_mcp_integration
from apm_cli.install.mcp.integration import PreparedMcpIntegration
from apm_cli.install.mcp.parked_retirement import retire_unrecorded_parked_servers
from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
from apm_cli.models.dependency.mcp import MCPDependency

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-mcp-declaration-scope"),
    pytest.mark.promise("P-INV-I4"),
]

_MATCHES = "apm_cli.install.mcp.native_entry.native_mcp_entry_matches_declaration"
_REMOVE = "apm_cli.integration.mcp_integrator.MCPIntegrator.remove_stale"
_RETIRE = "apm_cli.install.mcp.parked_retirement.retire_unrecorded_parked_servers"


def _self_defined(name: str) -> MCPDependency:
    return MCPDependency.from_dict(
        {"name": name, "registry": False, "transport": "stdio", "command": "srv"}
    )


def _retire(parked, receipted_owners):
    logger = MagicMock()
    with (
        patch(_MATCHES, return_value=True) as matches,
        patch(_REMOVE) as remove,
    ):
        removed = retire_unrecorded_parked_servers(
            parked,
            runtimes=["copilot"],
            receipted_owners=frozenset(receipted_owners),
            project_root=Path("/fake/project"),
            user_scope=False,
            scope=None,
            logger=logger,
        )
    return removed, matches, remove


def test_matching_entry_of_a_receipted_package_is_removed():
    removed, _, remove = _retire({"srv": ("pkg", _self_defined("srv"))}, {"pkg"})

    assert removed == {"srv"}
    remove.assert_called_once()


def test_matching_entry_without_deployment_proof_is_kept():
    """An identical entry is not proof APM wrote it: it may be the user's."""
    removed, matches, remove = _retire({"srv": ("pkg", _self_defined("srv"))}, set())

    assert removed == set()
    matches.assert_not_called()
    remove.assert_not_called()


def test_registry_server_is_never_removed_by_equality():
    registry = MCPDependency.from_string("io.github.example/srv")
    removed, _, remove = _retire({"srv": ("pkg", registry)}, {"pkg"})

    assert removed == set()
    remove.assert_not_called()


def _prepared_with_parked() -> PreparedMcpIntegration:
    view = CurrentMcpConfigView(
        dependencies=(),
        configs={},
        provenance={},
        problems=(),
        parked={"srv": ("pkg", _self_defined("srv"))},
    )
    return PreparedMcpIntegration([], view, view)


def _run(pre_install_lock):
    package = MagicMock()
    package.scripts = None
    package.targets = None
    package.target = None
    package.package_path = Path("/fake/project")
    package.get_all_mcp_dependencies.return_value = []
    with (
        patch("apm_cli.integration.mcp_integrator.MCPIntegrator"),
        patch(_RETIRE) as retire,
    ):
        run_mcp_integration(
            apm_package=package,
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
            logger=MagicMock(),
            runtime="copilot",
            prepared=_prepared_with_parked(),
            pre_install_lock=pre_install_lock,
            receipted_owners=frozenset({"pkg"}),
        )
    return retire


def test_install_that_started_with_a_lockfile_never_retires_by_equality():
    _run(LockFile()).assert_not_called()


def test_install_that_started_without_a_lockfile_retires_proven_servers():
    retire = _run(None)

    retire.assert_called_once()
    assert retire.call_args.kwargs["receipted_owners"] == frozenset({"pkg"})
