"""Characterisation tests for MCPIntegrator.remove_stale()."""

from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch):
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


class TestRemoveStaleCharacterisation:
    def test_remove_stale_no_logger(self):
        """remove_stale() with logger=None should not crash."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.remove_stale(stale_names=set())
        assert result is None

    def test_remove_stale_with_logger(self):
        """remove_stale() with logger should use it."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.remove_stale(stale_names=set(), logger=logger)
        assert result is None

    def test_remove_stale_empty_names(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.remove_stale(stale_names=set())
        assert result is None

    def test_remove_stale_with_runtime(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            runtime="vscode",
        )
        assert result is None

    def test_remove_stale_returns_none(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            logger=logger,
        )
        assert result is None

    def test_remove_stale_with_scope(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            logger=logger,
            scope=None,
        )
        assert result is None

    def test_remove_stale_verbose(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = True
        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            logger=logger,
        )
        assert result is None

    def test_remove_stale_with_exclude(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            exclude="vscode",
            logger=logger,
        )
        assert result is None


class TestCleanCodexToml:
    def test_preserves_windows_literal_keys_while_removing_stale_server(self, tmp_path):
        import tomlkit

        from apm_cli.integration.mcp_integrator import _clean_toml_mcp_config

        config_path = tmp_path / "config.toml"
        unrelated = (
            "[projects.'c:\\src\\projectdir\\subdir']\n"
            'trust_level = "trusted"\n'
            "\n"
            "[desktop.open-in-target-preferences.perPath]\n"
            "'C:\\Users\\me\\Documents\\Playground' = \"fileManager\"\n"
        )
        config_path.write_text(
            unrelated
            + "\n"
            + "[mcp_servers.stale-server]\n"
            + 'command = "old"\n'
            + "\n"
            + "[mcp_servers.keep-server]\n"
            + 'command = "keep"\n',
            encoding="utf-8",
        )

        removed = _clean_toml_mcp_config(
            config_path,
            {"stale-server"},
            "Codex CLI config",
            use_rich=False,
        )

        updated = config_path.read_text(encoding="utf-8")
        assert removed == 1
        assert unrelated in updated
        parsed = tomlkit.parse(updated)
        assert "stale-server" not in parsed["mcp_servers"]
        assert parsed["mcp_servers"]["keep-server"]["command"] == "keep"

    def test_skips_non_table_mcp_servers_without_rewriting(self, tmp_path):
        from apm_cli.integration.mcp_integrator import _clean_toml_mcp_config

        config_path = tmp_path / "config.toml"
        original = 'mcp_servers = ["stale-server"]\n'
        config_path.write_text(original, encoding="utf-8")

        removed = _clean_toml_mcp_config(
            config_path,
            {"stale-server"},
            "Codex CLI config",
            use_rich=False,
        )

        assert removed == 0
        assert config_path.read_text(encoding="utf-8") == original


class TestRemoveStaleIntelliJ:
    """Fixture-backed coverage for the JetBrains (intellij) stale-cleanup block."""

    def test_remove_stale_intellij_removes_from_servers_key(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        home = tmp_path / "home"
        config_dir = home / ".local" / "share" / "github-copilot" / "intellij"
        config_dir.mkdir(parents=True)
        mcp_json = config_dir / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "servers": {
                        "stale-server": {"command": "node"},
                        "keep-server": {"command": "node"},
                    }
                }
            )
        )

        logger = MagicMock()
        logger.verbose = False
        with (
            patch(
                "apm_cli.adapters.client.intellij._intellij_config_dir",
                return_value=config_dir,
            ),
            patch("pathlib.Path.home", return_value=home),
        ):
            MCPIntegrator.remove_stale(
                stale_names={"stale-server"},
                runtime="intellij",
                logger=logger,
            )

        data = json.loads(mcp_json.read_text())
        # Stale entry removed from the 'servers' key; unrelated entry preserved.
        assert "stale-server" not in data["servers"]
        assert "keep-server" in data["servers"]

    def test_remove_stale_intellij_skips_when_localappdata_unset(self, tmp_path):
        """A misconfigured env (PathTraversalError) must not crash cleanup."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        from apm_cli.utils.path_security import PathTraversalError

        logger = MagicMock()
        logger.verbose = False
        with patch(
            "apm_cli.adapters.client.intellij._intellij_config_dir",
            side_effect=PathTraversalError("LOCALAPPDATA unset"),
        ):
            result = MCPIntegrator.remove_stale(
                stale_names={"stale-server"},
                runtime="intellij",
                logger=logger,
            )
        assert result is None
