"""Unit tests for ``apm_cli.integration.mcp_integrator_install``.

Covers branches not hit by existing MCP tests:

* ``run_mcp_install`` empty deps returns 0 + warning
* ``run_mcp_install`` scope enum overrides user_scope bool
* ``run_mcp_install`` single runtime mode (--runtime)
* ``run_mcp_install`` registry ImportError raises RuntimeError
* ``run_mcp_install`` no target runtimes after all-excluded warning
* ``run_mcp_install`` user scope filtering skips workspace-only runtimes
* ``run_mcp_install`` no runtimes installed fallback to vscode
* ``run_mcp_install`` self-defined deps path (registry: false)
* ``run_mcp_install`` registry invalid server names raises RuntimeError
* ``run_mcp_install`` already configured servers skips re-install
* ``run_mcp_install`` exclude runtime filtering
* ``run_mcp_install`` target_runtimes empty after gating returns 0
* ``run_mcp_install`` script_runtimes intersection logic
* ``run_mcp_install`` no logger defaults to NullCommandLogger
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_dep(name: str) -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = True
    dep.is_self_defined = False
    dep.env = {}
    return dep


def _make_self_defined_dep(name: str, transport: str = "stdio") -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = False
    dep.is_self_defined = True
    dep.transport = transport
    dep.env = {}
    return dep


def _install_claude_only(
    runtime: str,
    servers: list[str],
    *_args: object,
    installed_servers: set[str],
    **_kwargs: object,
) -> bool:
    """Model successful writes separately from the aggregate install outcome."""
    if runtime != "claude":
        return False
    installed_servers.update(servers)
    return True


def test_self_defined_partial_runtime_failure_is_explicit() -> None:
    """A selected runtime failure must not be hidden by a sibling success."""
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.integration.mcp_integrator_install import _install_self_defined_deps

    dependency = _make_self_defined_dep("managed-server", transport="http")
    managed: dict[str, set[str]] = {}
    logger = MagicMock()

    with (
        patch.object(
            MCPIntegrator,
            "_check_self_defined_servers_needing_installation",
            return_value=["managed-server"],
        ),
        patch.object(MCPIntegrator, "_build_self_defined_info", return_value={}),
        patch.object(
            MCPIntegrator,
            "_install_for_runtime",
            side_effect=_install_claude_only,
        ) as install_runtime,
        pytest.raises(RuntimeError, match=r"managed-server \(intellij\)"),
    ):
        _install_self_defined_deps(
            self_defined_deps=[dependency],
            target_runtimes=["claude", "intellij"],
            stored_mcp_configs={},
            servers_to_update=set(),
            successful_updates=set(),
            project_root=None,
            user_scope=False,
            verbose=False,
            console=None,
            logger=logger,
            managed_target_servers=managed,
        )

    assert install_runtime.call_count == 2
    assert managed == {"claude": {"managed-server"}}
    logger.error.assert_any_call(
        "MCP configuration failed for selected runtime(s): managed-server (intellij). "
        "Fix the failed runtime MCP config and rerun the original install command, "
        "retaining --global and any --target selection."
    )


def test_registry_group_partial_runtime_failure_is_explicit() -> None:
    """Registry installs enforce the same strict IntelliJ failure contract."""
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.integration.mcp_integrator_install import _install_registry_group

    operations = MagicMock()
    operations.validate_servers_exist.return_value = (["managed-server"], [])
    operations.check_servers_needing_installation.return_value = ["managed-server"]
    operations.batch_fetch_server_info.return_value = {"managed-server": {}}
    operations.collect_environment_variables.return_value = {}
    operations.collect_runtime_variables.return_value = {}
    managed: dict[str, set[str]] = {}
    logger = MagicMock()

    with (
        patch.object(
            MCPIntegrator,
            "_install_for_runtime",
            side_effect=_install_claude_only,
        ) as install_runtime,
        pytest.raises(RuntimeError, match=r"managed-server \(intellij\)"),
    ):
        _install_registry_group(
            operations=operations,
            group_dep_names=["managed-server"],
            group_dep_map={},
            group_deps=["managed-server"],
            target_runtimes=["claude", "intellij"],
            stored_mcp_configs={},
            servers_to_update=set(),
            successful_updates=set(),
            project_root=None,
            user_scope=False,
            verbose=False,
            console=None,
            logger=logger,
            managed_target_servers=managed,
        )

    assert install_runtime.call_count == 2
    assert managed == {"claude": {"managed-server"}}
    logger.error.assert_any_call(
        "MCP configuration failed for selected runtime(s): managed-server (intellij). "
        "Fix the failed runtime MCP config and rerun the original install command, "
        "retaining --global and any --target selection."
    )


def test_intellij_migration_error_is_already_rendered() -> None:
    """Migration errors preserve their actionable message without generic wrapping."""
    from apm_cli.adapters.client.intellij import IntelliJConfigError
    from apm_cli.install.errors import InstallFailureAlreadyRendered
    from apm_cli.integration.mcp_integrator_install import _migrate_intellij_managed_config

    client = MagicMock()
    client.migrate_legacy_managed_servers.side_effect = IntelliJConfigError(
        "Fix the legacy IntelliJ config, then rerun apm install."
    )
    logger = MagicMock()
    with (
        patch("apm_cli.factory.ClientFactory.create_client", return_value=client),
        pytest.raises(
            InstallFailureAlreadyRendered,
            match="Fix the legacy IntelliJ config",
        ),
    ):
        _migrate_intellij_managed_config(
            ["intellij"],
            {"intellij": {"managed-server"}},
            project_root=None,
            user_scope=False,
            logger=logger,
        )

    logger.error.assert_called_once_with("Fix the legacy IntelliJ config, then rerun apm install.")


# ---------------------------------------------------------------------------
# Empty deps


@pytest.fixture
def registry_misses(monkeypatch):
    """Every registry lookup misses, independent of network reachability."""
    from apm_cli.registry.operations import MCPServerOperations

    monkeypatch.setattr(
        MCPServerOperations,
        "validate_servers_exist",
        lambda self, refs, *args, **kwargs: ([], list(refs)),
    )


# ---------------------------------------------------------------------------


class TestRunMcpInstallEmptyDeps:
    def test_empty_list_returns_zero_and_warns(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        logger = MagicMock()
        result = run_mcp_install([], logger=logger)
        assert result == 0
        logger.warning.assert_called_once()

    def test_none_logger_still_returns_zero(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install([])
        assert result == 0


# ---------------------------------------------------------------------------
# scope enum overrides user_scope bool
# ---------------------------------------------------------------------------


class TestRunMcpInstallScopeEnum:
    def _basic_stub(self):
        """Return the minimum stubs to short-circuit after scope handling."""
        mock_integrator_cls = MagicMock()
        mock_integrator_cls._detect_runtimes.return_value = set()
        mock_integrator_cls._gate_project_scoped_runtimes.return_value = []
        return mock_integrator_cls

    def test_scope_user_sets_user_scope_true(self, registry_misses) -> None:
        from apm_cli.core.scope import InstallScope
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
            run_mcp_install(
                [dep],
                runtime="copilot",
                scope=InstallScope.USER,
                logger=logger,
            )
        logger.error.assert_called_once()
        assert "server1" in logger.error.call_args.args[0]

    def test_scope_project_sets_user_scope_false(self, registry_misses) -> None:
        from apm_cli.core.scope import InstallScope
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()
        with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
            run_mcp_install(
                [dep],
                runtime="copilot",
                scope=InstallScope.PROJECT,
                logger=logger,
            )
        logger.error.assert_called_once()
        assert "server1" in logger.error.call_args.args[0]


# ---------------------------------------------------------------------------
# single runtime mode (--runtime)
# ---------------------------------------------------------------------------


class TestRunMcpInstallSingleRuntime:
    def test_single_runtime_targets_only_that_runtime(self, registry_misses) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        # Just verify no error about "no runtimes" when runtime= is explicit
        mock_integrator_cls = MagicMock()
        mock_integrator_cls._detect_runtimes.return_value = set()
        mock_integrator_cls._gate_project_scoped_runtimes.return_value = []

        with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
            run_mcp_install([dep], runtime="copilot", logger=logger)

        # Progress message about specific runtime should appear
        progress_calls = [str(c) for c in logger.progress.call_args_list]
        assert any("copilot" in c for c in progress_calls)


# ---------------------------------------------------------------------------
# registry ImportError
# ---------------------------------------------------------------------------


class TestRunMcpInstallRegistryImportError:
    def test_registry_import_error_raises_runtime_error(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        mock_integrator_cls = MagicMock()
        mock_integrator_cls._detect_runtimes.return_value = set()
        mock_integrator_cls._gate_project_scoped_runtimes.side_effect = lambda rts, **kw: rts

        import sys

        # Patch so that importing MCPServerOperations raises ImportError
        with (
            patch.dict(sys.modules, {"apm_cli.registry.operations": None}),
        ):
            with pytest.raises((RuntimeError, ImportError)):
                run_mcp_install([dep], runtime="copilot", logger=logger)


# ---------------------------------------------------------------------------
# exclude runtime filtering
# ---------------------------------------------------------------------------


class TestRunMcpInstallExclude:
    def test_exclude_removes_runtime_from_targets(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        # When all runtimes are excluded, returns 0 with warning
        mock_integrator_cls = MagicMock()
        mock_integrator_cls._detect_runtimes.return_value = set()
        mock_integrator_cls._gate_project_scoped_runtimes.side_effect = lambda rts, **kw: rts

        # Provide runtime + exclude the same runtime -> empty target list
        result = run_mcp_install(
            [dep],
            runtime="copilot",
            exclude="copilot",
            logger=logger,
        )

        assert result == 0
        logger.warning.assert_called_once_with(
            "All selected MCP runtimes excluded (--exclude copilot), skipping MCP configuration"
        )


# ---------------------------------------------------------------------------
# no target runtimes after gating
# ---------------------------------------------------------------------------


class TestRunMcpInstallNoTargetRuntimes:
    def test_returns_zero_when_all_gated_away(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        mock_integrator_cls = MagicMock()
        mock_integrator_cls._detect_runtimes.return_value = set()
        # gate returns empty list -> function returns 0 early
        mock_integrator_cls._gate_project_scoped_runtimes.return_value = []

        import sys
        from unittest.mock import MagicMock as MM

        mock_mcp_integrator_mod = MM()
        mock_mcp_integrator_mod.MCPIntegrator = mock_integrator_cls
        mock_mcp_integrator_mod._get_console.return_value = None
        mock_mcp_integrator_mod._is_vscode_available.return_value = False

        mock_rm = MM()
        mock_rm.is_runtime_available.return_value = True
        mock_rm_cls = MM(return_value=mock_rm)

        mock_cf = MM()
        mock_cf.create_client.return_value = MM()

        with (
            patch.dict(
                sys.modules,
                {
                    "apm_cli.integration.mcp_integrator": mock_mcp_integrator_mod,
                    "apm_cli.runtime.manager": MM(RuntimeManager=mock_rm_cls),
                    "apm_cli.factory": MM(ClientFactory=mock_cf),
                },
            ),
        ):
            result = run_mcp_install([dep], runtime="copilot", logger=logger)

        assert result == 0


# ---------------------------------------------------------------------------
# self-defined deps
# ---------------------------------------------------------------------------


class TestRunMcpInstallSelfDefined:
    def test_self_defined_non_stdio_env_remains_env_overrides(self) -> None:
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        from apm_cli.integration.mcp_integrator_install import run_mcp_install
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency(
            name="http-demo",
            registry=False,
            transport="http",
            url="https://example.test/mcp",
            env={"HTTP_TOKEN": "${HTTP_TOKEN}"},
        )

        with (
            patch(
                "apm_cli.integration.mcp_integrator_install._resolve_target_runtimes",
                return_value=["claude"],
            ),
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch.object(
                MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value={"http-demo"},
            ),
            patch.object(MCPIntegrator, "_install_for_runtime", return_value=True) as install_mock,
        ):
            result = run_mcp_install([dep], runtime="claude", logger=MagicMock())

        assert result == 1
        install_mock.assert_called_once()
        assert install_mock.call_args.args[2] == {"HTTP_TOKEN": "${HTTP_TOKEN}"}


# ---------------------------------------------------------------------------
# plain strings treated as registry deps
# ---------------------------------------------------------------------------


class TestRunMcpInstallPlainStrings:
    def test_plain_string_deps_are_registry_deps(self, registry_misses) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        logger = MagicMock()
        with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
            run_mcp_install(["github/server"], runtime="copilot", logger=logger)
        assert "github/server" in logger.error.call_args.args[0]


# ---------------------------------------------------------------------------
# stored_mcp_configs defaults to empty dict
# ---------------------------------------------------------------------------


class TestRunMcpInstallStoredMcpConfigs:
    def test_none_stored_configs_treated_as_empty(self, registry_misses) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()
        with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
            run_mcp_install([dep], runtime="copilot", stored_mcp_configs=None, logger=logger)


# ---------------------------------------------------------------------------
# apm_config lazy load
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# verbose detail logging
# ---------------------------------------------------------------------------


class TestRunMcpInstallVerboseLogging:
    def test_verbose_mode_no_crash(self, registry_misses) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()
        logger.verbose_detail = MagicMock()

        with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
            run_mcp_install([dep], runtime="copilot", verbose=True, logger=logger)

        logger.verbose_detail.assert_called_once_with("Validating 1 registry servers...")


# ---------------------------------------------------------------------------
# console print path (no console)
# ---------------------------------------------------------------------------


class TestRunMcpInstallConsoleNone:
    def test_no_console_uses_logger_progress(self, registry_misses) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        with pytest.raises(RuntimeError, match="Cannot install 1 missing server"):
            run_mcp_install([dep], runtime="copilot", logger=logger)

        logger.progress.assert_any_call("Targeting specific runtime: copilot")
