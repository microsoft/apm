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


class TestCopilotEnvVarResolutionInHeaders(unittest.TestCase):
    """Issue #944: ``${VAR}`` and ``${env:VAR}`` in headers are install-time resolved.

    Copilot CLI's mcp-config.json has no runtime env interpolation, so APM bakes
    the actual value in. The legacy ``<VAR>`` syntax already worked; these tests
    cover the new ``${VAR}`` and ``${env:VAR}`` syntaxes added for #944. Together
    with the existing ``<VAR>`` path, the three syntaxes share the same
    env_overrides -> os.environ -> prompt resolution flow.
    """

    def _adapter(self):
        with (
            patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        ):
            return CopilotClientAdapter()

    def test_resolves_bare_dollar_brace_var(self):
        adapter = self._adapter()
        with patch.dict(os.environ, {"MY_TOKEN": "secret-xyz"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer secret-xyz")

    def test_resolves_env_prefixed_var(self):
        """``${env:VAR}`` (VS Code-flavored) also resolves to the host env value."""
        adapter = self._adapter()
        with patch.dict(os.environ, {"MY_TOKEN": "secret-xyz"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer ${env:MY_TOKEN}", env_overrides=None
            )
        self.assertEqual(result, "Bearer secret-xyz")

    def test_legacy_angle_bracket_still_works(self):
        """Regression: ``<VAR>`` legacy syntax must keep functioning."""
        adapter = self._adapter()
        with patch.dict(os.environ, {"MY_TOKEN": "secret-xyz"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization", "Bearer <MY_TOKEN>", env_overrides=None
            )
        self.assertEqual(result, "Bearer secret-xyz")

    def test_env_overrides_take_precedence(self):
        """``env_overrides`` wins over ``os.environ``, identical to legacy behavior."""
        adapter = self._adapter()
        with patch.dict(os.environ, {"MY_TOKEN": "from-env"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization",
                "Bearer ${MY_TOKEN}",
                env_overrides={"MY_TOKEN": "from-overrides"},
            )
        self.assertEqual(result, "Bearer from-overrides")

    def test_unresolvable_passes_through(self):
        """Unset vars survive verbatim in non-interactive (env_overrides supplied) mode."""
        adapter = self._adapter()
        # Make sure target var is not in env
        with patch.dict(os.environ, {}, clear=True):
            result = adapter._resolve_env_variable(
                "Authorization",
                "Bearer ${MISSING_VAR}",
                env_overrides={"OTHER": "x"},  # presence forces non-interactive path
            )
        self.assertEqual(result, "Bearer ${MISSING_VAR}")

    def test_input_syntax_is_not_resolved(self):
        """``${input:...}`` must NOT be resolved here -- it's runtime-prompted by VS Code."""
        adapter = self._adapter()
        with patch.dict(os.environ, {"input": "should-not-match"}, clear=False):
            result = adapter._resolve_env_variable(
                "Authorization",
                "Bearer ${input:my-token}",
                env_overrides={"OTHER": "x"},
            )
        self.assertEqual(result, "Bearer ${input:my-token}")

    def test_github_actions_template_is_not_touched(self):
        """``${{ secrets.X }}`` (GHA template) must pass through unchanged."""
        adapter = self._adapter()
        result = adapter._resolve_env_variable(
            "Authorization",
            "Bearer ${{ secrets.GITHUB_TOKEN }}",
            env_overrides={"OTHER": "x"},
        )
        self.assertEqual(result, "Bearer ${{ secrets.GITHUB_TOKEN }}")

    def test_resolved_value_is_not_recursively_expanded(self):
        """Regression guard: a resolved value containing placeholder-like text
        must NOT be re-scanned for further substitution.

        Mirrors the original ``<VAR>``-only semantics where each placeholder is
        resolved exactly once. Important for tokens/values that legitimately
        contain ``${...}`` literal text (e.g. regex patterns, templated strings).
        """
        adapter = self._adapter()
        with patch.dict(
            os.environ,
            {"OUTER": "literal-${INNER}", "INNER": "should-not-appear"},
            clear=False,
        ):
            # Test all three placeholder syntaxes for symmetry.
            for syntax in ("<OUTER>", "${OUTER}", "${env:OUTER}"):
                with self.subTest(syntax=syntax):
                    result = adapter._resolve_env_variable(
                        "Authorization", syntax, env_overrides={"OTHER": "x"}
                    )
                    self.assertEqual(result, "literal-${INNER}")

    def test_mixed_syntaxes_in_one_value(self):
        """A header may mix legacy and new placeholders; all should resolve."""
        adapter = self._adapter()
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
        self.assertEqual(result, "old=old-val new=new-val env=env-val")


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
