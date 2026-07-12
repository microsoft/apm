from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.integration.mcp_integrator import MCPIntegrator, _is_vscode_available


def _make_dep(
    *,
    name: str = "server",
    is_self_defined: bool = False,
    transport: str = "stdio",
    command: str | None = None,
    args: list[str] | dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    tools: list[str] | None = None,
) -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_self_defined = is_self_defined
    dep.transport = transport
    dep.command = command
    dep.args = args
    dep.env = env
    dep.url = url
    dep.headers = headers
    dep.tools = tools
    dep.package = None
    dep.version = None
    dep.registry = False if is_self_defined else None
    dep.to_dict.return_value = {"name": name, "transport": transport}
    return dep


def _make_lock_dep(repo_url: str, *, depth: int = 1, virtual_path: str | None = None) -> MagicMock:
    dep = MagicMock()
    dep.repo_url = repo_url
    dep.depth = depth
    dep.virtual_path = virtual_path
    return dep


def _write_json(path: Path, content: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


class TestIsVscodeAvailable:
    def test_returns_true_when_code_cli_exists(self, tmp_path: Path) -> None:
        with patch("apm_cli.integration.mcp_integrator.shutil.which", return_value="/usr/bin/code"):
            assert _is_vscode_available(tmp_path) is True

    def test_returns_true_when_vscode_directory_exists(self, tmp_path: Path) -> None:
        (tmp_path / ".vscode").mkdir()

        with patch("apm_cli.integration.mcp_integrator.shutil.which", return_value=None):
            assert _is_vscode_available(tmp_path) is True

    def test_returns_false_when_no_cli_and_no_directory(self, tmp_path: Path) -> None:
        with patch("apm_cli.integration.mcp_integrator.shutil.which", return_value=None):
            assert _is_vscode_available(tmp_path) is False

    def test_uses_current_working_directory_when_project_root_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".vscode").mkdir()

        with (
            patch("apm_cli.integration.mcp_integrator.shutil.which", return_value=None),
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
        ):
            assert _is_vscode_available() is True


class TestCollectTransitive:
    def test_delegates_to_current_config_owner(self, tmp_path: Path) -> None:
        expected = [_make_dep(name="server")]
        modules_root = tmp_path / "apm_modules"
        lock_path = tmp_path / "apm.lock.yaml"
        logger = MagicMock()
        diagnostics = MagicMock()

        with patch(
            "apm_cli.integration.mcp_integrator._collect_transitive_compat",
            return_value=expected,
        ) as collect:
            result = MCPIntegrator.collect_transitive(
                modules_root,
                lock_path,
                trust_private=True,
                logger=logger,
                diagnostics=diagnostics,
            )

        assert result == expected
        collect.assert_called_once_with(
            modules_root,
            lock_path,
            True,
            logger=logger,
            diagnostics=diagnostics,
        )


class TestDeduplicate:
    def test_deduplicates_named_objects_by_first_occurrence(self) -> None:
        first = _make_dep(name="server-a")
        second = _make_dep(name="server-a")
        third = _make_dep(name="server-b")

        result = MCPIntegrator.deduplicate([first, second, third])

        assert result == [first, third]

    def test_deduplicates_named_dicts(self) -> None:
        result = MCPIntegrator.deduplicate(
            [
                {"name": "server-a"},
                {"name": "server-a"},
                {"name": "server-b"},
            ]
        )

        assert result == [{"name": "server-a"}, {"name": "server-b"}]

    def test_deduplicates_strings_by_value(self) -> None:
        result = MCPIntegrator.deduplicate(["server-a", "server-a", "server-b"])

        assert result == ["server-a", "server-b"]

    def test_deduplicates_mixed_name_sources_together(self) -> None:
        dep = _make_dep(name="server-a")

        result = MCPIntegrator.deduplicate(["server-a", dep, {"name": "server-a"}])

        assert result == ["server-a"]

    def test_empty_name_dicts_are_deduplicated_by_dict_equality(self) -> None:
        first = {"name": ""}
        second = {"name": ""}

        result = MCPIntegrator.deduplicate([first, second])

        assert result == [first]

    def test_deduplicates_same_unnamed_object_by_identity(self) -> None:
        dep = {"registry": "internal"}

        result = MCPIntegrator.deduplicate([dep, dep])

        assert result == [dep]

    def test_preserves_order(self) -> None:
        first = _make_dep(name="a")
        second = _make_dep(name="b")
        third = _make_dep(name="a")

        result = MCPIntegrator.deduplicate([first, second, third])

        assert result == [first, second]


class TestBuildSelfDefinedInfo:
    def test_builds_stdio_info_with_raw_command_args_and_env(self) -> None:
        dep = _make_dep(
            name="stdio-server",
            is_self_defined=True,
            transport="stdio",
            command="python",
            args=["-m", "srv"],
            env={"TOKEN": "secret"},
        )

        info = MCPIntegrator._build_self_defined_info(dep)

        assert info["name"] == "stdio-server"
        assert info["_raw_stdio"] == {
            "command": "python",
            "args": ["-m", "srv"],
            "env": {"TOKEN": "secret"},
        }
        assert info["packages"][0]["runtime_hint"] == "python"
        assert info["packages"][0]["runtime_arguments"] == [
            {"is_required": True, "value_hint": "-m"},
            {"is_required": True, "value_hint": "srv"},
        ]

    def test_builds_http_remote_with_headers(self) -> None:
        dep = _make_dep(
            name="http-server",
            is_self_defined=True,
            transport="http",
            url="https://example.com/mcp",
            headers={"X-Token": "abc"},
        )

        info = MCPIntegrator._build_self_defined_info(dep)

        assert info["remotes"] == [
            {
                "transport_type": "http",
                "url": "https://example.com/mcp",
                "headers": [{"name": "X-Token", "value": "abc"}],
            }
        ]
        assert "packages" not in info

    def test_builds_sse_remote(self) -> None:
        dep = _make_dep(
            name="sse-server",
            is_self_defined=True,
            transport="sse",
            url="https://example.com/sse",
        )

        info = MCPIntegrator._build_self_defined_info(dep)

        assert info["remotes"][0]["transport_type"] == "sse"
        assert info["remotes"][0]["url"] == "https://example.com/sse"

    def test_builds_runtime_arguments_from_dict_args(self) -> None:
        dep = _make_dep(
            name="dict-server",
            is_self_defined=True,
            transport="stdio",
            args={"mode": "fast", "port": "8080"},
        )

        info = MCPIntegrator._build_self_defined_info(dep)

        assert info["packages"][0]["runtime_arguments"] == [
            {"is_required": True, "value_hint": "fast"},
            {"is_required": True, "value_hint": "8080"},
        ]

    def test_uses_name_as_default_command_for_stdio(self) -> None:
        dep = _make_dep(name="fallback-server", is_self_defined=True, transport="stdio")

        info = MCPIntegrator._build_self_defined_info(dep)

        assert info["_raw_stdio"]["command"] == "fallback-server"
        assert info["packages"][0]["runtime_hint"] == "fallback-server"

    def test_embeds_tools_override(self) -> None:
        dep = _make_dep(
            name="tools-server",
            is_self_defined=True,
            transport="stdio",
            tools=["inspect", "deploy"],
        )

        info = MCPIntegrator._build_self_defined_info(dep)

        assert info["_apm_tools_override"] == ["inspect", "deploy"]


class TestRemoveStale:
    def test_empty_stale_names_is_noop(self, tmp_path: Path) -> None:
        with patch("apm_cli.factory.ClientFactory.supported_clients") as mock_supported:
            MCPIntegrator.remove_stale(set(), project_root=tmp_path)

        mock_supported.assert_not_called()

    def test_runtime_argument_limits_cleanup_to_single_runtime(self, tmp_path: Path) -> None:
        vscode = _write_json(
            tmp_path / ".vscode" / "mcp.json",
            {"servers": {"stale": {"command": "echo"}}},
        )
        cursor = _write_json(
            tmp_path / ".cursor" / "mcp.json",
            {"mcpServers": {"stale": {"command": "echo"}}},
        )

        with patch(
            "apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode", "cursor"]
        ):
            MCPIntegrator.remove_stale({"stale"}, runtime="vscode", project_root=tmp_path)

        assert "stale" not in json.loads(vscode.read_text(encoding="utf-8"))["servers"]
        assert "stale" in json.loads(cursor.read_text(encoding="utf-8"))["mcpServers"]

    def test_exclude_prevents_runtime_cleanup(self, tmp_path: Path) -> None:
        vscode = _write_json(
            tmp_path / ".vscode" / "mcp.json",
            {"servers": {"stale": {"command": "echo"}}},
        )
        cursor = _write_json(
            tmp_path / ".cursor" / "mcp.json",
            {"mcpServers": {"stale": {"command": "echo"}}},
        )

        with patch(
            "apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode", "cursor"]
        ):
            MCPIntegrator.remove_stale({"stale"}, exclude="vscode", project_root=tmp_path)

        assert "stale" in json.loads(vscode.read_text(encoding="utf-8"))["servers"]
        assert "stale" not in json.loads(cursor.read_text(encoding="utf-8"))["mcpServers"]

    def test_user_scope_filters_out_runtimes_without_user_support(self, tmp_path: Path) -> None:
        vscode = _write_json(
            tmp_path / ".vscode" / "mcp.json",
            {"servers": {"stale": {"command": "echo"}}},
        )
        unsupported_client = MagicMock()
        unsupported_client.supports_user_scope = False

        with (
            patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode"]),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=unsupported_client),
        ):
            MCPIntegrator.remove_stale(
                {"stale"},
                runtime="vscode",
                project_root=tmp_path,
                scope=InstallScope.USER,
            )

        assert "stale" in json.loads(vscode.read_text(encoding="utf-8"))["servers"]

    def test_removes_from_vscode_and_logs_progress(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / ".vscode" / "mcp.json",
            {"servers": {"stale": {"command": "echo"}, "keep": {"command": "cat"}}},
        )
        logger = MagicMock()

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode"]):
            MCPIntegrator.remove_stale(
                {"stale"}, runtime="vscode", project_root=tmp_path, logger=logger
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["servers"]
        assert "keep" in config["servers"]
        logger.progress.assert_called_once()

    def test_vscode_parse_error_is_ignored(self, tmp_path: Path) -> None:
        bad_path = tmp_path / ".vscode" / "mcp.json"
        bad_path.parent.mkdir(parents=True)
        bad_path.write_text("{not json", encoding="utf-8")

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode"]):
            MCPIntegrator.remove_stale({"stale"}, runtime="vscode", project_root=tmp_path)

        assert bad_path.read_text(encoding="utf-8") == "{not json"

    def test_removes_from_copilot_config(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / ".copilot" / "mcp-config.json",
            {"mcpServers": {"stale": {"command": "echo"}, "keep": {"command": "cat"}}},
        )

        with (
            patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["copilot"]),
            patch("apm_cli.integration.mcp_integrator.Path.home", return_value=tmp_path),
            patch("apm_cli.integration.mcp_integrator._rich_success") as mock_success,
        ):
            MCPIntegrator.remove_stale({"stale"}, runtime="copilot", project_root=tmp_path)

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["mcpServers"]
        assert "keep" in config["mcpServers"]
        mock_success.assert_called_once()

    def test_removes_from_codex_config(self, tmp_path: Path) -> None:
        import toml

        config_path = tmp_path / "codex.toml"
        config_path.write_text(
            toml.dumps(
                {
                    "mcp_servers": {
                        "stale": {"command": "echo"},
                        "keep": {"command": "cat"},
                    }
                }
            ),
            encoding="utf-8",
        )
        client = MagicMock()
        client.get_config_path.return_value = str(config_path)

        with (
            patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["codex"]),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=client),
            patch("apm_cli.integration.mcp_integrator._rich_success") as mock_success,
        ):
            MCPIntegrator.remove_stale({"stale"}, runtime="codex", project_root=tmp_path)

        config = toml.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["mcp_servers"]
        assert "keep" in config["mcp_servers"]
        mock_success.assert_called_once()

    def test_removes_from_cursor_config(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / ".cursor" / "mcp.json",
            {"mcpServers": {"stale": {"command": "echo"}, "keep": {"command": "cat"}}},
        )

        with (
            patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["cursor"]),
            patch("apm_cli.integration.mcp_integrator._rich_success") as mock_success,
        ):
            MCPIntegrator.remove_stale({"stale"}, runtime="cursor", project_root=tmp_path)

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["mcpServers"]
        assert "keep" in config["mcpServers"]
        mock_success.assert_called_once()

    def test_opencode_requires_marker_directory(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / "opencode.json",
            {"mcp": {"stale": {"command": "echo"}}},
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["opencode"]):
            MCPIntegrator.remove_stale({"stale"}, runtime="opencode", project_root=tmp_path)

        assert "stale" in json.loads(config_path.read_text(encoding="utf-8"))["mcp"]

    def test_removes_from_opencode_config(self, tmp_path: Path) -> None:
        (tmp_path / ".opencode").mkdir()
        config_path = _write_json(
            tmp_path / "opencode.json",
            {"mcp": {"stale": {"command": "echo"}, "keep": {"command": "cat"}}},
        )
        logger = MagicMock()

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["opencode"]):
            MCPIntegrator.remove_stale(
                {"stale"}, runtime="opencode", project_root=tmp_path, logger=logger
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["mcp"]
        assert "keep" in config["mcp"]
        logger.progress.assert_called_once()

    def test_removes_from_windsurf_config(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / ".codeium" / "windsurf" / "mcp_config.json",
            {"mcpServers": {"stale": {"command": "echo"}, "keep": {"command": "cat"}}},
        )

        with (
            patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["windsurf"]),
            patch("apm_cli.integration.mcp_integrator.Path.home", return_value=tmp_path),
            patch("apm_cli.integration.mcp_integrator._rich_success") as mock_success,
        ):
            MCPIntegrator.remove_stale({"stale"}, runtime="windsurf", project_root=tmp_path)

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["mcpServers"]
        assert "keep" in config["mcpServers"]
        mock_success.assert_called_once()

    def test_removes_from_gemini_config_with_logger(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / ".gemini" / "settings.json",
            {"mcpServers": {"stale": {"command": "echo"}, "keep": {"command": "cat"}}},
        )
        logger = MagicMock()

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["gemini"]):
            MCPIntegrator.remove_stale(
                {"stale"}, runtime="gemini", project_root=tmp_path, logger=logger
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["mcpServers"]
        assert "keep" in config["mcpServers"]
        logger.progress.assert_called_once()

    def test_removes_from_claude_project_config_and_logs_scope_notice(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        config_path = _write_json(
            tmp_path / ".mcp.json",
            {"mcpServers": {"stale": {"command": "echo"}, "keep": {"command": "cat"}}},
        )
        logger = MagicMock()

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["claude"]):
            MCPIntegrator.remove_stale(
                {"stale"},
                runtime="claude",
                project_root=tmp_path,
                logger=logger,
                scope=None,
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["mcpServers"]
        assert "keep" in config["mcpServers"]
        messages = [call.args[0] for call in logger.progress.call_args_list]
        assert any("scope unspecified" in message for message in messages)
        assert any(
            "Removed stale MCP server 'stale' from .mcp.json" in message for message in messages
        )

    def test_claude_project_cleanup_requires_marker_directory(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / ".mcp.json",
            {"mcpServers": {"stale": {"command": "echo"}}},
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["claude"]):
            MCPIntegrator.remove_stale({"stale"}, runtime="claude", project_root=tmp_path)

        assert "stale" in json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]

    def test_removes_from_claude_user_config_at_user_scope(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / ".claude.json",
            {"mcpServers": {"stale": {"command": "echo"}, "keep": {"command": "cat"}}},
        )
        client = MagicMock()
        client.supports_user_scope = True
        logger = MagicMock()

        with (
            patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["claude"]),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=client),
            patch("apm_cli.integration.mcp_integrator.Path.home", return_value=tmp_path),
        ):
            MCPIntegrator.remove_stale(
                {"stale"},
                runtime="claude",
                project_root=tmp_path,
                logger=logger,
                scope=InstallScope.USER,
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale" not in config["mcpServers"]
        assert "keep" in config["mcpServers"]
        logger.progress.assert_called_once()

    def test_expands_full_reference_to_short_name(self, tmp_path: Path) -> None:
        config_path = _write_json(
            tmp_path / ".vscode" / "mcp.json",
            {"servers": {"github-mcp-server": {"command": "echo"}}},
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode"]):
            MCPIntegrator.remove_stale(
                {"io.github.github/github-mcp-server"},
                runtime="vscode",
                project_root=tmp_path,
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "github-mcp-server" not in config["servers"]
