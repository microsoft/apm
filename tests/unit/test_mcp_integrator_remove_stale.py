"""Characterisation tests for MCPIntegrator.remove_stale()."""

import os
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

    def test_remove_stale_antigravity_uses_user_scope_config(self, tmp_path):
        """Global cleanup must mirror Antigravity's install-time path."""
        import json

        from apm_cli.core.scope import InstallScope
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        config_path = tmp_path / "home" / ".gemini" / "config" / "mcp_config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "stale-server": {"command": "old"},
                        "keep-server": {"command": "keep"},
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            MCPIntegrator.remove_stale(
                stale_names={"stale-server"},
                runtime="antigravity",
                project_root=tmp_path,
                scope=InstallScope.USER,
                logger=MagicMock(),
            )

        servers = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "stale-server" not in servers
        assert servers["keep-server"]["command"] == "keep"


@pytest.mark.windows_compat
def test_clean_json_atomic_failure_preserves_original_and_mode(tmp_path):
    """A failed stale cleanup must not truncate credential-bearing JSON."""
    import json
    import os
    import stat

    from apm_cli.install.errors import RequiredIntegrationError
    from apm_cli.integration.mcp_integrator import _clean_json_mcp_config

    config_path = tmp_path / "mcp_config.json"
    original = json.dumps(
        {
            "theme": "dark",
            "mcpServers": {
                "stale-server": {"command": "old"},
                "user-authored": {"command": "keep"},
            },
        },
        separators=(",", ":"),
    ).encode()
    config_path.write_bytes(original)
    os.chmod(config_path, 0o600)

    with (
        patch(
            "apm_cli.utils.atomic_io._replace_atomic_file",
            side_effect=OSError("simulated crash"),
        ),
        pytest.raises(RequiredIntegrationError, match="simulated crash"),
    ):
        _clean_json_mcp_config(
            config_path,
            {"stale-server"},
            MagicMock(),
            "test MCP config",
            fail_on_write_error=True,
        )

    assert config_path.read_bytes() == original
    if hasattr(os, "fchmod"):
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob("apm-atomic-*")) == []


@pytest.mark.windows_compat
def test_clean_json_strict_malformed_shape_preserves_original(tmp_path):
    from apm_cli.install.errors import RequiredIntegrationError
    from apm_cli.integration.mcp_integrator import _clean_json_mcp_config

    config_path = tmp_path / "mcp.json"
    original = b'{"mcpServers":["not-a-mapping"]}'
    config_path.write_bytes(original)

    with pytest.raises(RequiredIntegrationError, match="mcpServers must be a mapping"):
        _clean_json_mcp_config(
            config_path,
            {"stale"},
            MagicMock(),
            "test MCP config",
            fail_on_write_error=True,
        )

    assert config_path.read_bytes() == original


@pytest.mark.windows_compat
@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_remove_stale_hermes_preserves_unrelated_yaml(tmp_path, monkeypatch, mode):
    import os
    import stat

    import yaml

    from apm_cli.integration.mcp_integrator import MCPIntegrator

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openai\nmcp_servers:\n  stale:\n    command: old\n"
        "  user-authored:\n    command: keep\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        config_path.chmod(mode)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    MCPIntegrator.remove_stale(
        {"stale"},
        runtime="hermes",
        logger=MagicMock(),
        fail_on_write_error=True,
    )

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["model"]["provider"] == "openai"
    assert "stale" not in config["mcp_servers"]
    assert config["mcp_servers"]["user-authored"]["command"] == "keep"
    if os.name != "nt":
        assert stat.S_IMODE(config_path.stat().st_mode) == mode


@pytest.mark.windows_compat
@pytest.mark.parametrize("failure", ["malformed", "atomic"])
def test_remove_stale_hermes_failure_preserves_live_bytes(tmp_path, monkeypatch, failure):
    import os
    import stat

    from apm_cli.install.errors import RequiredIntegrationError
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    original = (
        b"mcp_servers: [not-a-mapping]\nSECRET_TOKEN: keep\n"
        if failure == "malformed"
        else b"mcp_servers:\n  stale:\n    command: old\nSECRET_TOKEN: keep\n"
    )
    config_path.write_bytes(original)
    if os.name != "nt":
        config_path.chmod(0o640)
    original_stat = config_path.stat()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    replace = (
        patch(
            "apm_cli.utils.atomic_io._replace_atomic_file",
            side_effect=OSError("simulated crash"),
        )
        if failure == "atomic"
        else patch("apm_cli.utils.atomic_io._replace_atomic_file")
    )
    with replace, pytest.raises(RequiredIntegrationError, match=r"Hermes config\.yaml"):
        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="hermes",
            logger=MagicMock(),
            fail_on_write_error=True,
        )

    assert config_path.read_bytes() == original
    preserved_stat = config_path.stat()
    assert stat.S_IMODE(preserved_stat.st_mode) == stat.S_IMODE(original_stat.st_mode)
    assert preserved_stat.st_uid == original_stat.st_uid
    assert preserved_stat.st_gid == original_stat.st_gid
    assert list(hermes_home.glob("apm-atomic-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires elevated Windows rights")
def test_remove_stale_rejects_symlinked_hermes_config(tmp_path, monkeypatch):
    """Strict cleanup must preserve both a config symlink and its target."""
    from apm_cli.install.errors import RequiredIntegrationError
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    target = tmp_path / "user-config.yaml"
    original = b"mcp_servers:\n  stale:\n    command: keep\n"
    target.write_bytes(original)
    config_path = hermes_home / "config.yaml"
    config_path.symlink_to(target)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(RequiredIntegrationError, match="symlinked MCP config"):
        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="hermes",
            logger=MagicMock(),
            fail_on_write_error=True,
        )

    assert config_path.is_symlink()
    assert target.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires elevated Windows rights")
def test_remove_stale_rejects_symlinked_hermes_ancestor(tmp_path, monkeypatch):
    """Strict cleanup must not traverse a symlinked config directory."""
    from apm_cli.install.errors import RequiredIntegrationError
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    target_home = tmp_path / "real-hermes"
    target_home.mkdir()
    config_path = target_home / "config.yaml"
    original = b"mcp_servers:\n  stale:\n    command: keep\n"
    config_path.write_bytes(original)
    hermes_home = tmp_path / ".hermes"
    hermes_home.symlink_to(target_home, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(RequiredIntegrationError, match="symlinked MCP config"):
        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="hermes",
            logger=MagicMock(),
            fail_on_write_error=True,
        )

    assert hermes_home.is_symlink()
    assert config_path.read_bytes() == original


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

    @pytest.mark.windows_compat
    def test_strict_non_table_mcp_servers_fails_closed(self, tmp_path):
        from apm_cli.install.errors import RequiredIntegrationError
        from apm_cli.integration.mcp_integrator import _clean_toml_mcp_config

        config_path = tmp_path / "config.toml"
        original = 'mcp_servers = ["stale-server"]\n'
        config_path.write_text(original, encoding="utf-8")

        with pytest.raises(RequiredIntegrationError, match="MCP cleanup failed"):
            _clean_toml_mcp_config(
                config_path,
                {"stale-server"},
                "Codex CLI config",
                use_rich=False,
                fail_on_write_error=True,
            )

        assert config_path.read_text(encoding="utf-8") == original

    def test_skips_non_utf8_config_without_rewriting(self, tmp_path):
        from apm_cli.integration.mcp_integrator import _clean_toml_mcp_config

        config_path = tmp_path / "config.toml"
        original = b"\xff"
        config_path.write_bytes(original)

        removed = _clean_toml_mcp_config(
            config_path,
            {"stale-server"},
            "Codex CLI config",
            use_rich=False,
        )

        assert removed == 0
        assert config_path.read_bytes() == original


class TestRemoveStaleIntelliJ:
    """Fixture-backed coverage for the JetBrains (intellij) stale-cleanup block."""

    def test_remove_stale_intellij_removes_from_servers_key(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        home = tmp_path / "home"
        config_dir = home / ".config" / "github-copilot" / "intellij"
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
                "apm_cli.adapters.client.intellij.IntelliJClientAdapter.get_config_path",
                return_value=str(mcp_json),
            ),
            patch(
                "apm_cli.adapters.client.intellij.IntelliJClientAdapter.get_legacy_config_path",
                return_value=None,
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

    def test_remove_stale_intellij_fails_when_config_path_is_unavailable(self, tmp_path):
        """A misconfigured path must fail instead of claiming cleanup success."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        from apm_cli.utils.path_security import PathTraversalError

        logger = MagicMock()
        logger.verbose = False
        with (
            patch(
                "apm_cli.adapters.client.intellij._intellij_config_root",
                side_effect=PathTraversalError("LOCALAPPDATA unset"),
            ),
            pytest.raises(PathTraversalError, match="LOCALAPPDATA"),
        ):
            MCPIntegrator.remove_stale(
                stale_names={"stale-server"},
                runtime="intellij",
                logger=logger,
            )

    def test_remove_stale_intellij_fails_on_malformed_json(self, tmp_path):
        """Malformed user config fails closed instead of being overwritten."""
        from apm_cli.adapters.client.intellij import IntelliJConfigError
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        config_path = tmp_path / "config" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        original = b"{invalid\n"
        config_path.write_bytes(original)
        with (
            patch(
                "apm_cli.adapters.client.intellij.IntelliJClientAdapter.get_config_path",
                return_value=str(config_path),
            ),
            patch(
                "apm_cli.adapters.client.intellij.IntelliJClientAdapter.get_legacy_config_path",
                return_value=None,
            ),
            pytest.raises(IntelliJConfigError, match="malformed JSON"),
        ):
            MCPIntegrator.remove_stale(
                stale_names={"stale-server"},
                runtime="intellij",
                logger=MagicMock(),
            )

        assert config_path.read_bytes() == original

    def test_remove_stale_intellij_resolves_output_path_once(self):
        """Multiple removal messages reuse one validated config path."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        client = MagicMock()
        client.remove_managed_servers.return_value = {"first", "second"}
        client.get_config_path.return_value = "/home/user/.config/github-copilot/intellij/mcp.json"
        with (
            patch("apm_cli.factory.ClientFactory.create_client", return_value=client),
            patch("apm_cli.integration.mcp_integrator._rich_success"),
        ):
            MCPIntegrator.remove_stale(
                stale_names={"first", "second"},
                runtime="intellij",
                logger=MagicMock(),
            )

        client.get_config_path.assert_called_once_with()
