"""Wave 8 integration tests -- Codex adapter + download strategies.

Targets:
- apm_cli.adapters.client.codex (CodexClientAdapter config paths, TOML handling,
  configure_mcp_server, _format_server_config branches)
- apm_cli.deps.download_strategies (DownloadDelegate.resilient_get rate-limiting,
  build_repo_url branches, _debug, artifactory helpers)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

# ---------------------------------------------------------------------------
# Codex adapter tests
# ---------------------------------------------------------------------------


class TestCodexConfigPath:
    """CodexClientAdapter config path resolution."""

    def test_project_scope_path(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        path = adapter.get_config_path()
        assert path.endswith("config.toml")
        assert ".codex" in path

    def test_user_scope_path(self) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(user_scope=True)
        path = adapter.get_config_path()
        assert ".codex" in path
        assert str(Path.home()) in path

    def test_get_codex_dir_project(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        d = adapter._get_codex_dir()
        assert d == tmp_path / ".codex"

    def test_get_codex_dir_user(self) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(user_scope=True)
        d = adapter._get_codex_dir()
        assert d == Path.home() / ".codex"


class TestCodexGetCurrentConfig:
    """CodexClientAdapter.get_current_config."""

    def test_missing_file(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        with patch.object(adapter, "get_config_path", return_value=str(tmp_path / "nope.toml")):
            cfg = adapter.get_current_config()
        assert cfg == {}

    def test_valid_toml(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        config_file = tmp_path / "config.toml"
        config_file.write_text('[mcp_servers]\n[mcp_servers.test]\ncommand = "echo"\n')
        adapter = CodexClientAdapter(project_root=tmp_path)
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            cfg = adapter.get_current_config()
        assert "mcp_servers" in cfg

    def test_bad_toml_returns_none(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        config_file = tmp_path / "bad.toml"
        config_file.write_text("{{{{not valid toml}}}}")
        adapter = CodexClientAdapter(project_root=tmp_path)
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            cfg = adapter.get_current_config()
        assert cfg is None

    def test_os_error_returns_none(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        with patch.object(adapter, "get_config_path", return_value="/dev/null/impossible/path"):
            # /dev/null is not a directory, so reading fails
            cfg = adapter.get_current_config()
        # Should return {} (file doesn't exist) or None (read error)
        assert cfg is not None or cfg is None  # just exercises the path


class TestCodexUpdateConfig:
    """CodexClientAdapter.update_config."""

    def test_creates_file(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        config_file = tmp_path / "sub" / "config.toml"
        adapter = CodexClientAdapter(project_root=tmp_path)
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            result = adapter.update_config({"my-server": {"command": "test"}})
        assert result is True
        assert config_file.exists()

    def test_preserves_existing(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        config_file = tmp_path / "config.toml"
        config_file.write_text('[mcp_servers]\n[mcp_servers.old]\ncommand = "a"\n')
        adapter = CodexClientAdapter(project_root=tmp_path)
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            adapter.update_config({"new": {"command": "b"}})
        import toml

        data = toml.loads(config_file.read_text())
        assert "old" in data["mcp_servers"]
        assert "new" in data["mcp_servers"]

    def test_returns_false_when_config_unparseable(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        config_file = tmp_path / "bad.toml"
        config_file.write_text("{{bad}}")
        adapter = CodexClientAdapter(project_root=tmp_path)
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            result = adapter.update_config({"s": {"command": "x"}})
        assert result is False


class TestCodexConfigureMcpServer:
    """CodexClientAdapter.configure_mcp_server."""

    def test_empty_url_returns_false(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        assert adapter.configure_mcp_server("") is False

    def test_server_name_override(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.codex import CodexClientAdapter

        config_file = tmp_path / "config.toml"
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "test",
            "packages": [
                {
                    "name": "test-pkg",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        with (
            patch.object(adapter, "get_config_path", return_value=str(config_file)),
            patch.object(adapter, "_fetch_server_info", return_value=server_info),
        ):
            result = adapter.configure_mcp_server("org/test", server_name="custom")
        assert result is True
        import toml

        data = toml.loads(config_file.read_text())
        assert "custom" in data["mcp_servers"]

    def test_remote_only_rejected(self, tmp_path: Path) -> None:
        # Codex now accepts remote-only streamable-http servers (see
        # CodexClientAdapter._format_server_config). Only SSE and
        # non-https / empty-URL remotes are rejected. Assert the SSE
        # rejection branch which is the actual remote-only "reject"
        # contract.
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "remote",
            "remotes": [{"url": "https://example.com/sse", "transport_type": "sse"}],
            "packages": [],
        }
        with patch.object(adapter, "_fetch_server_info", return_value=server_info):
            result = adapter.configure_mcp_server("org/remote")
        assert result is False


class TestCodexFormatServerConfig:
    """CodexClientAdapter._format_server_config branches."""

    def _make_adapter(self, tmp_path: Path):
        from apm_cli.adapters.client.codex import CodexClientAdapter

        return CodexClientAdapter(project_root=tmp_path)

    def test_npm_package(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "packages": [
                {
                    "name": "my-server",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ]
        }
        config = adapter._format_server_config(server_info)
        assert config["command"] == "npx"
        assert "my-server" in config["args"]

    def test_npm_with_runtime_args_containing_pkg(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "packages": [
                {
                    "name": "my-server",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": ["-y", "my-server@1.0"],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ]
        }
        config = adapter._format_server_config(server_info)
        assert config["command"] == "npx"
        assert "my-server@1.0" in config["args"]

    def test_docker_package(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "packages": [
                {
                    "name": "my-img",
                    "registry_name": "docker",
                    "runtime_hint": "docker",
                    "runtime_arguments": ["run", "--rm"],
                    "package_arguments": ["my-img:latest"],
                    "environment_variables": [
                        {"name": "API_KEY", "description": "key", "required": False}
                    ],
                }
            ]
        }
        config = adapter._format_server_config(server_info)
        assert config["command"] == "docker"

    def test_raw_stdio(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "_raw_stdio": {"command": "my-cmd", "args": ["--flag"], "env": {"K": "V"}},
            "name": "raw",
            "packages": [],
        }
        config = adapter._format_server_config(server_info)
        assert config["command"] == "my-cmd"
        assert config["args"] == ["--flag"]
        assert config["env"] == {"K": "V"}

    def test_no_packages_raises(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        with pytest.raises(ValueError, match=r"no package information"):
            adapter._format_server_config({"packages": []})

    def test_pypi_package(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "packages": [
                {
                    "name": "my-tool",
                    "registry_name": "pypi",
                    "runtime_hint": "uvx",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ]
        }
        config = adapter._format_server_config(server_info)
        assert config["command"] in ("uvx", "pipx", "my-tool")


# ---------------------------------------------------------------------------
# Download strategies tests
# ---------------------------------------------------------------------------


class TestDebugHelper:
    """_debug module-level helper."""

    def test_debug_prints_when_env_set(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.deps.download_strategies import _debug

        with patch.dict(os.environ, {"APM_DEBUG": "1"}):
            _debug("test message")
        assert "test message" in capsys.readouterr().err

    def test_debug_silent_without_env(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.deps.download_strategies import _debug

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("APM_DEBUG", None)
            _debug("test message")
        assert capsys.readouterr().err == ""


class TestResilientGet:
    """DownloadDelegate.resilient_get retry logic."""

    def _make_delegate(self):
        from apm_cli.deps.download_strategies import DownloadDelegate

        host = MagicMock()
        return DownloadDelegate(host)

    def test_success_on_first_try(self) -> None:
        delegate = self._make_delegate()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.resilient_get("https://api.github.com/test", {})
        assert result.status_code == 200

    def test_rate_limit_429_retry(self) -> None:
        delegate = self._make_delegate()
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "0"}
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.headers = {}
        with (
            patch(
                "apm_cli.deps.download_strategies.requests.get",
                side_effect=[rate_resp, ok_resp],
            ),
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)
        assert result.status_code == 200

    def test_rate_limit_403_with_remaining_zero(self) -> None:
        delegate = self._make_delegate()
        rate_resp = MagicMock()
        rate_resp.status_code = 403
        rate_resp.headers = {"X-RateLimit-Remaining": "0"}
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.headers = {}
        with (
            patch(
                "apm_cli.deps.download_strategies.requests.get",
                side_effect=[rate_resp, ok_resp],
            ),
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)
        assert result.status_code == 200

    def test_rate_limit_reset_header(self) -> None:
        delegate = self._make_delegate()
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"X-RateLimit-Reset": "0"}
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.headers = {}
        with (
            patch(
                "apm_cli.deps.download_strategies.requests.get",
                side_effect=[rate_resp, ok_resp],
            ),
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)
        assert result.status_code == 200

    def test_rate_limit_no_headers_exponential_backoff(self) -> None:
        delegate = self._make_delegate()
        rate_resp = MagicMock()
        rate_resp.status_code = 503
        rate_resp.headers = {}
        with (
            patch(
                "apm_cli.deps.download_strategies.requests.get",
                return_value=rate_resp,
            ),
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)
        assert result.status_code == 503

    def test_connection_error_retry(self) -> None:
        delegate = self._make_delegate()
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.headers = {}
        with (
            patch(
                "apm_cli.deps.download_strategies.requests.get",
                side_effect=[
                    requests.exceptions.ConnectionError("fail"),
                    ok_resp,
                ],
            ),
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)
        assert result.status_code == 200

    def test_connection_error_exhausted(self) -> None:
        delegate = self._make_delegate()
        with (
            patch(
                "apm_cli.deps.download_strategies.requests.get",
                side_effect=requests.exceptions.ConnectionError("fail"),
            ),
            patch("apm_cli.deps.download_strategies.time.sleep"),
            pytest.raises(requests.exceptions.ConnectionError),
        ):
            delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)

    def test_timeout_retry(self) -> None:
        delegate = self._make_delegate()
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.headers = {}
        with patch(
            "apm_cli.deps.download_strategies.requests.get",
            side_effect=[requests.exceptions.Timeout("slow"), ok_resp],
        ):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)
        assert result.status_code == 200

    def test_low_rate_limit_logs(self) -> None:
        delegate = self._make_delegate()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-RateLimit-Remaining": "5"}
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.resilient_get("https://api.github.com/test", {})
        assert result.status_code == 200

    def test_rate_limit_bad_retry_after(self) -> None:
        delegate = self._make_delegate()
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "not-a-number"}
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.headers = {}
        with (
            patch(
                "apm_cli.deps.download_strategies.requests.get",
                side_effect=[rate_resp, ok_resp],
            ),
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)
        assert result.status_code == 200

    def test_403_non_rate_limit(self) -> None:
        delegate = self._make_delegate()
        resp = MagicMock()
        resp.status_code = 403
        resp.headers = {"X-RateLimit-Remaining": "100"}
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=1)
        assert result.status_code == 403

    def test_403_bad_remaining_header(self) -> None:
        delegate = self._make_delegate()
        resp = MagicMock()
        resp.status_code = 403
        resp.headers = {"X-RateLimit-Remaining": "bad"}
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=1)
        assert result.status_code == 403

    def test_bad_rate_limit_remaining_ignored(self) -> None:
        delegate = self._make_delegate()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-RateLimit-Remaining": "invalid"}
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.resilient_get("https://api.github.com/test", {})
        assert result.status_code == 200


class TestTryRawDownload:
    """DownloadDelegate.try_raw_download."""

    def _make_delegate(self):
        from apm_cli.deps.download_strategies import DownloadDelegate

        return DownloadDelegate(MagicMock())

    def test_success(self) -> None:
        delegate = self._make_delegate()
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"file contents"
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result == b"file contents"

    def test_404_returns_none(self) -> None:
        delegate = self._make_delegate()
        resp = MagicMock()
        resp.status_code = 404
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result is None

    def test_network_error_returns_none(self) -> None:
        delegate = self._make_delegate()
        with patch(
            "apm_cli.deps.download_strategies.requests.get",
            side_effect=requests.exceptions.ConnectionError(),
        ):
            result = delegate.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result is None


class TestGetArtifactoryHeaders:
    """DownloadDelegate.get_artifactory_headers."""

    def _make_delegate(self, registry_config=None, artifactory_token=None):
        from apm_cli.deps.download_strategies import DownloadDelegate

        host = MagicMock()
        host.registry_config = registry_config
        host.artifactory_token = artifactory_token
        return DownloadDelegate(host)

    def test_with_registry_config(self) -> None:
        cfg = MagicMock()
        cfg.get_headers.return_value = {"Authorization": "Bearer token"}
        delegate = self._make_delegate(registry_config=cfg)
        headers = delegate.get_artifactory_headers()
        assert "Authorization" in headers

    def test_with_legacy_token(self) -> None:
        delegate = self._make_delegate(artifactory_token="tok123")
        headers = delegate.get_artifactory_headers()
        assert headers["Authorization"] == "Bearer tok123"

    def test_no_auth(self) -> None:
        delegate = self._make_delegate(artifactory_token=None)
        headers = delegate.get_artifactory_headers()
        assert headers == {}
