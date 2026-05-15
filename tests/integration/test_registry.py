"""Integration tests for MCP registry client."""

import contextlib
import gc
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path  # noqa: F401

import pytest

from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.registry.client import SimpleRegistryClient


def safe_rmdir(path):
    """Safely remove a directory with retry logic for Windows.

    Args:
        path (str): Path to directory to remove
    """
    try:
        shutil.rmtree(path)
    except PermissionError:
        # On Windows, give time for any lingering processes to release the lock
        time.sleep(0.5)
        gc.collect()  # Force garbage collection to release file handles
        try:
            shutil.rmtree(path)
        except PermissionError as e:
            print(f"Failed to remove directory {path}: {e}")
            # Continue without failing the test
            pass


class TestMCPRegistry:
    """Test the MCP registry client end-to-end against the public GitHub MCP Registry.

    Previously targeted the legacy ``demo.registry.azure-mcp.net`` host
    which only served the non-spec ``/v0/`` API (issue #1210). Retargeted
    to ``api.mcp.github.com`` which is MCP Registry v0.1 spec-compliant.
    """

    def setup_method(self):
        """Set up test environment."""
        self.registry_url = "https://api.mcp.github.com"
        self.registry_client = SimpleRegistryClient(self.registry_url)

        # Create a temporary directory for tests
        self.test_dir = tempfile.TemporaryDirectory()
        self.test_dir_path = self.test_dir.name
        os.chdir(self.test_dir_path)

        # Create .vscode directory
        os.makedirs(os.path.join(self.test_dir_path, ".vscode"), exist_ok=True)

    def teardown_method(self):
        """Clean up after tests."""
        # Force garbage collection to release file handles
        gc.collect()

        # Give time for Windows to release locks
        if sys.platform == "win32":
            time.sleep(0.1)

        # Leave the temp tree before unlinking it.  Otherwise cwd can still
        # reference the directory inode and os.getcwd() raises FileNotFoundError
        # on POSIX -- breaking later tests on the same xdist worker.
        with contextlib.suppress(FileNotFoundError, OSError):
            os.chdir(tempfile.gettempdir())

        # First, try the standard cleanup
        try:
            self.test_dir.cleanup()
        except PermissionError:
            # If standard cleanup fails on Windows, use our safe_rmdir function
            if hasattr(self, "test_dir_path") and os.path.exists(self.test_dir_path):
                safe_rmdir(self.test_dir_path)

    def test_list_servers(self):
        """Test listing servers from the registry."""
        servers, _ = self.registry_client.list_servers(limit=5)
        assert isinstance(servers, list), "Server list should be a list"
        assert len(servers) > 0, "Public registry should have some servers"

    def test_get_server(self):
        """Test getting server details for a specific server (v0.1: keyed by name)."""
        servers, _ = self.registry_client.list_servers(limit=5)
        if not servers:
            pytest.skip("No servers available in the registry")

        server_name = servers[0]["name"]
        server_info = self.registry_client.get_server(server_name)

        assert server_info is not None, f"Server info for {server_name} should be retrievable"
        assert "name" in server_info, "Server info should include name"
        assert server_info["name"] == server_name

    def test_vscode_adapter_with_registry(self):
        """Test VSCode adapter with registry integration."""
        adapter = VSCodeClientAdapter(self.registry_url)

        # Walk a small page to find a server whose packages map to a VSCode-supported
        # transport (npm, pypi, docker). The first registry entry isn't guaranteed
        # to be VSCode-compatible (e.g. uvx-only servers are skipped).
        servers, _ = self.registry_client.list_servers(limit=20)
        if not servers:
            pytest.skip("No servers available in the registry")

        configured = False
        chosen_name = None
        last_error: Exception | None = None
        # Known unsupported-server failures: registry shapes the adapter
        # actively skips (uvx-only, mcpb, etc.) raise ValueError or KeyError.
        # Anything else is captured and re-raised at the end so a registry
        # contract regression (e.g. v0.1 packages with `identifier` instead
        # of `name`) cannot silently disguise itself as "no compatible
        # server" -- which is the regression #1210 was filed against.
        _expected_skip_errors = (ValueError, KeyError, TypeError)
        for s in servers:
            name = s.get("name")
            if not name:
                continue
            try:
                if adapter.configure_mcp_server(name) is True:
                    configured = True
                    chosen_name = name
                    break
            except _expected_skip_errors:
                continue
            except Exception as exc:
                last_error = exc
                continue

        if not configured:
            if last_error is not None:
                raise AssertionError(
                    "VSCode adapter could not configure any of the first 20 registry "
                    "servers and the last failure was an unexpected exception (likely a "
                    "registry contract regression): "
                    f"{type(last_error).__name__}: {last_error}"
                ) from last_error
            pytest.skip("No VSCode-compatible server found in the first 20 registry entries")

        config_path = os.path.join(self.test_dir.name, ".vscode", "mcp.json")
        assert os.path.exists(config_path), "Configuration file should be created"

        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

        assert "servers" in config, "Config should have servers section"
        assert chosen_name in config["servers"], f"Config should include {chosen_name}"
        assert "type" in config["servers"][chosen_name], "Server config should have type"
