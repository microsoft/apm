"""Unit tests for LSP extraction in ``apm_cli.deps.plugin_parser``.

Covers ``_extract_lsp_servers``, ``_read_lsp_json``, ``_read_lsp_file``,
and ``_lsp_servers_to_apm_deps`` -- the LSP-specific helpers added to
the plugin parser.
"""

from __future__ import annotations

import json

from apm_cli.deps.plugin_parser import (
    _extract_lsp_servers,
    _lsp_servers_to_apm_deps,
    _read_lsp_json,
)

# ===========================================================================
# _read_lsp_json
# ===========================================================================


class TestReadLspJson:
    def test_reads_valid_json(self, tmp_path, caplog):
        lsp_file = tmp_path / ".lsp.json"
        lsp_file.write_text(json.dumps({"pyright": {"command": "pyright-langserver"}}))

        import logging

        result = _read_lsp_json(lsp_file, logging.getLogger("test"))
        assert "pyright" in result

    def test_returns_empty_on_invalid_json(self, tmp_path, caplog):
        lsp_file = tmp_path / ".lsp.json"
        lsp_file.write_text("not valid json{")

        import logging

        result = _read_lsp_json(lsp_file, logging.getLogger("test"))
        assert result == {}

    def test_returns_empty_on_non_dict_json(self, tmp_path):
        lsp_file = tmp_path / ".lsp.json"
        lsp_file.write_text(json.dumps(["not", "a", "dict"]))

        import logging

        result = _read_lsp_json(lsp_file, logging.getLogger("test"))
        assert result == {}


# ===========================================================================
# _extract_lsp_servers
# ===========================================================================


class TestExtractLspServers:
    def test_inline_dict_lsp_servers(self, tmp_path):
        manifest = {
            "lspServers": {
                "pyright": {
                    "command": "pyright-langserver",
                    "extensionToLanguage": {".py": "python"},
                }
            }
        }
        result = _extract_lsp_servers(tmp_path, manifest)
        assert "pyright" in result

    def test_string_reference_to_lsp_file(self, tmp_path):
        lsp_file = tmp_path / "lsp-config.json"
        lsp_file.write_text(
            json.dumps({"ruff-lsp": {"command": "ruff", "extensionToLanguage": {".py": "python"}}})
        )

        manifest = {"lspServers": "lsp-config.json"}
        result = _extract_lsp_servers(tmp_path, manifest)
        assert "ruff-lsp" in result

    def test_auto_discovery_of_lsp_json(self, tmp_path):
        lsp_json = tmp_path / ".lsp.json"
        lsp_json.write_text(
            json.dumps(
                {
                    "ts-lsp": {
                        "command": "typescript-language-server",
                        "extensionToLanguage": {".ts": "typescript"},
                    }
                }
            )
        )

        manifest = {}  # No lspServers key
        result = _extract_lsp_servers(tmp_path, manifest)
        assert "ts-lsp" in result

    def test_no_lsp_servers_no_file_returns_empty(self, tmp_path):
        result = _extract_lsp_servers(tmp_path, {})
        assert result == {}

    def test_unsupported_type_returns_empty(self, tmp_path):
        manifest = {"lspServers": 42}
        result = _extract_lsp_servers(tmp_path, manifest)
        assert result == {}

    def test_symlink_lsp_json_skipped(self, tmp_path):
        real = tmp_path / "real.json"
        real.write_text(json.dumps({"evil": {"command": "x"}}))
        link = tmp_path / ".lsp.json"
        link.symlink_to(real)

        result = _extract_lsp_servers(tmp_path, {})
        assert result == {}

    def test_path_traversal_in_string_ref_blocked(self, tmp_path):
        # Create a file outside plugin root
        outside = tmp_path.parent / "outside.json"
        outside.write_text(json.dumps({"evil": {"command": "x"}}))

        manifest = {"lspServers": "../outside.json"}
        result = _extract_lsp_servers(tmp_path, manifest)
        assert result == {}

    def test_plugin_root_substitution(self, tmp_path):
        manifest = {
            "lspServers": {
                "my-lsp": {
                    "command": "${CLAUDE_PLUGIN_ROOT}/bin/lsp",
                    "extensionToLanguage": {".py": "python"},
                }
            }
        }
        result = _extract_lsp_servers(tmp_path, manifest)
        assert "my-lsp" in result
        abs_root = str(tmp_path.resolve())
        assert result["my-lsp"]["command"] == f"{abs_root}/bin/lsp"


# ===========================================================================
# _lsp_servers_to_apm_deps
# ===========================================================================


class TestLspServersToApmDeps:
    def test_valid_server_converted(self, tmp_path):
        servers = {
            "pyright": {
                "command": "pyright-langserver",
                "extensionToLanguage": {".py": "python"},
            }
        }
        deps = _lsp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 1
        assert deps[0]["name"] == "pyright"
        assert deps[0]["command"] == "pyright-langserver"

    def test_non_dict_config_skipped(self, tmp_path):
        servers = {"bad": "not-a-dict"}
        deps = _lsp_servers_to_apm_deps(servers, tmp_path)
        assert deps == []

    def test_invalid_server_skipped(self, tmp_path):
        """A server that fails validation is skipped with a warning."""
        servers = {
            "no-cmd": {
                # Missing required 'command' and 'extensionToLanguage'
                "transport": "stdio",
            }
        }
        deps = _lsp_servers_to_apm_deps(servers, tmp_path)
        assert deps == []

    def test_multiple_servers_mixed_validity(self, tmp_path):
        servers = {
            "valid": {
                "command": "lsp",
                "extensionToLanguage": {".py": "python"},
            },
            "invalid": {
                # Missing required fields
            },
        }
        deps = _lsp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 1
        assert deps[0]["name"] == "valid"

    def test_all_fields_copied(self, tmp_path):
        servers = {
            "full": {
                "command": "lsp",
                "args": ["--stdio"],
                "extensionToLanguage": {".py": "python"},
                "transport": "stdio",
                "env": {"KEY": "val"},
                "initializationOptions": {"lint": True},
                "settings": {},
                "workspaceFolder": "/x",
                "startupTimeout": 5000,
                "shutdownTimeout": 3000,
                "restartOnCrash": True,
                "maxRestarts": 3,
            }
        }
        deps = _lsp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 1
        d = deps[0]
        assert d["args"] == ["--stdio"]
        assert d["transport"] == "stdio"
        assert d["env"] == {"KEY": "val"}
        assert d["initializationOptions"] == {"lint": True}
        assert d["startupTimeout"] == 5000
        assert d["restartOnCrash"] is True
        assert d["maxRestarts"] == 3
