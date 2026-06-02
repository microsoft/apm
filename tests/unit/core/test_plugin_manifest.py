"""Unit tests for ``apm_cli.core.plugin_manifest``."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.plugin_manifest import (
    PLUGIN_ECOSYSTEM_PATHS,
    PLUGIN_MANIFEST_ECOSYSTEMS,
    build_plugin_manifest,
    collect_mcp_servers,
    find_or_synthesize_plugin_json,
    write_plugin_manifest,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _minimal_apm_yml(tmp_path: Path, **extra: object) -> Path:
    """Write a minimal ``apm.yml`` to ``tmp_path`` and return its path."""
    fields = {"name": "my-plugin", "version": "1.0.0", "description": "test plugin", **extra}
    lines = "\n".join(f"{k}: {v}" for k, v in fields.items())
    apm = tmp_path / "apm.yml"
    _write(apm, lines + "\n")
    return apm


# ---------------------------------------------------------------------------
# TestCollectMcpServers
# ---------------------------------------------------------------------------


class TestCollectMcpServers:
    def test_returns_servers_from_valid_mcp_json(self, tmp_path: Path) -> None:
        mcp = tmp_path / ".mcp.json"
        mcp.write_text(
            json.dumps({"mcpServers": {"my-server": {"command": "npx", "args": ["-y", "server"]}}}),
            encoding="utf-8",
        )
        result = collect_mcp_servers(tmp_path)
        assert result == {"my-server": {"command": "npx", "args": ["-y", "server"]}}

    def test_returns_empty_when_no_mcp_file(self, tmp_path: Path) -> None:
        assert collect_mcp_servers(tmp_path) == {}

    def test_returns_empty_when_mcp_is_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real_mcp.json"
        real.write_text(
            json.dumps({"mcpServers": {"srv": {}}}),
            encoding="utf-8",
        )
        link = tmp_path / ".mcp.json"
        link.symlink_to(real)
        assert collect_mcp_servers(tmp_path) == {}

    def test_returns_empty_on_invalid_json(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text("{not valid json", encoding="utf-8")
        assert collect_mcp_servers(tmp_path) == {}

    def test_returns_empty_when_mcp_servers_not_dict(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": ["server-a", "server-b"]}),
            encoding="utf-8",
        )
        assert collect_mcp_servers(tmp_path) == {}

    def test_returns_empty_when_mcp_json_has_no_mcp_servers_key(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"otherKey": {}}),
            encoding="utf-8",
        )
        assert collect_mcp_servers(tmp_path) == {}

    def test_returns_empty_mcp_servers_dict(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {}}),
            encoding="utf-8",
        )
        assert collect_mcp_servers(tmp_path) == {}


# ---------------------------------------------------------------------------
# TestBuildPluginManifest
# ---------------------------------------------------------------------------


class TestBuildPluginManifest:
    def test_claude_includes_mcp_servers(self, tmp_path: Path) -> None:
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "node"}}}),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        assert "mcpServers" in manifest
        assert manifest["mcpServers"] == {"srv": {"command": "node"}}

    def test_claude_omits_mcp_when_no_mcp_json(self, tmp_path: Path) -> None:
        apm = _minimal_apm_yml(tmp_path)
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        assert "mcpServers" not in manifest

    def test_copilot_omits_mcp_servers(self, tmp_path: Path) -> None:
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "node"}}}),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "copilot")
        assert "mcpServers" not in manifest

    def test_strips_convention_directory_keys(self, tmp_path: Path) -> None:
        apm = _minimal_apm_yml(tmp_path)
        with patch("apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml") as mock_synth:
            mock_synth.return_value = {
                "name": "my-plugin",
                "version": "1.0.0",
                "agents": ["./agents"],
                "skills": ["./skills"],
                "commands": ["./commands"],
                "instructions": "./INSTRUCTIONS.md",
            }
            manifest = build_plugin_manifest(tmp_path, apm, "copilot")
        assert "agents" not in manifest
        assert "skills" not in manifest
        assert "commands" not in manifest
        assert "instructions" not in manifest
        assert manifest["name"] == "my-plugin"

    def test_name_and_version_from_apm_yml(self, tmp_path: Path) -> None:
        apm = _minimal_apm_yml(tmp_path, author="Jane Doe")
        manifest = build_plugin_manifest(tmp_path, apm, "copilot")
        assert manifest["name"] == "my-plugin"
        assert manifest["version"] == "1.0.0"
        assert manifest["description"] == "test plugin"
        assert manifest["author"] == {"name": "Jane Doe"}

    def test_claude_strips_credential_env_block(self, tmp_path: Path) -> None:
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "node",
                            "env": {"API_TOKEN": "secret-value"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        assert manifest["mcpServers"]["srv"] == {"command": "node"}
        assert "env" not in manifest["mcpServers"]["srv"]

    def test_claude_strips_credential_named_key(self, tmp_path: Path) -> None:
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "node",
                            "apiKey": "leak",
                            "headers": {"x": "y"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        srv = manifest["mcpServers"]["srv"]
        assert "apiKey" not in srv
        # headers is a sensitive block name -- stripped wholesale, not preserved.
        assert "headers" not in srv
        assert srv["command"] == "node"

    def test_claude_strips_nested_credential_keys(self, tmp_path: Path) -> None:
        # A credential hiding under a nested object (config.token) and inside a
        # nested headers block (headers.Authorization) must both be removed --
        # a shallow top-level pass would leak them.
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "node",
                            "config": {"token": "leak", "region": "eu"},
                            "transport": {
                                "headers": {"Authorization": "Bearer leak"},
                                "url": "https://api.example.com",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        srv = manifest["mcpServers"]["srv"]
        raw = json.dumps(manifest)
        assert "leak" not in raw
        assert "Bearer" not in raw
        # Non-secret siblings survive the recursion.
        assert srv["config"] == {"region": "eu"}
        assert srv["transport"]["url"] == "https://api.example.com"
        assert "headers" not in srv["transport"]

    def test_claude_redacts_secret_values(self, tmp_path: Path) -> None:
        # Secrets embedded in otherwise-innocuous values (a basic-auth URL and
        # an inline --token flag in args) are redacted even though their keys
        # carry no credential signal.
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "node",
                            "args": ["--token=sk-supersecret", "--verbose"],
                            "url": "https://alice:hunter2@api.example.com/v1",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        raw = json.dumps(manifest)
        assert "sk-supersecret" not in raw
        assert "hunter2" not in raw
        # Non-secret args survive; the flag name is kept, only its value scrubbed.
        srv = manifest["mcpServers"]["srv"]
        assert "--verbose" in srv["args"]
        assert any("--token=" in a for a in srv["args"])

    def test_server_name_with_key_substring_survives(self, tmp_path: Path) -> None:
        # Sensitivity applies to keys INSIDE a server object, never to the
        # server name itself -- a server called "my-keychain" must not vanish.
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"my-keychain": {"command": "node"}}}),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        assert manifest["mcpServers"]["my-keychain"] == {"command": "node"}

    def test_claude_redacts_positional_provider_tokens(self, tmp_path: Path) -> None:
        # Bare provider tokens (no key/flag signal) passed as positional args --
        # the canonical shape for several MCP servers -- must be redacted by
        # their recognisable prefix, not slip through verbatim.
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "npx",
                            "args": [
                                "-y",
                                "@modelcontextprotocol/server-github",
                                "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                                "xoxb-1234567890-abcdefghijkl",
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        raw = json.dumps(manifest)
        assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in raw
        assert "xoxb-1234567890-abcdefghijkl" not in raw
        # The non-secret positional args survive.
        srv = manifest["mcpServers"]["srv"]
        assert "@modelcontextprotocol/server-github" in srv["args"]

    def test_claude_redacts_space_separated_flag_value(self, tmp_path: Path) -> None:
        # The space-separated "--token VALUE" form leaves the secret in the NEXT
        # array element; the list-context lookahead must scrub it.
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "node",
                            "args": ["--token", "sk-spaced-secret-value", "--port", "8080"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        srv = manifest["mcpServers"]["srv"]
        assert "sk-spaced-secret-value" not in json.dumps(manifest)
        # The flag name itself is kept; a non-secret flag/value pair survives.
        assert "--token" in srv["args"]
        assert "--port" in srv["args"]
        assert "8080" in srv["args"]

    def test_claude_redacts_env_prefix_and_bearer(self, tmp_path: Path) -> None:
        # A shell env-prefix assignment in a command string and a Bearer header
        # carried as a standalone arg must both be redacted.
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "API_KEY=sk-envsecret npx server",
                            "args": ["-H", "Authorization: Bearer sk-live-headersecret"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        raw = json.dumps(manifest)
        assert "sk-envsecret" not in raw
        assert "sk-live-headersecret" not in raw
        # The variable name and the -H flag survive; only the values are scrubbed.
        srv = manifest["mcpServers"]["srv"]
        assert "API_KEY=" in srv["command"]
        assert "-H" in srv["args"]

    def test_claude_redacts_single_string_space_flag(self, tmp_path: Path) -> None:
        # The space-separated "--token VALUE" form carried inside ONE string --
        # either a whole command line or a single args element -- must be
        # redacted. The list-context lookahead only fires across separate array
        # elements, so this guards the single-string bypass.
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "npx server --token generic-space-secret-12345",
                            "args": ["--password hunter2-inline-value"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        raw = json.dumps(manifest)
        assert "generic-space-secret-12345" not in raw
        assert "hunter2-inline-value" not in raw
        # The flag names survive; only their trailing values are scrubbed.
        srv = manifest["mcpServers"]["srv"]
        assert "--token" in srv["command"]
        assert any("--password" in a for a in srv["args"])

    def test_claude_redacts_lowercase_env_and_more_token_prefixes(self, tmp_path: Path) -> None:
        # Lowercase env-prefix assignments and additional provider token prefixes
        # (GitLab, npm, PyPI) must be redacted -- an uppercase-only or
        # GitHub/OpenAI-only rule would leak them.
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "api_key=lowercasesecretvalue node srv",
                            "args": [
                                "glpat-abcdefghijklmnopqrstuvwx",
                                "npm_abcdefghijklmnopqrstuvwxyz0123456789",
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        raw = json.dumps(manifest)
        assert "lowercasesecretvalue" not in raw
        assert "glpat-abcdefghijklmnopqrstuvwx" not in raw
        assert "npm_abcdefghijklmnopqrstuvwxyz0123456789" not in raw
        # The lowercase variable name is preserved; only its value is scrubbed.
        assert "api_key=" in manifest["mcpServers"]["srv"]["command"]

    def test_claude_redacts_extended_provider_token_prefixes(self, tmp_path: Path) -> None:
        # HuggingFace, Stripe (underscore form), SendGrid, Supabase, and
        # Databricks tokens passed as positional args must be redacted -- a
        # GitHub/GitLab/npm-only allowlist would leak them. The fixtures are
        # assembled from fragments at runtime so no contiguous provider-token
        # literal lands in this file (avoids secret-scanning push protection on
        # a synthetic test value).
        hf = "hf_" + ("a" * 30)
        stripe = "sk_" + "live_" + ("b" * 24)
        sendgrid = "SG." + ("c" * 22) + "." + ("d" * 22)
        supabase = "sbp_" + ("e" * 32)
        databricks = "dapi" + ("0123456789abcdef" * 2)
        apm = _minimal_apm_yml(tmp_path)
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "node srv",
                            "args": [hf, stripe, sendgrid, supabase, databricks],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = build_plugin_manifest(tmp_path, apm, "claude")
        raw = json.dumps(manifest)
        for token in (hf, stripe, sendgrid, supabase, databricks):
            assert token not in raw


# ---------------------------------------------------------------------------
# TestWritePluginManifest
# ---------------------------------------------------------------------------


class TestWritePluginManifest:
    def test_writes_to_claude_path(self, tmp_path: Path) -> None:
        manifest = {"name": "plugin", "version": "1.0.0"}
        result = write_plugin_manifest(tmp_path, manifest, "claude")
        expected = tmp_path / ".claude-plugin" / "plugin.json"
        assert result == expected
        assert expected.exists()

    def test_writes_to_copilot_path(self, tmp_path: Path) -> None:
        manifest = {"name": "plugin", "version": "1.0.0"}
        result = write_plugin_manifest(tmp_path, manifest, "copilot")
        expected = tmp_path / ".github" / "plugin" / "plugin.json"
        assert result == expected
        assert expected.exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        manifest = {"name": "plugin"}
        result = write_plugin_manifest(tmp_path, manifest, "claude")
        assert result is not None
        assert result.parent.is_dir()

    def test_existing_file_preserved_without_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = tmp_path / ".claude-plugin" / "plugin.json"
        _write(existing, json.dumps({"name": "old-content"}))

        warnings_emitted: list[str] = []
        monkeypatch.setattr(
            "apm_cli.core.plugin_manifest._rich_warning",
            lambda msg, **kw: warnings_emitted.append(msg),
        )

        manifest = {"name": "new-content"}
        result = write_plugin_manifest(tmp_path, manifest, "claude")

        # Without --force the existing file is preserved and the write skipped.
        assert result is None
        preserved = json.loads(existing.read_text(encoding="utf-8"))
        assert preserved["name"] == "old-content"
        assert len(warnings_emitted) == 1
        assert "--force" in warnings_emitted[0]

    def test_overwrites_existing_with_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = tmp_path / ".claude-plugin" / "plugin.json"
        _write(existing, json.dumps({"name": "old-content"}))

        warnings_emitted: list[str] = []
        monkeypatch.setattr(
            "apm_cli.core.plugin_manifest._rich_warning",
            lambda msg, **kw: warnings_emitted.append(msg),
        )
        monkeypatch.setattr(
            "apm_cli.core.plugin_manifest._rich_success",
            lambda msg, **kw: None,
        )

        manifest = {"name": "new-content"}
        result = write_plugin_manifest(tmp_path, manifest, "claude", force=True)

        assert result == existing
        written = json.loads(existing.read_text(encoding="utf-8"))
        assert written["name"] == "new-content"
        assert len(warnings_emitted) == 1
        assert "verwriting" in warnings_emitted[0]

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        manifest = {"name": "plugin"}
        result = write_plugin_manifest(tmp_path, manifest, "claude", dry_run=True)
        expected = tmp_path / ".claude-plugin" / "plugin.json"
        assert result is None
        assert not expected.exists()

    def test_unknown_ecosystem_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings_emitted: list[str] = []
        monkeypatch.setattr(
            "apm_cli.core.plugin_manifest._rich_warning",
            lambda msg, **kw: warnings_emitted.append(msg),
        )

        manifest = {"name": "plugin"}
        result = write_plugin_manifest(tmp_path, manifest, "nonexistent-ecosystem")
        assert result is None
        assert len(warnings_emitted) == 1

    def test_output_is_valid_json(self, tmp_path: Path) -> None:
        manifest = {"name": "plugin", "version": "1.0.0", "description": "test"}
        result = write_plugin_manifest(tmp_path, manifest, "claude")
        assert result is not None
        raw = result.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        assert parsed == manifest
        # Verify indentation (indent=2 -- second line should start with 2 spaces)
        lines = raw.splitlines()
        assert len(lines) > 1
        assert lines[1].startswith("  ")

    def test_dry_run_with_logger_calls_info(self, tmp_path: Path) -> None:
        logger = MagicMock()
        manifest = {"name": "plugin"}
        result = write_plugin_manifest(tmp_path, manifest, "claude", dry_run=True, logger=logger)
        assert result is None
        logger.info.assert_called_once()

    def test_unknown_ecosystem_with_logger_calls_warning(self, tmp_path: Path) -> None:
        logger = MagicMock()
        result = write_plugin_manifest(tmp_path, {"name": "plugin"}, "unknown", logger=logger)
        assert result is None
        logger.warning.assert_called_once()

    def test_rejects_symlink_escape(self, tmp_path: Path) -> None:
        """Symlinked ecosystem dir pointing outside project root is rejected."""
        from apm_cli.utils.path_security import PathTraversalError

        outside = tmp_path / "outside"
        outside.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        # Symlink .claude-plugin -> ../outside
        (project / ".claude-plugin").symlink_to(outside)

        with pytest.raises(PathTraversalError):
            write_plugin_manifest(project, {"name": "p"}, "claude")


# ---------------------------------------------------------------------------
# TestFindOrSynthesizePluginJson
# ---------------------------------------------------------------------------


class TestFindOrSynthesizePluginJson:
    def test_finds_existing_plugin_json(self, tmp_path: Path) -> None:
        # Place plugin.json at the root (first candidate location)
        existing = {"name": "existing-plugin", "version": "2.0.0"}
        plugin_json = tmp_path / "plugin.json"
        plugin_json.write_text(json.dumps(existing), encoding="utf-8")
        apm = _minimal_apm_yml(tmp_path)

        result = find_or_synthesize_plugin_json(tmp_path, apm, suppress_missing_warning=True)
        assert result == existing

    def test_synthesises_when_no_plugin_json(self, tmp_path: Path) -> None:
        apm = _minimal_apm_yml(tmp_path)
        result = find_or_synthesize_plugin_json(tmp_path, apm, suppress_missing_warning=True)
        assert result["name"] == "my-plugin"
        assert result["version"] == "1.0.0"

    def test_falls_back_on_parse_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Create a malformed plugin.json at the root
        bad_json = tmp_path / "plugin.json"
        bad_json.write_text("{malformed json]", encoding="utf-8")
        apm = _minimal_apm_yml(tmp_path)

        warnings_emitted: list[str] = []
        monkeypatch.setattr(
            "apm_cli.core.plugin_manifest._rich_warning",
            lambda msg, **kw: warnings_emitted.append(msg),
        )

        result = find_or_synthesize_plugin_json(tmp_path, apm)
        # Should fall back to synthesised result from apm.yml
        assert result["name"] == "my-plugin"
        assert len(warnings_emitted) == 1
        assert "parse" in warnings_emitted[0].lower() or "Falling back" in warnings_emitted[0]

    def test_suppress_missing_warning_prevents_info_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        apm = _minimal_apm_yml(tmp_path)
        info_calls: list[str] = []
        monkeypatch.setattr(
            "apm_cli.core.plugin_manifest._rich_info",
            lambda msg, **kw: info_calls.append(msg),
        )

        find_or_synthesize_plugin_json(tmp_path, apm, suppress_missing_warning=True)
        assert info_calls == []

    def test_emits_info_when_no_plugin_json_and_not_suppressed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        apm = _minimal_apm_yml(tmp_path)
        info_calls: list[str] = []
        monkeypatch.setattr(
            "apm_cli.core.plugin_manifest._rich_info",
            lambda msg, **kw: info_calls.append(msg),
        )

        find_or_synthesize_plugin_json(tmp_path, apm, suppress_missing_warning=False)
        assert len(info_calls) == 1

    def test_finds_plugin_json_under_github_plugin(self, tmp_path: Path) -> None:
        existing = {"name": "github-plugin"}
        plugin_json = tmp_path / ".github" / "plugin" / "plugin.json"
        _write(plugin_json, json.dumps(existing))
        apm = _minimal_apm_yml(tmp_path)

        result = find_or_synthesize_plugin_json(tmp_path, apm, suppress_missing_warning=True)
        assert result == existing

    def test_falls_back_with_logger_calls_warning(self, tmp_path: Path) -> None:
        bad_json = tmp_path / "plugin.json"
        bad_json.write_text("{bad json", encoding="utf-8")
        apm = _minimal_apm_yml(tmp_path)

        logger = MagicMock()
        result = find_or_synthesize_plugin_json(tmp_path, apm, logger=logger)
        assert result["name"] == "my-plugin"
        logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_plugin_manifest_ecosystems_frozenset(self) -> None:
        assert isinstance(PLUGIN_MANIFEST_ECOSYSTEMS, frozenset)
        assert {"claude", "copilot"} == PLUGIN_MANIFEST_ECOSYSTEMS

    def test_plugin_ecosystem_paths_keys_match_ecosystems(self) -> None:
        assert set(PLUGIN_ECOSYSTEM_PATHS.keys()) == PLUGIN_MANIFEST_ECOSYSTEMS

    def test_claude_path(self) -> None:
        assert PLUGIN_ECOSYSTEM_PATHS["claude"] == ".claude-plugin/plugin.json"

    def test_copilot_path(self) -> None:
        assert PLUGIN_ECOSYSTEM_PATHS["copilot"] == ".github/plugin/plugin.json"
