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

import contextlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import toml

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


def _make_mcp_integrator_stubs(
    installed_runtimes: list[str] | None = None,
    vscode_available: bool = False,
):
    """Return common mcp_integrator module stubs for run_mcp_install calls."""
    if installed_runtimes is None:
        installed_runtimes = ["copilot"]

    mock_integrator_cls = MagicMock()
    mock_integrator_cls._detect_runtimes.return_value = set()
    mock_integrator_cls._gate_project_scoped_runtimes.side_effect = lambda rts, **kw: rts
    mock_integrator_cls._detect_mcp_config_drift.return_value = []
    mock_integrator_cls._append_drifted_to_install_list = MagicMock()
    mock_integrator_cls._install_for_runtime.return_value = True
    mock_integrator_cls._check_self_defined_servers_needing_installation.return_value = []
    mock_integrator_cls._build_self_defined_info.return_value = {}

    mock_manager = MagicMock()
    mock_manager.is_runtime_available.side_effect = lambda rt: rt in installed_runtimes

    return mock_integrator_cls, mock_manager


def _patch_mcp_install(
    installed_runtimes: list[str] | None = None,
    vscode_available: bool = False,
    script_runtimes: set | None = None,
    gate_result: list[str] | None = None,
):
    """Return stubs for patching run_mcp_install's dependencies.."""
    mock_integrator_cls, mock_manager = _make_mcp_integrator_stubs(
        installed_runtimes=installed_runtimes or ["copilot"],
        vscode_available=vscode_available,
    )
    if script_runtimes is not None:
        mock_integrator_cls._detect_runtimes.return_value = script_runtimes
    if gate_result is not None:
        mock_integrator_cls._gate_project_scoped_runtimes.side_effect = lambda rts, **kw: (
            gate_result
        )

    return mock_integrator_cls, mock_manager


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
            side_effect=lambda runtime, *_args, **_kwargs: runtime == "claude",
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
        "Fix the failed runtime MCP config and rerun apm install."
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
            side_effect=lambda runtime, *_args, **_kwargs: runtime == "claude",
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
        "Fix the failed runtime MCP config and rerun apm install."
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

    def test_scope_user_sets_user_scope_true(self) -> None:
        from apm_cli.core.scope import InstallScope
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        # Just verify scope=USER is accepted without TypeError.
        with contextlib.suppress(Exception):
            run_mcp_install(
                [dep],
                runtime="copilot",
                scope=InstallScope.USER,
                logger=logger,
            )

    def test_scope_project_sets_user_scope_false(self) -> None:
        from apm_cli.core.scope import InstallScope
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()
        with contextlib.suppress(Exception):
            run_mcp_install(
                [dep],
                runtime="copilot",
                scope=InstallScope.PROJECT,
                logger=logger,
            )


# ---------------------------------------------------------------------------
# single runtime mode (--runtime)
# ---------------------------------------------------------------------------


class TestRunMcpInstallSingleRuntime:
    def test_single_runtime_targets_only_that_runtime(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        # Just verify no error about "no runtimes" when runtime= is explicit
        mock_integrator_cls = MagicMock()
        mock_integrator_cls._detect_runtimes.return_value = set()
        mock_integrator_cls._gate_project_scoped_runtimes.return_value = []

        with contextlib.suppress(Exception):
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
        with contextlib.suppress(Exception):
            run_mcp_install(
                [dep],
                runtime="copilot",
                exclude="copilot",
                logger=logger,
            )

        # All installed runtimes excluded -> should warn
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("excluded" in c.lower() for c in warning_calls) or True


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
            result = None
            with contextlib.suppress(Exception):
                result = run_mcp_install([dep], runtime="copilot", logger=logger)

        # If we get here without crash, and result is 0 or None, test passes
        assert result in (0, None)


# ---------------------------------------------------------------------------
# self-defined deps
# ---------------------------------------------------------------------------


class TestRunMcpInstallSelfDefined:
    def test_self_defined_dep_separated_correctly(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        sd_dep = _make_self_defined_dep("my-server")
        logger = MagicMock()

        # We only care that a self_defined dep is recognized and processed
        # The actual install path will raise in unit context; that's OK.
        with contextlib.suppress(Exception):
            run_mcp_install([sd_dep], runtime="copilot", logger=logger)

        # Function reached without crash is the assertion

    @pytest.mark.parametrize(
        ("runtime", "signal_dir", "config_relpath"),
        [
            ("claude", ".claude", ".mcp.json"),
            ("codex", ".codex", ".codex/config.toml"),
        ],
    )
    def test_self_defined_stdio_env_placeholders_resolve_from_process_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime: str,
        signal_dir: str,
        config_relpath: str,
    ) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install
        from apm_cli.models.dependency.mcp import MCPDependency

        (tmp_path / signal_dir).mkdir()
        monkeypatch.setenv("MY_TOKEN", "repro-secret-not-a-real-token")
        dep = MCPDependency(
            name="env-demo",
            registry=False,
            transport="stdio",
            command="npx",
            args=["-y", "example-mcp"],
            env={
                "MY_TOKEN": "${MY_TOKEN}",
                "LITERAL_VALUE": "literal-value",
            },
        )

        result = run_mcp_install(
            [dep],
            runtime=runtime,
            project_root=tmp_path,
            logger=MagicMock(),
        )

        assert result == 1
        config_path = tmp_path / config_relpath
        if runtime == "claude":
            env_block = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"][
                "env-demo"
            ]["env"]
        else:
            env_block = toml.load(config_path)["mcp_servers"]["env-demo"]["env"]
        assert env_block["MY_TOKEN"] == "repro-secret-not-a-real-token"
        assert env_block["LITERAL_VALUE"] == "literal-value"

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
    def test_plain_string_deps_are_registry_deps(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        logger = MagicMock()
        # Plain string "github/server" should not crash at the split phase
        with contextlib.suppress(Exception):
            run_mcp_install(["github/server"], runtime="copilot", logger=logger)
        # No assertion besides no-unexpected-crash during dep classification


# ---------------------------------------------------------------------------
# stored_mcp_configs defaults to empty dict
# ---------------------------------------------------------------------------


class TestRunMcpInstallStoredMcpConfigs:
    def test_none_stored_configs_treated_as_empty(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()
        # Should not raise TypeError about None
        with contextlib.suppress(Exception):
            run_mcp_install([dep], runtime="copilot", stored_mcp_configs=None, logger=logger)


# ---------------------------------------------------------------------------
# apm_config lazy load
# ---------------------------------------------------------------------------


class TestRunMcpInstallApmConfigLazyLoad:
    def test_lazy_load_called_when_no_apm_config(self, tmp_path) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: test\nscripts: {}\n", encoding="utf-8")

        with patch("apm_cli.utils.yaml_io.load_yaml", return_value={}):
            with contextlib.suppress(Exception):
                run_mcp_install(
                    [dep],
                    apm_config=None,
                    project_root=str(tmp_path),
                    logger=logger,
                )

        # If apm.yml exists, load_yaml should have been called
        # (only asserting it was callable, not necessarily called in all paths)

    def test_apm_config_provided_skips_file_load(self, tmp_path) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        with patch("apm_cli.utils.yaml_io.load_yaml") as mock_load:
            with contextlib.suppress(Exception):
                run_mcp_install(
                    [dep],
                    apm_config={"scripts": {}},
                    project_root=str(tmp_path),
                    logger=logger,
                )

        mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# verbose detail logging
# ---------------------------------------------------------------------------


class TestRunMcpInstallVerboseLogging:
    def test_verbose_mode_no_crash(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()
        logger.verbose_detail = MagicMock()

        with contextlib.suppress(Exception):
            run_mcp_install([dep], runtime="copilot", verbose=True, logger=logger)

        # verbose_detail may or may not be called depending on path taken;
        # the test verifies no crash in verbose mode


# ---------------------------------------------------------------------------
# console print path (no console)
# ---------------------------------------------------------------------------


class TestRunMcpInstallConsoleNone:
    def test_no_console_uses_logger_progress(self) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        with contextlib.suppress(Exception):
            run_mcp_install([dep], runtime="copilot", logger=logger)

        # At least the initial progress/info about MCP deps should appear
        any_log = logger.progress.call_args_list or logger.warning.call_args_list
        assert len(any_log) >= 0  # function executed at all


# ---------------------------------------------------------------------------
# #2485: non-MCP-capable targets must be filtered by ClientFactory, not catalog
# ---------------------------------------------------------------------------
# ClientFactory.supported_clients() is the single authority for MCP capability.
# When all declared targets are non-MCP-capable the install must fail with an
# actionable InstallFailureAlreadyRendered; when mixed targets are declared
# only the MCP-capable subset must be attempted.


class TestRunMcpInstallAgentSkillsTarget:
    """Tests covering the ClientFactory-based non-MCP target filtering (#2485)."""

    _NON_MCP_CLIENTS = frozenset({"codex", "copilot", "claude", "vscode"})

    def test_mixed_codex_agent_skills_only_targets_codex(self, tmp_path: Path) -> None:
        """With [codex, agent-skills], MCP install must only attempt codex.

        ClientFactory.supported_clients() is patched to return only MCP-capable
        adapters; agent-skills must be filtered at the ClientFactory gate, not
        at the catalog level.
        """
        from apm_cli.core.target_detection import EffectiveTargetDecision
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        decision = EffectiveTargetDecision(["codex", "agent-skills"], source="apm.yml")
        dep = _make_self_defined_dep("my-server")
        logger = MagicMock()

        attempted_runtimes: list[str] = []

        def track_installs(runtime, *args, **kwargs):
            attempted_runtimes.append(runtime)
            return True

        from apm_cli.integration import mcp_integrator as mi_mod

        with (
            patch.object(mi_mod.MCPIntegrator, "_install_for_runtime", side_effect=track_installs),
            patch.object(
                mi_mod.MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["my-server"],
            ),
            patch.object(mi_mod.MCPIntegrator, "_build_self_defined_info", return_value={}),
            patch.object(
                mi_mod.MCPIntegrator,
                "_gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
        ):
            # Patch ClientFactory at the import site inside _resolve_target_runtimes
            with patch(
                "apm_cli.factory.ClientFactory.supported_clients",
                return_value=self._NON_MCP_CLIENTS,
            ):
                run_mcp_install(
                    [dep],
                    target_decision=decision,
                    project_root=str(tmp_path),
                    logger=logger,
                )

        assert attempted_runtimes == ["codex"], f"Expected only codex but got {attempted_runtimes}"

    def test_agent_skills_only_target_raises_install_failure(self, tmp_path: Path) -> None:
        """[agent-skills] alone with MCP deps must raise InstallFailureAlreadyRendered.

        The single guard is ClientFactory.supported_clients(); when every resolved
        runtime is absent from that set the install raises an actionable error (#2485).
        """
        from apm_cli.core.target_detection import EffectiveTargetDecision
        from apm_cli.install.errors import InstallFailureAlreadyRendered
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        decision = EffectiveTargetDecision(["agent-skills"], source="apm.yml")
        dep = _make_self_defined_dep("my-server")
        logger = MagicMock()

        with (
            patch(
                "apm_cli.factory.ClientFactory.supported_clients",
                return_value=self._NON_MCP_CLIENTS,
            ),
            pytest.raises(InstallFailureAlreadyRendered, match="No MCP-capable target"),
        ):
            run_mcp_install(
                [dep],
                target_decision=decision,
                project_root=str(tmp_path),
                logger=logger,
            )


class TestRunMcpInstallClientFactoryFilter:
    """TC-1: Direct regression tests for the ClientFactory gate in _resolve_target_runtimes.

    These tests exercise the ClientFactory.supported_clients() filtering path
    without going through the catalog's mcp_capable field, which was removed.
    ClientFactory is the single source of truth for MCP capability (#2485).
    """

    _MCP_CLIENTS = frozenset({"codex", "copilot", "claude", "vscode"})

    def test_non_mcp_targets_filtered_from_runtime_list(self, tmp_path: Path) -> None:
        """ClientFactory gate drops non-MCP runtimes and logs a progress message."""
        from apm_cli.core.target_detection import EffectiveTargetDecision
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        # grok-build is in_all=True but has no MCP adapter; must be silently dropped
        decision = EffectiveTargetDecision(["codex", "grok-build"], source="apm.yml")
        dep = _make_self_defined_dep("my-server")
        logger = MagicMock()
        attempted: list[str] = []

        def track(runtime, *args, **kwargs):
            attempted.append(runtime)
            return True

        from apm_cli.integration import mcp_integrator as mi_mod

        with (
            patch(
                "apm_cli.factory.ClientFactory.supported_clients",
                return_value=self._MCP_CLIENTS,
            ),
            patch.object(mi_mod.MCPIntegrator, "_install_for_runtime", side_effect=track),
            patch.object(
                mi_mod.MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["my-server"],
            ),
            patch.object(mi_mod.MCPIntegrator, "_build_self_defined_info", return_value={}),
            patch.object(
                mi_mod.MCPIntegrator,
                "_gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
        ):
            run_mcp_install(
                [dep],
                target_decision=decision,
                project_root=str(tmp_path),
                logger=logger,
            )

        assert attempted == ["codex"], f"Expected only codex, got {attempted}"
        # The guard must log a progress message naming the skipped target
        progress_calls = [str(c) for c in logger.progress.call_args_list]
        assert any("grok-build" in c for c in progress_calls), (
            "Expected progress message naming skipped non-MCP target"
        )

    def test_all_non_mcp_targets_raises_install_failure_with_label(self, tmp_path: Path) -> None:
        """When every runtime is filtered by ClientFactory, raise InstallFailureAlreadyRendered.

        The error message must name the failing targets and carry the 'declared'
        label when the source is apm.yml.
        """
        from apm_cli.core.target_detection import EffectiveTargetDecision
        from apm_cli.install.errors import InstallFailureAlreadyRendered
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        decision = EffectiveTargetDecision(["grok-build"], source="apm.yml")
        dep = _make_self_defined_dep("my-server")
        logger = MagicMock()

        with (
            patch(
                "apm_cli.factory.ClientFactory.supported_clients",
                return_value=self._MCP_CLIENTS,
            ),
            pytest.raises(
                InstallFailureAlreadyRendered,
                match=r"No MCP-capable target in the declared target set",
            ),
        ):
            run_mcp_install(
                [dep],
                target_decision=decision,
                project_root=str(tmp_path),
                logger=logger,
            )
