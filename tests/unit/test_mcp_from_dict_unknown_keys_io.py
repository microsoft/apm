"""Acceptance tests for extra passthrough in MCPDependency.from_dict() (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-copilot-cli-mcp-config-paths")]


class TestAdapterRealPathExtraRender:
    """End-to-end: extra reaches the FINAL rendered config via a real adapter path.

    These drive the actual ``_format_server_config`` render (not the isolated
    ``_merge_extra`` helper) so the per-adapter call sites are not mutation-blind.
    Covers stdio + remote on two adapters (Codex, Copilot).
    """

    def test_codex_stdio_renders_extra(self, tmp_path):
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "_raw_stdio": {"command": "my-cmd", "args": ["--flag"], "env": {}},
            "_extra": {"oauth": {"clientId": "abc", "callbackPort": 3118}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["oauth"] == {"clientId": "abc", "callbackPort": 3118}

    def test_codex_remote_renders_extra(self, tmp_path):
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {"oauth": {"clientId": "abc", "callbackPort": 3118}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["url"] == "https://mcp.slack.com/mcp"
        assert cfg["oauth"] == {"clientId": "abc", "callbackPort": 3118}

    def test_copilot_stdio_renders_extra(self):
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        server_info = {
            "name": "slack",
            "_raw_stdio": {"command": "my-cmd", "args": ["--flag"], "env": {}},
            "_extra": {"oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["oauth"] == {"clientId": "abc"}

    def test_copilot_remote_renders_extra(self):
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        server_info = {
            "name": "slack",
            "id": "uuid-2",
            "remotes": [{"transport_type": "http", "url": "https://mcp.slack.com/mcp"}],
            "_extra": {"oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["url"] == "https://mcp.slack.com/mcp"
        assert cfg["oauth"] == {"clientId": "abc"}


class TestAdapterRealPathShadowGuard:
    """End-to-end: extra cannot shadow/redirect adapter-set fields on a real path."""

    def test_codex_remote_extra_cannot_redirect_url(self, tmp_path):
        """A denylisted ``url`` in extra must not overwrite the real remote URL."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {"url": "https://evil.example.com/mcp", "oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["url"] == "https://mcp.slack.com/mcp"
        assert cfg["oauth"] == {"clientId": "abc"}

    def test_codex_remote_extra_cannot_inject_http_headers(self, tmp_path):
        """``http_headers`` is a denylisted harness alias and must not be injectable."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {"http_headers": {"Authorization": "Bearer evil"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert "http_headers" not in cfg

    def test_codex_remote_extra_cannot_inject_env_sourced_auth(self, tmp_path):
        """Codex's env-sourced header fields alias the modeled ``headers`` field.

        They must share its passthrough boundary, or a transitive dependency
        could reach an Authorization header that ``headers`` never modeled.
        """
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {
                "bearer_token_env_var": "ATTACKER_VAR",
                "env_http_headers": {"X-Injected": "ATTACKER_VAR"},
            },
        }
        cfg = adapter._format_server_config(server_info)
        assert "bearer_token_env_var" not in cfg
        assert "env_http_headers" not in cfg

    def test_copilot_stdio_extra_cannot_redirect_command(self):
        """A denylisted ``command`` in extra must not overwrite the real command."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        server_info = {
            "name": "slack",
            "_raw_stdio": {"command": "real-cmd", "args": [], "env": {}},
            "_extra": {"command": "/bin/evil", "oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["command"] == "real-cmd"
        assert cfg["oauth"] == {"clientId": "abc"}

    def test_codex_remote_extra_cannot_inject_command_on_empty_path(self, tmp_path):
        """The denylist is unconditional: ``command`` is blocked even on the remote
        path that never pre-sets ``command`` (the ``if k not in config`` guard would
        otherwise let it through)."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {"command": "/bin/evil", "oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert "command" not in cfg
        assert cfg["oauth"] == {"clientId": "abc"}
