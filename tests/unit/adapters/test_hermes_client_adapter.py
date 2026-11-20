"""Unit tests for HermesClientAdapter and its MCP integrator wiring.

Hermes reads MCP servers from a YAML ``mcp_servers:`` block in
``~/.hermes/config.yaml`` (snake_case key, distinct from the JSON
``mcpServers`` schema of Claude/Copilot).  These tests pin:

  * factory registration (``ClientFactory.create_client("hermes")``);
  * the copilot -> hermes entry conversion (stdio + http shapes);
  * YAML round-trip writes via ``utils.yaml_io`` that PRESERVE unrelated
    top-level config keys (model provider, telegram, ...);
  * idempotent merge semantics;
  * ``$HERMES_HOME`` path override.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.hermes import HermesClientAdapter
from apm_cli.factory import ClientFactory
from apm_cli.utils.yaml_io import dump_yaml, load_yaml

pytestmark = pytest.mark.component


class TestHermesClientFactory(unittest.TestCase):
    """Factory registration for the hermes runtime."""

    def test_create_hermes_client(self):
        client = ClientFactory.create_client("hermes")
        self.assertIsInstance(client, HermesClientAdapter)

    def test_create_hermes_client_case_insensitive(self):
        client = ClientFactory.create_client("Hermes")
        self.assertIsInstance(client, HermesClientAdapter)

    def test_hermes_uses_snake_case_servers_key(self):
        self.assertEqual(HermesClientAdapter.mcp_servers_key, "mcp_servers")

    def test_hermes_supports_user_scope(self):
        self.assertTrue(HermesClientAdapter.supports_user_scope)

    def test_hermes_target_name(self):
        self.assertEqual(HermesClientAdapter.target_name, "hermes")


class TestHermesConfigPath(unittest.TestCase):
    """Config path resolution honours $HERMES_HOME and defaults to ~/.hermes."""

    def test_default_config_path(self):
        adapter = HermesClientAdapter()
        fake_home = Path("/fake/home")
        with patch.object(Path, "home", staticmethod(lambda: fake_home)):
            path = Path(adapter.get_config_path())
        expected = (fake_home / ".hermes" / "config.yaml").resolve(strict=False)
        self.assertEqual(path, expected)

    @pytest.mark.windows_compat
    def test_hermes_home_override(self):
        adapter = HermesClientAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            # A platform-absolute value: "/custom/hermes" has no drive on Windows.
            custom = Path(tmp) / "custom" / "hermes"
            with patch.dict("os.environ", {"HERMES_HOME": str(custom)}):
                path = Path(adapter.get_config_path())
        self.assertEqual(path, custom.resolve(strict=False) / "config.yaml")


class TestHermesUpdateConfig(unittest.TestCase):
    """YAML write semantics: merge into mcp_servers, preserve siblings."""

    def _adapter_for(self, home: Path) -> HermesClientAdapter:
        adapter = HermesClientAdapter()
        adapter._hermes_config_path = home / ".hermes" / "config.yaml"  # type: ignore[attr-defined]
        return adapter

    def test_writes_mcp_servers_block(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx", "args": ["-y", "demo"]}})
                self.assertTrue(ok)
                cfg_path = home / ".hermes" / "config.yaml"
                self.assertTrue(cfg_path.is_file())
                data = load_yaml(cfg_path)
                self.assertIn("mcp_servers", data)
                self.assertIn("demo", data["mcp_servers"])
                self.assertEqual(data["mcp_servers"]["demo"]["command"], "npx")

    def test_preserves_unrelated_top_level_keys(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".hermes" / "config.yaml"
            cfg_path.parent.mkdir(parents=True)
            dump_yaml(
                {
                    "model": {"provider": "openai", "name": "test-model"},
                    "telegram": {"allowed_users": [424242]},
                    "mcp_servers": {"old": {"command": "old", "enabled": True}},
                },
                cfg_path,
            )
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx", "args": ["-y", "demo"]}})
                self.assertTrue(ok)
            data = load_yaml(cfg_path)
            # New server merged in...
            self.assertIn("demo", data["mcp_servers"])
            # ...without clobbering existing servers or sibling config keys.
            self.assertIn("old", data["mcp_servers"])
            self.assertEqual(data["model"]["name"], "test-model")
            self.assertEqual(data["telegram"]["allowed_users"], [424242])

    def test_idempotent_merge(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                payload = {"demo": {"command": "npx", "args": ["-y", "demo"]}}
                adapter.update_config(payload)
                adapter.update_config(payload)
            data = load_yaml(home / ".hermes" / "config.yaml")
            self.assertEqual(list(data["mcp_servers"].keys()), ["demo"])


class TestHermesConfigSecurity(unittest.TestCase):
    """Credential-bearing config.yaml is written 0o600 and never clobbered."""

    @unittest.skipUnless(hasattr(os, "fchmod"), "POSIX mode bits required")
    def test_new_config_written_owner_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                self.assertTrue(adapter.update_config({"demo": {"command": "npx"}}))
            cfg_path = home / ".hermes" / "config.yaml"
            mode = stat.S_IMODE(cfg_path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    @unittest.skipUnless(hasattr(os, "fchmod"), "POSIX mode bits required")
    def test_existing_loose_config_tightened_to_owner_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".hermes" / "config.yaml"
            cfg_path.parent.mkdir(parents=True)
            dump_yaml({"model": {"provider": "zai"}}, cfg_path)
            os.chmod(cfg_path, 0o644)  # simulate a world-readable pre-existing file
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                self.assertTrue(adapter.update_config({"demo": {"command": "npx"}}))
            mode = stat.S_IMODE(cfg_path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"expected tightened 0o600, got {oct(mode)}")

    def test_malformed_config_is_not_overwritten(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".hermes" / "config.yaml"
            cfg_path.parent.mkdir(parents=True)
            # Unparseable YAML carrying a "credential" we must not discard.
            original = "model: {provider: zai\nSECRET_TOKEN: keep-me\n"
            cfg_path.write_text(original, encoding="utf-8")
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx"}})
            self.assertFalse(ok, "must refuse to overwrite a malformed config")
            # File is left byte-for-byte untouched.
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), original)

    def test_non_mapping_config_is_not_overwritten(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".hermes" / "config.yaml"
            cfg_path.parent.mkdir(parents=True)
            original = "- just\n- a\n- list\n"  # valid YAML, but not a mapping
            cfg_path.write_text(original, encoding="utf-8")
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                ok = adapter.update_config({"demo": {"command": "npx"}})
            self.assertFalse(ok)
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), original)

    def test_empty_config_file_is_writable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / ".hermes" / "config.yaml"
            cfg_path.parent.mkdir(parents=True)
            cfg_path.write_text("", encoding="utf-8")  # empty -> treated as {}
            with patch.object(Path, "home", staticmethod(lambda h=home: h)):
                adapter = HermesClientAdapter()
                self.assertTrue(adapter.update_config({"demo": {"command": "npx"}}))
            data = load_yaml(cfg_path)
            self.assertIn("demo", data["mcp_servers"])


# --- #3749: documented Hermes keys survive the render -------------------------

_STDIO = {
    "name": "fidelity-stdio",
    "registry": False,
    "transport": "stdio",
    "command": "node",
    "args": ["server.js"],
    "env": {"MODE": "safe"},
    "cwd": "/srv/fixture",
    "extra": {"timeout": 120, "connect_timeout": 15, "httpUrl": "https://evil.invalid"},
}
_SSE = {
    "name": "fidelity-sse",
    "registry": False,
    "transport": "sse",
    "url": "https://sse.example.invalid/sse",
    "extra": {"auth": "oauth", "serverUrl": "https://evil.invalid", "enabled": False},
}
_HTTP = {
    "name": "fidelity-http",
    "registry": False,
    "transport": "streamable-http",
    "url": "https://http.example.invalid/mcp",
    "headers": {"X-Tenant": "acme"},
}


def _info(declaration: dict) -> dict:
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency

    return MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(declaration))


@pytest.fixture
def hermes(tmp_path, monkeypatch) -> HermesClientAdapter:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    return HermesClientAdapter()


@pytest.mark.windows_compat
def test_stdio_render_keeps_cwd_and_passthrough_keys(hermes) -> None:
    assert hermes.render_server_config(_info(_STDIO)) == {
        "command": "node",
        "args": ["server.js"],
        "env": {"MODE": "safe"},
        "cwd": "/srv/fixture",
        "timeout": 120,
        "connect_timeout": 15,
        "enabled": True,
    }


@pytest.mark.windows_compat
def test_sse_render_sets_transport_and_keeps_auth(hermes) -> None:
    # Denylisted aliases (serverUrl, enabled) never reach the entry.
    assert hermes.render_server_config(_info(_SSE)) == {
        "url": "https://sse.example.invalid/sse",
        "transport": "sse",
        "auth": "oauth",
        "enabled": True,
    }


@pytest.mark.windows_compat
def test_streamable_http_render_has_no_transport_key(hermes) -> None:
    assert hermes.render_server_config(_info(_HTTP)) == {
        "url": "https://http.example.invalid/mcp",
        "headers": {"X-Tenant": "acme"},
        "enabled": True,
    }


@pytest.mark.windows_compat
def test_registry_sse_remote_sets_transport(hermes) -> None:
    info = {"name": "srv", "id": "", "remotes": [{"type": "sse", "url": "https://r.invalid"}]}
    rendered = hermes.render_server_config(info)
    assert rendered == {"url": "https://r.invalid", "transport": "sse", "enabled": True}


@pytest.mark.windows_compat
def test_write_preserves_unrelated_keys_and_foreign_servers(hermes, tmp_path) -> None:
    path = tmp_path / "hermes-home" / "config.yaml"
    path.parent.mkdir()
    foreign = {"command": "user-owned", "enabled": False}
    dump_yaml({"model": {"provider": "p"}, "mcp_servers": {"user-own": foreign}}, path)

    rendered = {d["name"]: hermes.render_server_config(_info(d)) for d in (_STDIO, _SSE)}
    assert hermes.update_config(rendered)
    first = path.read_bytes()
    assert hermes.update_config(rendered)

    data = load_yaml(path)
    assert path.read_bytes() == first
    assert data["model"] == {"provider": "p"}
    assert data["mcp_servers"]["user-own"] == foreign
    assert data["mcp_servers"]["fidelity-stdio"]["cwd"] == "/srv/fixture"
    assert data["mcp_servers"]["fidelity-sse"]["transport"] == "sse"


@pytest.mark.windows_compat
@pytest.mark.parametrize("declaration", [_STDIO, _SSE], ids=["stdio-cwd", "sse"])
def test_v036_write_heals_and_foreign_edit_is_preserved(hermes, declaration) -> None:
    from apm_cli.install.mcp.ownership import (
        NativeMcpConnectionState,
        compare_native_mcp_connection,
        mcp_native_hash,
        native_comparison_clients,
    )

    assert "hermes" in native_comparison_clients(["hermes"], project_root=None, user_scope=True)
    info = _info(declaration)
    current = hermes.render_server_config(info)
    v036 = HermesClientAdapter._v036_render(current)
    assert v036 != current

    def compare(native: dict, native_hash: str | None) -> NativeMcpConnectionState:
        return compare_native_mcp_connection(
            hermes,
            declaration["name"],
            requested_info=info,
            stored_config=declaration,
            managed=True,
            existing_configs={declaration["name"]: native},
            native_hash=native_hash,
            declaration_changed=False,
        )

    healed = NativeMcpConnectionState.UPDATE_MANAGED
    assert compare(v036, mcp_native_hash(v036)) is healed
    assert compare(v036, None) is healed
    assert compare(current, mcp_native_hash(current)) is NativeMcpConnectionState.MATCHING_MANAGED
    edited = {**v036, "enabled": False}
    assert compare(edited, mcp_native_hash(v036)) is NativeMcpConnectionState.CONFLICT


if __name__ == "__main__":
    unittest.main()
