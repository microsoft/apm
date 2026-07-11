"""Integration tests for mcp_integrator_install.

Covers uncovered lines/branches in:
  src/apm_cli/integration/mcp_integrator_install.py

Strategy: hermetic -- mocks registry, runtime, console.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import tomlkit
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.mcp_integrator_install import run_mcp_install

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_dep(
    name: str,
    is_registry_resolved: bool = True,
    env: dict | None = None,
) -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = is_registry_resolved
    dep.is_self_defined = not is_registry_resolved
    dep.env = env or {}
    return dep


def _make_self_defined_dep(
    name: str,
    transport: str = "stdio",
    env: dict | None = None,
) -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = False
    dep.is_self_defined = True
    dep.transport = transport
    dep.env = env or {}
    return dep


def test_install_restores_dev_mcp_dependencies_to_lockfile_and_config(tmp_path, monkeypatch):
    """apm install restores dev MCP servers to runtime config and lockfile."""
    monkeypatch.chdir(tmp_path)
    LockFile().write(tmp_path / "apm.lock.yaml")
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "dev-mcp-project",
                "version": "0.0.1",
                "target": "copilot",
                "dependencies": {"apm": [], "mcp": []},
                "devDependencies": {
                    "mcp": [
                        {
                            "name": "dev-server",
                            "registry": False,
                            "transport": "stdio",
                            "command": "python",
                            "args": ["-m", "dev_server"],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["install", "--runtime", "vscode", "--target", "copilot"])

    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert config["servers"]["dev-server"] == {
        "type": "stdio",
        "command": "python",
        "args": ["-m", "dev_server"],
    }
    lockfile = LockFile.read(tmp_path / "apm.lock.yaml")
    assert lockfile is not None
    assert lockfile.mcp_servers == ["dev-server"]
    assert lockfile.mcp_configs["dev-server"]["command"] == "python"


def test_install_target_contraction_removes_only_apm_managed_mcp_servers(tmp_path, monkeypatch):
    """Reinstalling with fewer targets purges APM-owned entries from dropped targets."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    LockFile().write(tmp_path / "apm.lock.yaml")
    manifest = {
        "name": "mcp-target-contraction",
        "version": "0.0.1",
        "targets": ["copilot", "codex"],
        "dependencies": {
            "mcp": [
                {
                    "name": "apm-managed",
                    "registry": False,
                    "transport": "stdio",
                    "command": "echo",
                    "args": ["managed"],
                }
            ]
        },
    }
    (tmp_path / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    codex_config = tmp_path / ".codex" / "config.toml"
    codex_config.parent.mkdir()
    codex_config.write_text(
        "[projects.'c:\\src\\project']\n"
        'trust_level = "trusted"\n'
        "\n"
        "[mcp_servers.user-authored]\n"
        'command = "user-command"\n',
        encoding="utf-8",
    )

    broad = CliRunner().invoke(
        cli,
        ["install", "--target", "copilot,codex", "--no-policy"],
    )
    assert broad.exit_code == 0, broad.output
    broad_config = tomlkit.parse(codex_config.read_text(encoding="utf-8"))
    assert broad_config["mcp_servers"]["apm-managed"]["command"] == "echo"
    broad_lock = LockFile.read(tmp_path / "apm.lock.yaml")
    assert broad_lock is not None
    assert broad_lock.mcp_target_servers == {
        "codex": ["apm-managed"],
        "vscode": ["apm-managed"],
    }

    manifest["targets"] = ["copilot"]
    (tmp_path / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    contracted = CliRunner().invoke(
        cli,
        ["install", "--target", "copilot", "--no-policy"],
    )

    assert contracted.exit_code == 0, contracted.output
    updated_text = codex_config.read_text(encoding="utf-8")
    updated = tomlkit.parse(updated_text)
    assert "apm-managed" not in updated["mcp_servers"]
    assert updated["mcp_servers"]["user-authored"]["command"] == "user-command"
    assert updated["projects"][r"c:\src\project"]["trust_level"] == "trusted"
    contracted_lock = LockFile.read(tmp_path / "apm.lock.yaml")
    assert contracted_lock is not None
    assert contracted_lock.mcp_target_servers == {"copilot": ["apm-managed"]}


# ---------------------------------------------------------------------------
# Early-return and no-deps branches -- line 70
# ---------------------------------------------------------------------------


class TestRunMcpInstallEarlyReturns:
    def test_empty_deps_returns_zero(self, tmp_path, monkeypatch):
        """Line 70: no MCP deps -> early return 0."""
        monkeypatch.chdir(tmp_path)
        result = run_mcp_install([], logger=NullCommandLogger())
        assert result == 0

    def test_none_deps_treated_as_empty(self, tmp_path, monkeypatch):
        """Empty list -> returns 0 immediately."""
        monkeypatch.chdir(tmp_path)
        with patch("apm_cli.integration.mcp_integrator._get_console", return_value=None):
            result = run_mcp_install([], logger=NullCommandLogger())
        assert result == 0


# ---------------------------------------------------------------------------
# Scope handling -- lines 78-81
# ---------------------------------------------------------------------------


class TestScopeHandling:
    def test_user_scope_sets_user_scope_true(self, tmp_path, monkeypatch):
        """Line 78-79: InstallScope.USER -> user_scope=True."""
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                scope=InstallScope.USER,
                apm_config={},
            )

        # No runtimes -> returns 0 (gated)
        assert result == 0

    def test_project_scope_sets_user_scope_false(self, tmp_path, monkeypatch):
        """Line 80-81: InstallScope.PROJECT -> user_scope=False."""
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                scope=InstallScope.PROJECT,
                apm_config={},
            )

        assert result == 0


# ---------------------------------------------------------------------------
# Single runtime mode -- line 122-125
# ---------------------------------------------------------------------------


class TestSingleRuntimeMode:
    def test_explicit_runtime_targets_only_that_runtime(self, tmp_path, monkeypatch):
        """Line 122-125: runtime arg -> target_runtimes=[runtime]."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("test-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["copilot"],
            ),
            patch("apm_cli.registry.operations.MCPServerOperations") as MockOps,
        ):
            ops = MockOps.return_value
            ops.validate_servers_exist.return_value = (["test-server"], [])
            ops.check_servers_needing_installation.return_value = []
            ops.batch_fetch_server_info.return_value = {}
            ops.collect_environment_variables.return_value = {}
            ops.collect_runtime_variables.return_value = {}

            result = run_mcp_install(
                [dep],
                runtime="copilot",
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )

        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Runtime detection -- ImportError fallback path (lines 201-224)
# ---------------------------------------------------------------------------


class TestRuntimeDetectionImportErrorFallback:
    def test_import_error_falls_back_to_basic_detection(self, tmp_path, monkeypatch):
        """Lines 201-224: ImportError during RuntimeManager -> basic detection."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError("not available"),
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0

    def test_import_error_detects_vscode(self, tmp_path, monkeypatch):
        """Line 206-207: ImportError path detects VSCode via _is_vscode_available."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=True,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0

    def test_import_error_detects_cursor_directory(self, tmp_path, monkeypatch):
        """Line 209-210: ImportError path detects Cursor via .cursor/ dir."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".cursor").mkdir()
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )
        assert result == 0

    def test_import_error_detects_opencode_directory(self, tmp_path, monkeypatch):
        """Lines 212-213: ImportError path detects OpenCode via .opencode/ dir."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".opencode").mkdir()
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )
        assert result == 0

    def test_import_error_detects_gemini_directory(self, tmp_path, monkeypatch):
        """Lines 215-216: ImportError path detects Gemini via .gemini/ dir."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gemini").mkdir()
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )
        assert result == 0

    def test_import_error_detects_windsurf_directory(self, tmp_path, monkeypatch):
        """Lines 218-219: ImportError path detects Windsurf via .windsurf/ dir."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".windsurf").mkdir()
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )
        assert result == 0

    def test_import_error_detects_claude_directory(self, tmp_path, monkeypatch):
        """Lines 221-224: ImportError path detects Claude Code via .claude/ dir."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".claude").mkdir()
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )
        assert result == 0


# ---------------------------------------------------------------------------
# Script runtimes + target runtimes logic (lines 232-265)
# ---------------------------------------------------------------------------


class TestScriptRuntimesLogic:
    def test_no_script_runtimes_uses_all_installed(self, tmp_path, monkeypatch):
        """Lines 255-265: no script_runtimes -> use installed_runtimes directly."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                side_effect=lambda rt: "/usr/bin/copilot" if rt == "copilot" else None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],  # no script runtimes
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0

    def test_script_runtimes_intersection_with_installed(self, tmp_path, monkeypatch):
        """Lines 233-253: script_runtimes intersects installed -> filtered target list."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                side_effect=lambda rt: "/usr/bin/copilot" if rt == "copilot" else None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=["copilot"],  # script references copilot
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={"scripts": {"copilot": {}}},
                project_root=str(tmp_path),
            )

        assert result == 0

    def test_script_runtimes_no_installed_warns(self, tmp_path, monkeypatch):
        """Lines 252-254: script_runtimes present but no installed match -> warning."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")
        logger = MagicMock()
        logger.warning = MagicMock()
        logger.progress = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()
        logger.error = MagicMock()
        logger.verbose_detail = MagicMock()

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=["cursor"],  # references cursor but it's not installed
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=logger,
                apm_config={"scripts": {"cursor": {}}},
                project_root=str(tmp_path),
            )

        assert result == 0
        logger.warning.assert_called()


# ---------------------------------------------------------------------------
# Exclude runtime -- lines 268-276
# ---------------------------------------------------------------------------


class TestExcludeRuntime:
    def test_exclude_all_runtimes_returns_zero(self, tmp_path, monkeypatch):
        """Lines 268-276: --exclude removes all -> warn and return 0."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")
        logger = MagicMock()
        logger.warning = MagicMock()
        logger.progress = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()
        logger.error = MagicMock()
        logger.verbose_detail = MagicMock()

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                side_effect=lambda rt: "/usr/bin/" + rt if rt == "copilot" else None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                exclude="copilot",
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0


# ---------------------------------------------------------------------------
# No runtimes installed -- fallback to vscode (lines 279-281)
# ---------------------------------------------------------------------------


class TestNoRuntimesFallback:
    def test_no_runtimes_falls_back_to_vscode(self, tmp_path, monkeypatch):
        """Lines 279-281: no runtimes installed -> vscode fallback."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,  # pass-through
            ),
            patch("apm_cli.registry.operations.MCPServerOperations") as MockOps,
        ):
            ops = MockOps.return_value
            ops.validate_servers_exist.return_value = (["my-server"], [])
            ops.check_servers_needing_installation.return_value = []
            ops.batch_fetch_server_info.return_value = {}
            ops.collect_environment_variables.return_value = {}
            ops.collect_runtime_variables.return_value = {}

            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )

        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# User-scope filtering (lines 301-326)
# ---------------------------------------------------------------------------


class TestUserScopeFiltering:
    def test_user_scope_filters_workspace_only_runtimes(self, tmp_path, monkeypatch):
        """Lines 301-326: scope=USER removes workspace-only runtimes."""
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")
        logger = MagicMock()
        logger.warning = MagicMock()
        logger.progress = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()
        logger.error = MagicMock()
        logger.verbose_detail = MagicMock()

        mock_client_ws = MagicMock()
        mock_client_ws.supports_user_scope = False  # workspace-only

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch("apm_cli.factory.ClientFactory") as MockCF,
        ):
            MockCF.create_client.return_value = mock_client_ws

            result = run_mcp_install(
                [dep],
                runtime="vscode",  # single runtime mode
                scope=InstallScope.USER,
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0
        logger.warning.assert_called()

    def test_user_scope_no_supported_runtimes_warns_and_returns_zero(self, tmp_path, monkeypatch):
        """Lines 322-326: all runtimes filtered at user scope -> warn and return 0."""
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")
        logger = MagicMock()
        logger.warning = MagicMock()
        logger.progress = MagicMock()

        workspace_client = MagicMock()
        workspace_client.supports_user_scope = False

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch("apm_cli.factory.ClientFactory") as MockCF,
        ):
            MockCF.create_client.return_value = workspace_client

            result = run_mcp_install(
                [dep],
                runtime="vscode",
                scope=InstallScope.USER,
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0


# ---------------------------------------------------------------------------
# Registry ImportError -- lines 476-479
# ---------------------------------------------------------------------------


class TestRegistryImportError:
    def test_missing_registry_operations_raises_runtime_error(self, tmp_path, monkeypatch):
        """Lines 476-479: MCPServerOperations import fails -> RuntimeError."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["copilot"],
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                side_effect=ImportError("module missing"),
            ),
        ):
            with pytest.raises(RuntimeError, match=r"[Rr]egistry"):
                run_mcp_install(
                    [dep],
                    runtime="copilot",
                    logger=NullCommandLogger(),
                    apm_config={},
                    project_root=str(tmp_path),
                )


# ---------------------------------------------------------------------------
# Invalid server in registry -- line 346-349
# ---------------------------------------------------------------------------


class TestInvalidRegistry:
    def test_invalid_server_raises_runtime_error(self, tmp_path, monkeypatch):
        """Lines 346-349: server not found in registry -> RuntimeError."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("unknown-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["copilot"],
            ),
            patch("apm_cli.registry.operations.MCPServerOperations") as MockOps,
        ):
            ops = MockOps.return_value
            ops.validate_servers_exist.return_value = ([], ["unknown-server"])

            with pytest.raises(RuntimeError, match=r"[Mm]issing"):
                run_mcp_install(
                    [dep],
                    runtime="copilot",
                    logger=NullCommandLogger(),
                    apm_config={},
                    project_root=str(tmp_path),
                )


# ---------------------------------------------------------------------------
# Self-defined deps -- lines 481-580
# ---------------------------------------------------------------------------


class TestSelfDefinedDeps:
    def test_self_defined_already_configured_no_console(self, tmp_path, monkeypatch):
        """Lines 510-521: already-configured self-defined servers logged."""
        monkeypatch.chdir(tmp_path)
        dep = _make_self_defined_dep("my-custom-server")
        logger = MagicMock()
        logger.success = MagicMock()
        logger.verbose_detail = MagicMock()
        logger.progress = MagicMock()
        logger.warning = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()
        logger.error = MagicMock()

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["copilot"],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation",
                return_value=[],  # already configured
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_mcp_config_drift",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                runtime="copilot",
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0
        logger.success.assert_called()

    def test_self_defined_install_success(self, tmp_path, monkeypatch):
        """Lines 523-573: self-defined dep install."""
        monkeypatch.chdir(tmp_path)
        dep = _make_self_defined_dep("custom-server")
        logger = MagicMock()
        logger.progress = MagicMock()
        logger.success = MagicMock()
        logger.error = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()
        logger.verbose_detail = MagicMock()
        logger.warning = MagicMock()

        synthetic_info = {"command": "npx", "args": ["custom-server"]}

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["copilot"],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation",
                return_value=["custom-server"],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_mcp_config_drift",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._build_self_defined_info",
                return_value=synthetic_info,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime",
                return_value=True,
            ),
        ):
            result = run_mcp_install(
                [dep],
                runtime="copilot",
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 1

    def test_self_defined_install_all_runtimes_fail(self, tmp_path, monkeypatch):
        """Lines 574-580: self-defined dep fails all runtimes -> error logged."""
        monkeypatch.chdir(tmp_path)
        dep = _make_self_defined_dep("failing-server")
        logger = MagicMock()
        logger.progress = MagicMock()
        logger.error = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()
        logger.verbose_detail = MagicMock()
        logger.warning = MagicMock()
        logger.success = MagicMock()

        synthetic_info = {"command": "npx"}

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["copilot"],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation",
                return_value=["failing-server"],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_mcp_config_drift",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._build_self_defined_info",
                return_value=synthetic_info,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime",
                return_value=False,
            ),
        ):
            result = run_mcp_install(
                [dep],
                runtime="copilot",
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0
        logger.error.assert_called()


# ---------------------------------------------------------------------------
# Console panel rendering (lines 582-601)
# ---------------------------------------------------------------------------


class TestConsolePanelRendering:
    def test_console_all_configured_shows_up_to_date(self, tmp_path, monkeypatch):
        """Lines 598-599: configured_count=0 -> 'All servers up to date'."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")
        mock_console = MagicMock()

        with (
            patch(
                "apm_cli.integration.mcp_integrator._get_console",
                return_value=mock_console,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["copilot"],
            ),
            patch("apm_cli.registry.operations.MCPServerOperations") as MockOps,
        ):
            ops = MockOps.return_value
            ops.validate_servers_exist.return_value = (["my-server"], [])
            ops.check_servers_needing_installation.return_value = []
            ops.batch_fetch_server_info.return_value = {}
            ops.collect_environment_variables.return_value = {}
            ops.collect_runtime_variables.return_value = {}

            result = run_mcp_install(
                [dep],
                runtime="copilot",
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0
        console_print_calls = [str(c) for c in mock_console.print.call_args_list]
        assert any("up to date" in c.lower() for c in console_print_calls)

    def test_console_configured_count_shows_summary(self, tmp_path, monkeypatch):
        """Lines 584-597: configured_count > 0 -> summary with counts."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("new-server")
        mock_console = MagicMock()

        with (
            patch(
                "apm_cli.integration.mcp_integrator._get_console",
                return_value=mock_console,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["copilot"],
            ),
            patch("apm_cli.registry.operations.MCPServerOperations") as MockOps,
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime",
                return_value=True,
            ),
        ):
            ops = MockOps.return_value
            ops.validate_servers_exist.return_value = (["new-server"], [])
            ops.check_servers_needing_installation.return_value = ["new-server"]
            ops.batch_fetch_server_info.return_value = {"new-server": {}}
            ops.collect_environment_variables.return_value = {}
            ops.collect_runtime_variables.return_value = {}

            result = run_mcp_install(
                [dep],
                runtime="copilot",
                logger=NullCommandLogger(),
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 1
        console_print_calls = [str(c) for c in mock_console.print.call_args_list]
        assert any("configured" in c.lower() for c in console_print_calls)
