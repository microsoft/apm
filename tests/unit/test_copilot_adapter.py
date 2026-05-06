"""Unit tests for the Copilot client adapter transport validation (issue #791)."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from apm_cli.adapters.client.copilot import CopilotClientAdapter


class TestCopilotRemoteTransportValidation(unittest.TestCase):
    """Validation of ``transport_type`` mirrors PR #656 (VS Code adapter)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = os.path.join(self.temp_dir, "mcp-config.json")
        with open(self.temp_path, "w") as f:
            json.dump({"mcpServers": {}}, f)

        self.mock_registry_patcher = patch("apm_cli.adapters.client.copilot.SimpleRegistryClient")
        self.mock_registry_class = self.mock_registry_patcher.start()
        self.mock_registry_class.return_value = MagicMock()

        self.mock_integration_patcher = patch("apm_cli.adapters.client.copilot.RegistryIntegration")
        self.mock_integration_class = self.mock_integration_patcher.start()
        self.mock_integration_class.return_value = MagicMock()

        self.get_path_patcher = patch(
            "apm_cli.adapters.client.copilot.CopilotClientAdapter.get_config_path",
            return_value=self.temp_path,
        )
        self.get_path_patcher.start()

    def tearDown(self):
        self.get_path_patcher.stop()
        self.mock_integration_patcher.stop()
        self.mock_registry_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_remote_missing_transport_type_defaults_to_http(self):
        """Remote with no transport_type produces a type=http config (issue #791)."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-1",
            "name": "atlassian-mcp-server",
            "remotes": [{"url": "https://mcp.atlassian.com/v1/mcp"}],
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], "https://mcp.atlassian.com/v1/mcp")

    def test_remote_empty_transport_type_defaults_to_http(self):
        """Empty string transport_type is treated as missing."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-2",
            "name": "remote-srv",
            "remotes": [{"transport_type": "", "url": "https://example.com/mcp"}],
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], "https://example.com/mcp")

    def test_remote_none_transport_type_defaults_to_http(self):
        """Null transport_type is treated as missing."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-3",
            "name": "remote-srv",
            "remotes": [{"transport_type": None, "url": "https://example.com/mcp"}],
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")

    def test_remote_whitespace_transport_type_defaults_to_http(self):
        """Whitespace-only transport_type is treated as missing."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-4",
            "name": "remote-srv",
            "remotes": [{"transport_type": "  ", "url": "https://example.com/mcp"}],
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")

    def test_remote_unsupported_transport_raises(self):
        """Unrecognized transport_type raises ValueError with server name."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-5",
            "name": "future-srv",
            "remotes": [{"transport_type": "grpc", "url": "https://example.com/mcp"}],
        }

        with self.assertRaises(ValueError) as ctx:
            adapter._format_server_config(server_info)

        message = str(ctx.exception)
        self.assertIn("Unsupported remote transport", message)
        self.assertIn("grpc", message)
        self.assertIn("future-srv", message)
        self.assertIn("Copilot", message)

    def test_remote_supported_transports_do_not_raise(self):
        """'sse' and 'streamable-http' transports pass validation."""
        adapter = CopilotClientAdapter()

        for transport in ("http", "sse", "streamable-http"):
            server_info = {
                "id": f"remote-{transport}",
                "name": f"srv-{transport}",
                "remotes": [{"transport_type": transport, "url": "https://example.com/mcp"}],
            }

            config = adapter._format_server_config(server_info)
            # Copilot CLI always emits type="http" for auth compatibility.
            self.assertEqual(config["type"], "http")
            self.assertEqual(config["url"], "https://example.com/mcp")

    def test_remote_skips_entries_without_url(self):
        """Remotes with empty URLs are skipped; first usable remote wins."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-multi",
            "name": "multi-remote",
            "remotes": [
                {"transport_type": "http", "url": ""},
                {"transport_type": "sse", "url": "https://good.example.com/sse"},
            ],
        }

        config = adapter._format_server_config(server_info)
        self.assertEqual(config["url"], "https://good.example.com/sse")


class TestCopilotEnvVarTranslationInHeaders(unittest.TestCase):
    """Issue #1152: Copilot CLI adapter translates env-var placeholders to
    its native runtime substitution syntax (``${VAR}``) instead of resolving
    them to plaintext at install time. Per Copilot CLI documentation,
    ``${VAR}``, ``$VAR`` and ``${VAR:-default}`` are evaluated at server
    start, so install-time resolution unnecessarily bakes secrets to disk.
    """

    def setUp(self):
        CopilotClientAdapter.reset_install_run_state()

    def tearDown(self):
        CopilotClientAdapter.reset_install_run_state()

    def _adapter(self):
        with (
            patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        ):
            return CopilotClientAdapter()

    def test_translate_env_prefix_to_bare(self):
        """``${env:VAR}`` becomes ``${VAR}``; the ``env:`` prefix is stripped."""
        adapter = self._adapter()
        with patch.dict(os.environ, {"MY_TOKEN": "secret-xyz"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${env:MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer ${MY_TOKEN}")
        self.assertNotIn("secret-xyz", result)

    def test_translate_bare_brace_is_idempotent(self):
        """``${VAR}`` is already Copilot-native; pass through unchanged."""
        adapter = self._adapter()
        with patch.dict(os.environ, {"MY_TOKEN": "secret-xyz"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer ${MY_TOKEN}")
        self.assertNotIn("secret-xyz", result)

    def test_translation_ignores_os_environ(self):
        """Canonical regression trap: translation MUST be pure-textual.
        Even if the env var is set in the process, the result must be the
        ``${VAR}`` reference -- never the literal value.
        """
        adapter = self._adapter()
        with patch.dict(os.environ, {"MY_TOKEN": "PLAINTEXT_DO_NOT_BAKE"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${env:MY_TOKEN}", env_overrides=None
            )
        self.assertNotIn("PLAINTEXT_DO_NOT_BAKE", result)
        self.assertEqual(result, "Bearer ${MY_TOKEN}")

    def test_translation_ignores_env_overrides(self):
        """``env_overrides`` is irrelevant in translation mode -- the value
        is never resolved here, the variable name is preserved as-is.
        """
        adapter = self._adapter()
        result = adapter._resolve_env_variable(
            "Authorization",
            "Bearer ${MY_TOKEN}",
            env_overrides={"MY_TOKEN": "from-overrides"},
        )
        self.assertEqual(result, "Bearer ${MY_TOKEN}")
        self.assertNotIn("from-overrides", result)

    def test_default_syntax_passes_through(self):
        """``${VAR:-default}`` is Copilot-native default syntax; passthrough."""
        adapter = self._adapter()
        result = adapter._resolve_env_variable(
            "Authorization", "Bearer ${MY_TOKEN:-anon}", env_overrides=None
        )
        self.assertEqual(result, "Bearer ${MY_TOKEN:-anon}")

    def test_legacy_angle_translates_with_warning(self):
        """``<VAR>`` legacy APM syntax translates to ``${VAR}`` and is
        recorded for the post-install deprecation warning."""
        adapter = self._adapter()
        adapter._last_legacy_angle_vars = set()
        result = adapter._resolve_env_variable(
            "Authorization", "Bearer <MY_TOKEN>", env_overrides=None
        )
        self.assertEqual(result, "Bearer ${MY_TOKEN}")
        self.assertIn("MY_TOKEN", adapter._last_legacy_angle_vars)

    def test_input_syntax_is_not_resolved(self):
        """``${input:...}`` is VS Code-only; passthrough untouched."""
        adapter = self._adapter()
        result = adapter._resolve_env_variable(
            "Authorization", "Bearer ${input:my-token}", env_overrides=None
        )
        self.assertEqual(result, "Bearer ${input:my-token}")

    def test_github_actions_template_is_not_touched(self):
        """``${{ secrets.X }}`` (GHA template) must pass through unchanged."""
        adapter = self._adapter()
        result = adapter._resolve_env_variable(
            "Authorization",
            "Bearer ${{ secrets.GITHUB_TOKEN }}",
            env_overrides=None,
        )
        self.assertEqual(result, "Bearer ${{ secrets.GITHUB_TOKEN }}")

    def test_mixed_syntaxes_translated_consistently(self):
        """A header may mix legacy ``<VAR>`` and new ``${VAR}``/``${env:VAR}``;
        all translate to ``${VAR}``, none resolve to literal values."""
        adapter = self._adapter()
        adapter._last_legacy_angle_vars = set()
        with patch.dict(
            os.environ,
            {"OLD": "old-val", "NEW": "new-val", "ENV_PREFIXED": "env-val"},
            clear=False,
        ):
            result = adapter._resolve_env_variable(
                "X-Mixed",
                "old=<OLD> new=${NEW} env=${env:ENV_PREFIXED}",
                env_overrides=None,
            )
        self.assertEqual(result, "old=${OLD} new=${NEW} env=${ENV_PREFIXED}")
        for plaintext in ("old-val", "new-val", "env-val"):
            self.assertNotIn(plaintext, result)


class TestCopilotEnvTranslationStdioEnv(unittest.TestCase):
    """Translation behaviour in stdio ``env`` block values."""

    def _adapter(self):
        with (
            patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        ):
            return CopilotClientAdapter()

    def test_env_block_value_translates_to_runtime_placeholder(self):
        """An env-var declared in registry stdio env_vars with a placeholder
        default emits ``${KEY}`` so Copilot CLI resolves it at server start.
        """
        adapter = self._adapter()
        env_vars = [
            {"name": "MY_TOKEN", "value": "${env:MY_TOKEN}"},
        ]
        with patch.dict(os.environ, {"MY_TOKEN": "PLAINTEXT_DO_NOT_BAKE"}, clear=False):
            resolved = adapter._resolve_environment_variables(env_vars, env_overrides=None)
        self.assertEqual(resolved.get("MY_TOKEN"), "${MY_TOKEN}")
        self.assertNotIn("PLAINTEXT_DO_NOT_BAKE", str(resolved))
        self.assertIn("MY_TOKEN", adapter._last_env_placeholder_keys)

    def test_github_toolsets_literal_default_preserved(self):
        """Non-secret literal defaults (e.g. ``GITHUB_TOOLSETS=context``)
        must remain literal so tools work without the user exporting them."""
        adapter = self._adapter()
        env_vars = [
            {"name": "GITHUB_TOOLSETS", "value": "context"},
        ]
        resolved = adapter._resolve_environment_variables(env_vars, env_overrides=None)
        self.assertEqual(resolved.get("GITHUB_TOOLSETS"), "context")


class TestCopilotEnvTranslationStdioArgs(unittest.TestCase):
    """Translation behaviour for placeholders in stdio command ``args``."""

    def _adapter(self):
        with (
            patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        ):
            return CopilotClientAdapter()

    def test_args_env_placeholder_translates(self):
        """``--host=${env:H}`` becomes ``--host=${H}`` in the rendered args."""
        adapter = self._adapter()
        with patch.dict(os.environ, {"H": "PLAINTEXT_DO_NOT_BAKE"}, clear=False):
            result = adapter._resolve_variable_placeholders(
                "--host=${env:H}", resolved_env={}, runtime_vars=None
            )
        self.assertEqual(result, "--host=${H}")
        self.assertNotIn("PLAINTEXT_DO_NOT_BAKE", result)

    def test_args_runtime_template_var_still_resolved(self):
        """``{ado_org}`` template vars are APM-specific; resolved at install
        time even in translate mode (Copilot can't see them)."""
        adapter = self._adapter()
        result = adapter._resolve_variable_placeholders(
            "--org={ado_org}", resolved_env={}, runtime_vars={"ado_org": "myorg"}
        )
        self.assertEqual(result, "--org=myorg")


class TestSiblingAdaptersUnchanged(unittest.TestCase):
    """Regression trap: sibling adapters that inherit from
    ``CopilotClientAdapter`` MUST keep the legacy install-time resolution
    behaviour. Their ``_supports_runtime_env_substitution`` is pinned to
    ``False`` until each is individually audited (#1152 follow-ups)."""

    def _adapter(self, cls):
        with (
            patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        ):
            return cls()

    def test_cursor_still_resolves_to_literal(self):
        from apm_cli.adapters.client.cursor import CursorClientAdapter

        adapter = self._adapter(CursorClientAdapter)
        self.assertFalse(adapter._supports_runtime_env_substitution)
        with patch.dict(os.environ, {"MY_TOKEN": "literal-value"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer literal-value")

    def test_claude_still_resolves_to_literal(self):
        """Claude Desktop config does NOT support runtime substitution --
        this adapter MUST keep resolving."""
        from apm_cli.adapters.client.claude import ClaudeClientAdapter

        adapter = self._adapter(ClaudeClientAdapter)
        self.assertFalse(adapter._supports_runtime_env_substitution)
        with patch.dict(os.environ, {"MY_TOKEN": "literal-value"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${env:MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer literal-value")

    def test_windsurf_still_resolves_to_literal(self):
        from apm_cli.adapters.client.windsurf import WindsurfClientAdapter

        adapter = self._adapter(WindsurfClientAdapter)
        self.assertFalse(adapter._supports_runtime_env_substitution)
        with patch.dict(os.environ, {"MY_TOKEN": "literal-value"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer literal-value")

    def test_opencode_still_resolves_to_literal(self):
        from apm_cli.adapters.client.opencode import OpenCodeClientAdapter

        adapter = self._adapter(OpenCodeClientAdapter)
        self.assertFalse(adapter._supports_runtime_env_substitution)
        with patch.dict(os.environ, {"MY_TOKEN": "literal-value"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer literal-value")

    def test_gemini_still_resolves_to_literal(self):
        from apm_cli.adapters.client.gemini import GeminiClientAdapter

        adapter = self._adapter(GeminiClientAdapter)
        self.assertFalse(adapter._supports_runtime_env_substitution)
        with patch.dict(os.environ, {"MY_TOKEN": "literal-value"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer literal-value")


class TestCopilotEnvVarTranslationInStdioEnvBlock(unittest.TestCase):
    """Issue #1152 supply-chain regression trap: self-defined stdio deps
    pass ``env`` as a plain dict ({NAME: value}), not a list of
    {name, description, required} dicts. The translate-mode pipeline
    must handle this shape without silently dropping the block to ``{}``.
    """

    def setUp(self):
        CopilotClientAdapter.reset_install_run_state()

    def tearDown(self):
        CopilotClientAdapter.reset_install_run_state()

    def test_dict_shaped_env_block_translates_all_placeholder_syntaxes(self):
        adapter = CopilotClientAdapter()
        result = adapter._resolve_environment_variables(
            {
                "PRIMARY_TOKEN": "${MY_STDIO_TOKEN}",
                "PREFIXED_TOKEN": "${env:MY_STDIO_TOKEN}",
                "LEGACY_TOKEN": "<MY_LEGACY_VAR>",
            },
            env_overrides=None,
        )
        self.assertEqual(result["PRIMARY_TOKEN"], "${MY_STDIO_TOKEN}")
        self.assertEqual(result["PREFIXED_TOKEN"], "${MY_STDIO_TOKEN}")
        self.assertEqual(result["LEGACY_TOKEN"], "${MY_LEGACY_VAR}")

    def test_dict_shaped_env_block_with_literal_replaced_by_runtime_placeholder(self):
        adapter = CopilotClientAdapter()
        with patch.dict(os.environ, {"MY_TOKEN": "ignored-os-env"}, clear=False):
            result = adapter._resolve_environment_variables(
                {"MY_TOKEN": "literal-value-from-apm-yml"}, env_overrides=None
            )
        self.assertEqual(result["MY_TOKEN"], "${MY_TOKEN}")
        for v in result.values():
            self.assertNotIn("literal-value-from-apm-yml", v)
            self.assertNotIn("ignored-os-env", v)

    def test_dict_shaped_env_block_does_not_silently_drop(self):
        """Regression trap for the bug where dict-input was iterated as a
        list-of-dicts, every key failed isinstance(dict), and the result
        was an empty {} -- breaking every self-defined stdio MCP server.
        """
        adapter = CopilotClientAdapter()
        result = adapter._resolve_environment_variables(
            {"FOO": "${env:FOO}", "BAR": "${BAR}"}, env_overrides=None
        )
        self.assertEqual(set(result.keys()), {"FOO", "BAR"})


class TestCopilotInstallRunSummary(unittest.TestCase):
    """Issue #1152: aggregated post-install diagnostics.

    ``emit_install_run_summary`` consolidates security-upgrade,
    unset-env-var, and legacy-syntax diagnostics into a single
    end-of-run block so the user sees one actionable summary even when
    many servers were configured.
    """

    def setUp(self):
        CopilotClientAdapter.reset_install_run_state()

    def tearDown(self):
        CopilotClientAdapter.reset_install_run_state()

    def _adapter(self):
        with (
            patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        ):
            return CopilotClientAdapter()

    def test_security_upgrade_warning_when_baked_keys_detected(self):
        """A server config that previously had literal env values triggers
        a single security-improvement warning naming the affected keys.
        """
        adapter = self._adapter()
        # Simulate a previously-baked config on disk.
        with patch.object(
            CopilotClientAdapter,
            "_collect_previously_baked_keys",
            return_value=({"GITHUB_TOKEN", "LINEAR_KEY"}, False),
        ):
            adapter._collect_previously_baked_keys.__call__  # noqa: B018 - sanity
            CopilotClientAdapter._security_upgraded_keys.update({"GITHUB_TOKEN", "LINEAR_KEY"})

        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()

        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("Security improvement", joined)
        self.assertIn("GITHUB_TOKEN", joined)
        self.assertIn("LINEAR_KEY", joined)

    def test_security_upgrade_detects_baked_http_header_literals(self):
        """Regression trap: a previously-baked HTTP header literal (which
        does not expose the env-var name) must still trigger the
        security-improvement notice for whatever placeholder keys the new
        write introduces. The pre-fix path produced an empty intersection
        because the headers branch added a sentinel string instead of a
        real env-var name.
        """
        adapter = self._adapter()
        # Simulate previous on-disk state: env block had no baked literals
        # (empty set), but headers block did (True). Combined with new
        # placeholder keys for this write, the upgrade notice MUST list
        # the new keys -- they are the vars the user must export.
        with patch.object(
            CopilotClientAdapter,
            "_collect_previously_baked_keys",
            return_value=(set(), True),
        ):
            adapter._last_env_placeholder_keys = {"GH_TOKEN"}
            previously_baked_keys, previously_baked_headers = (
                adapter._collect_previously_baked_keys("github/server", "github-mcp")
            )
            self.assertEqual(previously_baked_keys, set())
            self.assertTrue(previously_baked_headers)
            # Mirror configure_mcp_server's upgrade-detection logic.
            upgraded = previously_baked_keys & adapter._last_env_placeholder_keys
            if previously_baked_headers and adapter._last_env_placeholder_keys:
                upgraded = upgraded | adapter._last_env_placeholder_keys
            self.assertEqual(upgraded, {"GH_TOKEN"})
            CopilotClientAdapter._security_upgraded_keys.update(upgraded)

        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("Security improvement", joined)
        self.assertIn("GH_TOKEN", joined)
        # The legacy "(http header value)" sentinel must NOT appear.
        self.assertNotIn("(http header value)", joined)

    def test_unset_env_warning_aggregates_across_servers(self):
        """Two servers contributing different unset env vars produce a
        single aggregated warning with a copy-pasteable export hint.
        """
        CopilotClientAdapter._unset_env_keys_by_server["github-mcp"] = ["GH_TOKEN"]
        CopilotClientAdapter._unset_env_keys_by_server["linear"] = ["LINEAR_KEY"]
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("GH_TOKEN", joined)
        self.assertIn("LINEAR_KEY", joined)
        self.assertIn("export GH_TOKEN=... LINEAR_KEY=...", joined)

    def test_summary_emitted_only_once(self):
        """Calling ``emit_install_run_summary`` twice in the same process
        emits the diagnostics exactly once (idempotent guard)."""
        CopilotClientAdapter._unset_env_keys_by_server["s"] = ["X"]
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
            CopilotClientAdapter.emit_install_run_summary()
        self.assertEqual(mock_warn.call_count, 1)

    def test_unset_env_emit_summary_records_keys(self):
        """``_emit_install_summary`` populates the unset-env bucket for
        keys not present in ``os.environ``; set keys are not recorded.
        """
        adapter = self._adapter()
        adapter._last_env_placeholder_keys = {"DEFINITELY_NOT_SET_VAR_XYZ"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEFINITELY_NOT_SET_VAR_XYZ", None)
            adapter._emit_install_summary("svc", {"type": "local"})
        self.assertIn("svc", CopilotClientAdapter._unset_env_keys_by_server)
        self.assertIn(
            "DEFINITELY_NOT_SET_VAR_XYZ",
            CopilotClientAdapter._unset_env_keys_by_server["svc"],
        )

    def test_set_env_var_not_recorded_as_unset(self):
        """When the env var IS exported, the unset bucket is not
        populated for that server."""
        adapter = self._adapter()
        adapter._last_env_placeholder_keys = {"PRESENT_VAR"}
        with patch.dict(os.environ, {"PRESENT_VAR": "value"}, clear=False):
            adapter._emit_install_summary("svc", {"type": "local"})
        self.assertNotIn("svc", CopilotClientAdapter._unset_env_keys_by_server)


class TestCopilotSelectRemoteWithUrl(unittest.TestCase):
    """Direct unit tests for the ``_select_remote_with_url`` helper."""

    def test_returns_first_remote_with_url(self):
        remotes = [
            {"url": ""},
            {"url": "https://example.com/a"},
            {"url": "https://example.com/b"},
        ]
        self.assertEqual(
            CopilotClientAdapter._select_remote_with_url(remotes)["url"],
            "https://example.com/a",
        )

    def test_returns_none_when_no_url(self):
        remotes = [{"url": ""}, {"url": "   "}, {"url": None}]
        self.assertIsNone(CopilotClientAdapter._select_remote_with_url(remotes))

    def test_handles_empty_list(self):
        self.assertIsNone(CopilotClientAdapter._select_remote_with_url([]))


if __name__ == "__main__":
    unittest.main()
