"""Unit tests for apm_cli.integration.mcp_integrator (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.component


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch):
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


# ---------------------------------------------------------------------------
# _is_vscode_available
# ---------------------------------------------------------------------------


class TestIsVscodeAvailable:
    def test_returns_true_when_code_on_path(self, tmp_path):
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        with patch("shutil.which", return_value="/usr/bin/code"):
            assert _is_vscode_available(tmp_path) is True

    def test_returns_true_when_vscode_dir_exists(self, tmp_path):
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        (tmp_path / ".vscode").mkdir()
        with patch("shutil.which", return_value=None):
            assert _is_vscode_available(tmp_path) is True

    def test_returns_false_when_neither(self, tmp_path):
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        with patch("shutil.which", return_value=None):
            assert _is_vscode_available(tmp_path) is False


# ---------------------------------------------------------------------------
# MCPIntegrator._build_self_defined_info
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCPIntegrator._apply_overlay
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCPIntegrator.deduplicate
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCPIntegrator.remove_stale
# ---------------------------------------------------------------------------


class TestRemoveStale:
    def test_noop_when_empty_stale_names(self, tmp_path):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        MCPIntegrator.remove_stale(set(), project_root=tmp_path, logger=logger)
        logger.progress.assert_not_called()

    def test_removes_from_vscode_mcp_json(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_json = vscode_dir / "mcp.json"
        mcp_json.write_text(
            json.dumps({"servers": {"stale-server": {"cmd": "x"}, "keep": {"cmd": "y"}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode"]):
            MCPIntegrator.remove_stale(
                {"stale-server"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "stale-server" not in result["servers"]
        assert "keep" in result["servers"]

    def test_removes_from_opencode_json(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        opencode_dir = tmp_path / ".opencode"
        opencode_dir.mkdir()
        cfg = tmp_path / "opencode.json"
        cfg.write_text(
            json.dumps({"mcp": {"stale-srv": {}, "keep-srv": {}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["opencode"]):
            MCPIntegrator.remove_stale(
                {"stale-srv"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(cfg.read_text(encoding="utf-8"))
        assert "stale-srv" not in result["mcp"]
        assert "keep-srv" in result["mcp"]

    def test_removes_from_gemini_settings(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        cfg = gemini_dir / "settings.json"
        cfg.write_text(
            json.dumps({"mcpServers": {"stale-gem": {}, "keep": {}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["gemini"]):
            MCPIntegrator.remove_stale(
                {"stale-gem"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(cfg.read_text(encoding="utf-8"))
        assert "stale-gem" not in result["mcpServers"]

    def test_removes_from_claude_project_mcp_json(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "stale-claude": {"type": "stdio", "command": "old", "args": []},
                        "keep": {},
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["claude"]):
            MCPIntegrator.remove_stale(
                {"stale-claude"},
                project_root=tmp_path,
                logger=MagicMock(),
                stored_configs={
                    "stale-claude": {
                        "name": "stale-claude",
                        "registry": False,
                        "transport": "stdio",
                        "command": "old",
                    }
                },
            )

        result = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "stale-claude" not in result["mcpServers"]

    def test_scope_unspecified_logs_progress_for_claude(self, tmp_path):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["claude"]):
            MCPIntegrator.remove_stale(
                {"some-server"},
                project_root=tmp_path,
                logger=logger,
                scope=None,
                runtime="claude",
            )
        # logger.progress should have been called at least once with scope info
        calls = [str(c) for c in logger.progress.call_args_list]
        assert any("scope" in c.lower() for c in calls)

    def test_expand_stale_includes_short_name(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_json = vscode_dir / "mcp.json"
        # Use the short name (last segment) as the config key
        mcp_json.write_text(
            json.dumps({"servers": {"github-mcp-server": {"cmd": "x"}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode"]):
            # Pass the full reference
            MCPIntegrator.remove_stale(
                {"io.github.github/github-mcp-server"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "github-mcp-server" not in result["servers"]


# ---------------------------------------------------------------------------
# MCPIntegrator.update_lockfile
# ---------------------------------------------------------------------------


class TestUpdateLockfile:
    def test_noop_when_lockfile_missing(self, tmp_path):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        lock_path = tmp_path / "apm.lock.yaml"
        # Does not exist -- should silently return
        MCPIntegrator.update_lockfile({"server-a"}, lock_path=lock_path)

    def test_updates_mcp_servers_in_lockfile(self, tmp_path):
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        lock_path = tmp_path / "apm.lock.yaml"
        LockFile().save(lock_path)

        MCPIntegrator.update_lockfile({"server-a", "server-b"}, lock_path=lock_path)

        lockfile = LockFile.read(lock_path)
        assert lockfile is not None
        assert lockfile.mcp_servers == sorted({"server-a", "server-b"})

    def test_noop_when_lockfile_read_returns_none(self, tmp_path):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text("", encoding="utf-8")

        with patch("apm_cli.deps.lockfile.LockFile.read", return_value=None):
            # Should not raise
            MCPIntegrator.update_lockfile({"srv"}, lock_path=lock_path)

    def test_mcp_configs_written_when_provided(self, tmp_path):
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        lock_path = tmp_path / "apm.lock.yaml"
        LockFile().save(lock_path)

        configs = {"srv": {"key": "val"}}
        MCPIntegrator.update_lockfile({"srv"}, lock_path=lock_path, mcp_configs=configs)

        lockfile = LockFile.read(lock_path)
        assert lockfile is not None
        assert lockfile.mcp_configs == configs


# ---------------------------------------------------------------------------
# MCPIntegrator._detect_runtimes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCPIntegrator._install_for_runtime -- ImportError / ValueError paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCPIntegrator._check_self_defined_servers_needing_installation
# ---------------------------------------------------------------------------
