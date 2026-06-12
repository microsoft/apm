"""Phase-3 unit tests for apm_cli.adapters.client.copilot.

Covers uncovered paths identified from the coverage report — specifically:
- Module-level helper functions with non-string input
- get_config_path / update_config / get_current_config
- configure_mcp_server branches (empty URL, None server_info, server_name vs. URL
  key, runtime-env aggregation, exception handler)
- _collect_previously_baked_keys branches
- _emit_install_summary branches
- emit_install_run_summary (legacy-angle summary)
- _format_server_config (_raw_stdio, no packages/remotes, tools_override)
- _dispatch_package_to_config (npm, docker, fallback)
- _select_and_dispatch_best_package (None-package early return)
- _resolve_environment_variables legacy mode (dict + list)
- _resolve_env_variable legacy mode (env_overrides, os.environ, skip-prompting)
- _inject_env_vars_into_docker_args / _inject_docker_env_vars
- _process_arguments (positional, named, string)
- _resolve_variable_placeholders (empty, legacy mode, runtime_vars)
- _resolve_env_placeholders (compat wrapper)
- _select_best_package priority ordering
- _is_github_server edge cases
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from apm_cli.adapters.client.copilot import (
    CopilotClientAdapter,
    _extract_legacy_angle_vars,
    _has_env_placeholder,
    _translate_env_placeholder,
)

# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------


def _make_adapter(**kwargs) -> CopilotClientAdapter:
    """Return a CopilotClientAdapter with all external deps mocked out."""
    with (
        patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
    ):
        return CopilotClientAdapter(**kwargs)


# ---------------------------------------------------------------------------
# Module-level helper functions: non-string input
# ---------------------------------------------------------------------------


class TestModuleLevelHelpers(unittest.TestCase):
    """Branch: non-string input returns early / default."""

    def test_translate_env_placeholder_non_string_passthrough(self) -> None:
        """_translate_env_placeholder returns non-string values unchanged (line 63)."""
        self.assertIsNone(_translate_env_placeholder(None))
        self.assertEqual(_translate_env_placeholder(42), 42)
        self.assertEqual(_translate_env_placeholder(3.14), 3.14)
        self.assertEqual(_translate_env_placeholder(True), True)

    def test_extract_legacy_angle_vars_non_string_returns_empty_set(self) -> None:
        """_extract_legacy_angle_vars returns empty set for non-string (line 81)."""
        self.assertEqual(_extract_legacy_angle_vars(None), set())
        self.assertEqual(_extract_legacy_angle_vars(42), set())
        self.assertEqual(_extract_legacy_angle_vars(["<VAR>"]), set())

    def test_has_env_placeholder_non_string_returns_false(self) -> None:
        """_has_env_placeholder returns False for non-string (line 92)."""
        self.assertFalse(_has_env_placeholder(None))
        self.assertFalse(_has_env_placeholder(0))
        self.assertFalse(_has_env_placeholder(["${VAR}"]))

    def test_translate_env_placeholder_string_translations(self) -> None:
        """String inputs are translated correctly."""
        self.assertEqual(_translate_env_placeholder("${env:FOO}"), "${FOO}")
        self.assertEqual(_translate_env_placeholder("<BAR>"), "${BAR}")
        self.assertEqual(_translate_env_placeholder("${BAZ}"), "${BAZ}")
        # passthrough: default syntax
        self.assertEqual(_translate_env_placeholder("${X:-default}"), "${X:-default}")

    def test_has_env_placeholder_true_cases(self) -> None:
        self.assertTrue(_has_env_placeholder("${FOO}"))
        self.assertTrue(_has_env_placeholder("${env:BAR}"))
        self.assertTrue(_has_env_placeholder("<MY_VAR>"))

    def test_has_env_placeholder_false_for_plain_string(self) -> None:
        self.assertFalse(_has_env_placeholder("plain literal"))


# ---------------------------------------------------------------------------
# get_config_path
# ---------------------------------------------------------------------------


class TestGetConfigPath(unittest.TestCase):
    def test_returns_path_under_home_copilot(self) -> None:
        adapter = _make_adapter()
        config_path = adapter.get_config_path()
        self.assertIn(".copilot", config_path)
        self.assertTrue(config_path.endswith("mcp-config.json"))


# ---------------------------------------------------------------------------
# get_current_config
# ---------------------------------------------------------------------------


class TestGetCurrentConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = _make_adapter()
        self.patcher = patch("apm_cli.adapters.client.copilot.CopilotClientAdapter.get_config_path")
        self.mock_path = self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()

    def test_returns_empty_dict_when_file_missing(self) -> None:
        """Line 222: non-existent path returns {}."""
        self.mock_path.return_value = "/nonexistent/path/mcp-config.json"
        result = self.adapter.get_current_config()
        self.assertEqual(result, {})

    def test_returns_config_when_file_valid(self) -> None:
        """Lines 225-227: existing valid JSON is loaded."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"mcpServers": {"s": {"type": "local"}}}, f)
            tmp = f.name
        try:
            self.mock_path.return_value = tmp
            result = self.adapter.get_current_config()
            self.assertIn("mcpServers", result)
        finally:
            os.unlink(tmp)

    def test_returns_empty_dict_on_json_decode_error(self) -> None:
        """Lines 228-229: invalid JSON falls back to {}."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json")
            tmp = f.name
        try:
            self.mock_path.return_value = tmp
            result = self.adapter.get_current_config()
            self.assertEqual(result, {})
        finally:
            os.unlink(tmp)

    def test_returns_empty_dict_on_os_error(self) -> None:
        """Lines 228-229: OSError on open falls back to {}."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            tmp = f.name
        # file exists but open will raise
        try:
            self.mock_path.return_value = tmp
            with patch("builtins.open", side_effect=OSError("permission denied")):
                result = self.adapter.get_current_config()
            self.assertEqual(result, {})
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# update_config
# ---------------------------------------------------------------------------


class TestUpdateConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp_dir, "mcp-config.json")
        self.adapter = _make_adapter()
        self.patcher = patch(
            "apm_cli.adapters.client.copilot.CopilotClientAdapter.get_config_path",
            return_value=self.config_path,
        )
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_creates_file_and_mcpservers_key_when_absent(self) -> None:
        """Lines 199-212: mcpServers key is created when absent."""
        # Start with no file on disk — get_current_config will return {}
        self.adapter.update_config({"my-server": {"type": "local"}})
        with open(self.config_path) as f:
            saved = json.load(f)
        self.assertIn("mcpServers", saved)
        self.assertIn("my-server", saved["mcpServers"])

    def test_merges_into_existing_config(self) -> None:
        """update_config merges updates into existing mcpServers dict."""
        with open(self.config_path, "w") as f:
            json.dump({"mcpServers": {"existing": {"type": "local"}}}, f)
        self.adapter.update_config({"new-server": {"type": "http"}})
        with open(self.config_path) as f:
            saved = json.load(f)
        self.assertIn("existing", saved["mcpServers"])
        self.assertIn("new-server", saved["mcpServers"])


# ---------------------------------------------------------------------------
# configure_mcp_server
# ---------------------------------------------------------------------------


class TestConfigureMcpServer(unittest.TestCase):
    def setUp(self) -> None:
        CopilotClientAdapter.reset_install_run_state()
        self.tmp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp_dir, "mcp-config.json")
        with open(self.config_path, "w") as f:
            json.dump({"mcpServers": {}}, f)
        self.adapter = _make_adapter()
        self.path_patcher = patch(
            "apm_cli.adapters.client.copilot.CopilotClientAdapter.get_config_path",
            return_value=self.config_path,
        )
        self.path_patcher.start()

    def tearDown(self) -> None:
        self.path_patcher.stop()
        CopilotClientAdapter.reset_install_run_state()
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_empty_server_url_returns_false(self) -> None:
        """Line 256-258: empty server_url returns False immediately."""
        result = self.adapter.configure_mcp_server("")
        self.assertFalse(result)

    def test_none_server_url_returns_false(self) -> None:
        result = self.adapter.configure_mcp_server(None)
        self.assertFalse(result)

    def test_returns_false_when_server_info_is_none(self) -> None:
        """Line 262-263: _fetch_server_info returns None → configure returns False."""
        with patch.object(self.adapter, "_fetch_server_info", return_value=None):
            result = self.adapter.configure_mcp_server("vendor/my-server")
        self.assertFalse(result)

    def test_uses_provided_server_name_as_config_key(self) -> None:
        """Line 286-288: explicit server_name overrides URL-derived key."""
        server_info = {
            "id": "srv-id",
            "name": "my-server",
            "remotes": [{"url": "https://example.com/mcp"}],
        }
        with patch.object(self.adapter, "_fetch_server_info", return_value=server_info):
            result = self.adapter.configure_mcp_server("vendor/my-server", server_name="custom-key")
        self.assertTrue(result)
        with open(self.config_path) as f:
            saved = json.load(f)
        self.assertIn("custom-key", saved["mcpServers"])

    def test_derives_config_key_from_url_after_slash(self) -> None:
        """Line 293-294: key is the part after the last slash in server_url."""
        server_info = {
            "id": "srv-id",
            "name": "my-server",
            "remotes": [{"url": "https://example.com/mcp"}],
        }
        with patch.object(self.adapter, "_fetch_server_info", return_value=server_info):
            result = self.adapter.configure_mcp_server("vendor/my-server")
        self.assertTrue(result)
        with open(self.config_path) as f:
            saved = json.load(f)
        self.assertIn("my-server", saved["mcpServers"])

    def test_uses_full_url_as_key_when_no_slash(self) -> None:
        """Line 295-297: fallback to full server_url when no slash."""
        server_info = {
            "id": "srv-id",
            "name": "my-server",
            "remotes": [{"url": "https://example.com/mcp"}],
        }
        with patch.object(self.adapter, "_fetch_server_info", return_value=server_info):
            result = self.adapter.configure_mcp_server("noslash-server")
        self.assertTrue(result)
        with open(self.config_path) as f:
            saved = json.load(f)
        self.assertIn("noslash-server", saved["mcpServers"])

    def test_exception_in_format_returns_false_and_prints_error(self) -> None:
        """Lines 323-325: exception inside try block returns False."""
        with patch.object(self.adapter, "_fetch_server_info", return_value={"id": "x"}):
            with patch.object(
                self.adapter,
                "_format_server_config",
                side_effect=RuntimeError("boom"),
            ):
                result = self.adapter.configure_mcp_server("vendor/boom-server")
        self.assertFalse(result)

    def test_aggregates_legacy_angle_offenders(self) -> None:
        """Lines 303-307: _last_legacy_angle_vars populates class-level bucket."""
        server_info = {
            "id": "srv-id",
            "name": "legacy-srv",
            "remotes": [{"url": "https://example.com/mcp"}],
        }
        with patch.object(self.adapter, "_fetch_server_info", return_value=server_info):
            # Inject a legacy var into the per-server tracker before write
            def _inject_legacy(si, env_overrides=None, runtime_vars=None):
                self.adapter._last_legacy_angle_vars = {"OLD_TOKEN"}
                return {"type": "http", "url": "https://example.com/mcp"}

            with patch.object(self.adapter, "_format_server_config", side_effect=_inject_legacy):
                self.adapter.configure_mcp_server("vendor/legacy-srv")

        self.assertIn("legacy-srv", CopilotClientAdapter._legacy_angle_offenders_by_server)
        self.assertIn(
            "OLD_TOKEN",
            CopilotClientAdapter._legacy_angle_offenders_by_server["legacy-srv"],
        )

    def test_security_upgrade_detected_when_headers_were_baked(self) -> None:
        """Lines 314-317: previously_baked_headers + placeholder keys → security upgrade."""
        server_info = {
            "id": "srv-id",
            "name": "test-srv",
            "remotes": [{"url": "https://example.com/mcp"}],
        }
        with patch.object(self.adapter, "_fetch_server_info", return_value=server_info):
            with patch.object(
                self.adapter,
                "_collect_previously_baked_keys",
                return_value=(set(), True),
            ):

                def _set_placeholder(si, env_overrides=None, runtime_vars=None):
                    self.adapter._last_env_placeholder_keys = {"GH_TOKEN"}
                    return {"type": "http", "url": "https://example.com/mcp"}

                with patch.object(
                    self.adapter, "_format_server_config", side_effect=_set_placeholder
                ):
                    self.adapter.configure_mcp_server("vendor/test-srv")

        self.assertIn("GH_TOKEN", CopilotClientAdapter._security_upgraded_keys)

    def test_security_upgrade_when_baked_keys_overlap(self) -> None:
        """Lines 313-317: intersection of previously_baked_keys and placeholder_keys."""
        server_info = {
            "id": "srv-id",
            "name": "overlap-srv",
            "remotes": [{"url": "https://example.com/mcp"}],
        }
        with patch.object(self.adapter, "_fetch_server_info", return_value=server_info):
            with patch.object(
                self.adapter,
                "_collect_previously_baked_keys",
                return_value=({"MY_KEY"}, False),
            ):

                def _set_placeholder(si, env_overrides=None, runtime_vars=None):
                    self.adapter._last_env_placeholder_keys = {"MY_KEY"}
                    return {"type": "http", "url": "https://example.com/mcp"}

                with patch.object(
                    self.adapter, "_format_server_config", side_effect=_set_placeholder
                ):
                    self.adapter.configure_mcp_server("vendor/overlap-srv")

        self.assertIn("MY_KEY", CopilotClientAdapter._security_upgraded_keys)


# ---------------------------------------------------------------------------
# _collect_previously_baked_keys
# ---------------------------------------------------------------------------


class TestCollectPreviouslyBakedKeys(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = _make_adapter()

    def test_exception_in_get_current_config_returns_empty(self) -> None:
        """Lines 338-339: exception in get_current_config returns (set(), False)."""
        with patch.object(self.adapter, "get_current_config", side_effect=RuntimeError("io error")):
            result = self.adapter._collect_previously_baked_keys("vendor/srv", None)
        self.assertEqual(result, (set(), False))

    def test_uses_server_name_as_key_when_provided(self) -> None:
        """Line 342: server_name takes precedence over URL-derived key."""
        current = {"mcpServers": {"my-key": {"type": "local", "env": {"TOKEN": "literal-value"}}}}
        with patch.object(self.adapter, "get_current_config", return_value=current):
            keys, headers_baked = self.adapter._collect_previously_baked_keys(
                "vendor/srv", "my-key"
            )
        self.assertIn("TOKEN", keys)
        self.assertFalse(headers_baked)

    def test_derives_key_from_url_when_no_server_name(self) -> None:
        """Line 344-345: key is derived from slash-split of server_url."""
        current = {"mcpServers": {"my-server": {"type": "local", "env": {"SECRET": "baked"}}}}
        with patch.object(self.adapter, "get_current_config", return_value=current):
            keys, _ = self.adapter._collect_previously_baked_keys("vendor/my-server", None)
        self.assertIn("SECRET", keys)

    def test_uses_full_url_when_no_slash(self) -> None:
        """Line 346-347: no-slash server_url used as full key."""
        current = {"mcpServers": {"noslash": {"type": "local", "env": {"KEY": "val"}}}}
        with patch.object(self.adapter, "get_current_config", return_value=current):
            keys, _ = self.adapter._collect_previously_baked_keys("noslash", None)
        self.assertIn("KEY", keys)

    def test_returns_empty_when_existing_is_not_dict(self) -> None:
        """Line 349-350: non-dict existing entry returns (set(), False)."""
        current = {"mcpServers": {"srv": "not-a-dict"}}
        with patch.object(self.adapter, "get_current_config", return_value=current):
            keys, headers_baked = self.adapter._collect_previously_baked_keys("vendor/srv", None)
        self.assertEqual(keys, set())
        self.assertFalse(headers_baked)

    def test_env_with_placeholder_value_not_counted_as_baked(self) -> None:
        """Env value that IS a placeholder is not counted as baked literal."""
        current = {"mcpServers": {"srv": {"type": "local", "env": {"TOKEN": "${TOKEN}"}}}}
        with patch.object(self.adapter, "get_current_config", return_value=current):
            keys, _ = self.adapter._collect_previously_baked_keys("vendor/srv", None)
        self.assertNotIn("TOKEN", keys)

    def test_headers_with_literal_value_marks_baked(self) -> None:
        """Lines 358-363: literal header value sets headers_were_baked=True."""
        current = {
            "mcpServers": {
                "srv": {
                    "type": "http",
                    "headers": {"Authorization": "Bearer literal-token"},
                }
            }
        }
        with patch.object(self.adapter, "get_current_config", return_value=current):
            _, headers_baked = self.adapter._collect_previously_baked_keys("vendor/srv", None)
        self.assertTrue(headers_baked)

    def test_headers_with_placeholder_not_baked(self) -> None:
        """Header value that IS a placeholder is not counted as baked."""
        current = {
            "mcpServers": {
                "srv": {
                    "type": "http",
                    "headers": {"Authorization": "Bearer ${GH_TOKEN}"},
                }
            }
        }
        with patch.object(self.adapter, "get_current_config", return_value=current):
            _, headers_baked = self.adapter._collect_previously_baked_keys("vendor/srv", None)
        self.assertFalse(headers_baked)


# ---------------------------------------------------------------------------
# _emit_install_summary
# ---------------------------------------------------------------------------


class TestEmitInstallSummary(unittest.TestCase):
    def setUp(self) -> None:
        CopilotClientAdapter.reset_install_run_state()

    def tearDown(self) -> None:
        CopilotClientAdapter.reset_install_run_state()

    def test_no_op_when_runtime_substitution_not_supported(self) -> None:
        """Line 373-374: _emit_install_summary is a no-op when flag is False."""
        with (
            patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        ):
            # Create an adapter with runtime substitution disabled by monkeypatching
            adapter = _make_adapter()
            adapter._supports_runtime_env_substitution = False
        adapter._last_env_placeholder_keys = {"SOME_KEY"}
        adapter._emit_install_summary("svc", {"type": "local"})
        # No unset keys should be recorded since we returned early
        self.assertNotIn("svc", CopilotClientAdapter._unset_env_keys_by_server)

    def test_records_vars_from_env_block_in_server_config(self) -> None:
        """Lines 376-384: vars scanned from env block of server_config."""
        adapter = _make_adapter()
        adapter._last_env_placeholder_keys = set()
        server_config = {"type": "local", "env": {"MYVAR": "${MYVAR}"}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MYVAR", None)
            adapter._emit_install_summary("svc2", server_config)
        self.assertIn("svc2", CopilotClientAdapter._unset_env_keys_by_server)
        self.assertIn("MYVAR", CopilotClientAdapter._unset_env_keys_by_server["svc2"])

    def test_records_vars_from_headers_block_in_server_config(self) -> None:
        """Lines 376-384: vars scanned from headers block of server_config."""
        adapter = _make_adapter()
        adapter._last_env_placeholder_keys = set()
        server_config = {
            "type": "http",
            "headers": {"Authorization": "Bearer ${HEADER_TOKEN}"},
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEADER_TOKEN", None)
            adapter._emit_install_summary("svc3", server_config)
        self.assertIn("svc3", CopilotClientAdapter._unset_env_keys_by_server)
        self.assertIn("HEADER_TOKEN", CopilotClientAdapter._unset_env_keys_by_server["svc3"])


# ---------------------------------------------------------------------------
# emit_install_run_summary — legacy angle offenders
# ---------------------------------------------------------------------------


class TestEmitInstallRunSummaryLegacyAngle(unittest.TestCase):
    def setUp(self) -> None:
        CopilotClientAdapter.reset_install_run_state()

    def tearDown(self) -> None:
        CopilotClientAdapter.reset_install_run_state()

    def test_legacy_angle_offenders_warning_emitted(self) -> None:
        """Lines 463-473: legacy angle offenders trigger deprecation warning."""
        CopilotClientAdapter._legacy_angle_offenders_by_server["legacy-srv"] = {"OLD_VAR"}
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("Deprecated", joined)
        self.assertIn("legacy-srv", joined)

    def test_no_warnings_when_all_state_empty(self) -> None:
        """emit_install_run_summary emits nothing when all buckets are empty."""
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        mock_warn.assert_not_called()
        self.assertTrue(CopilotClientAdapter._install_run_summary_emitted)

    def test_reset_install_run_state_clears_all_buckets(self) -> None:
        """Lines 481-484: reset_install_run_state clears every class-level bucket."""
        CopilotClientAdapter._security_upgraded_keys.add("K")
        CopilotClientAdapter._unset_env_keys_by_server["s"] = ["X"]
        CopilotClientAdapter._legacy_angle_offenders_by_server["s2"] = {"Y"}
        CopilotClientAdapter._install_run_summary_emitted = True

        CopilotClientAdapter.reset_install_run_state()

        self.assertEqual(CopilotClientAdapter._security_upgraded_keys, set())
        self.assertEqual(CopilotClientAdapter._unset_env_keys_by_server, {})
        self.assertEqual(CopilotClientAdapter._legacy_angle_offenders_by_server, {})
        self.assertFalse(CopilotClientAdapter._install_run_summary_emitted)

    def test_singular_noun_for_single_unset_var(self) -> None:
        """The unset-env warning uses 'variable' (singular) for one var."""
        CopilotClientAdapter._unset_env_keys_by_server["s"] = ["SOLO"]
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("variable", joined)
        self.assertIn("SOLO", joined)

    def test_plural_noun_for_multiple_unset_vars(self) -> None:
        """The unset-env warning uses 'variables' (plural) for multiple vars."""
        CopilotClientAdapter._unset_env_keys_by_server["s"] = ["A", "B"]
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        joined = "\n".join(call.args[0] for call in mock_warn.call_args_list)
        self.assertIn("variables", joined)


# ---------------------------------------------------------------------------
# _format_server_config — _raw_stdio path
# ---------------------------------------------------------------------------


class TestFormatServerConfigRawStdio(unittest.TestCase):
    def setUp(self) -> None:
        CopilotClientAdapter.reset_install_run_state()

    def tearDown(self) -> None:
        CopilotClientAdapter.reset_install_run_state()

    def _adapter(self) -> CopilotClientAdapter:
        return _make_adapter()

    def test_raw_stdio_without_env_or_args(self) -> None:
        """Lines 511-532: _raw_stdio path with no env and no args."""
        server_info = {
            "id": "s1",
            "name": "stdio-srv",
            "_raw_stdio": {"command": "node", "args": []},
        }
        config = self._adapter()._format_server_config(server_info)
        self.assertEqual(config["command"], "node")
        self.assertEqual(config["args"], [])

    def test_raw_stdio_with_env_vars_translated(self) -> None:
        """Lines 515-520: env dict from _raw_stdio is resolved/translated."""
        server_info = {
            "id": "s2",
            "name": "stdio-env-srv",
            "_raw_stdio": {
                "command": "python",
                "args": [],
                "env": {"MY_KEY": "${MY_KEY}"},
            },
        }
        config = self._adapter()._format_server_config(server_info)
        # In translate mode ${MY_KEY} passes through as ${MY_KEY}
        self.assertEqual(config["env"]["MY_KEY"], "${MY_KEY}")

    def test_raw_stdio_with_args_containing_placeholder(self) -> None:
        """Lines 521-527: args are route through _resolve_variable_placeholders."""
        server_info = {
            "id": "s3",
            "name": "stdio-args-srv",
            "_raw_stdio": {
                "command": "node",
                "args": ["--token=${env:MY_TOKEN}", 42],
            },
        }
        config = self._adapter()._format_server_config(server_info)
        self.assertIn("--token=${MY_TOKEN}", config["args"])
        # Non-string args passed through unchanged
        self.assertIn(42, config["args"])

    def test_raw_stdio_with_tools_override(self) -> None:
        """Line 530-531: _apm_tools_override is applied for _raw_stdio."""
        server_info = {
            "id": "s4",
            "name": "stdio-tools-srv",
            "_raw_stdio": {"command": "node", "args": []},
            "_apm_tools_override": ["read_file", "write_file"],
        }
        config = self._adapter()._format_server_config(server_info)
        self.assertEqual(config["tools"], ["read_file", "write_file"])


# ---------------------------------------------------------------------------
# _format_server_config — remote path with tools_override
# ---------------------------------------------------------------------------


class TestFormatServerConfigRemoteToolsOverride(unittest.TestCase):
    def test_tools_override_applied_on_remote_path(self) -> None:
        """Line 571-572: _apm_tools_override is applied for remote path."""
        adapter = _make_adapter()
        server_info = {
            "id": "r1",
            "name": "remote-srv",
            "remotes": [{"url": "https://example.com/mcp"}],
            "_apm_tools_override": ["list_files"],
        }
        config = adapter._format_server_config(server_info)
        self.assertEqual(config["tools"], ["list_files"])


# ---------------------------------------------------------------------------
# _format_server_config — no packages, no remotes
# ---------------------------------------------------------------------------


class TestFormatServerConfigNoPackagesNoRemotes(unittest.TestCase):
    def test_raises_value_error_when_no_packages_and_no_remotes(self) -> None:
        """Lines 579-586: missing both packages and remotes raises ValueError."""
        adapter = _make_adapter()
        server_info = {"id": "incomplete", "name": "bad-srv", "packages": [], "remotes": []}
        with self.assertRaises(ValueError) as ctx:
            adapter._format_server_config(server_info)
        self.assertIn("bad-srv", str(ctx.exception))
        self.assertIn("incomplete", str(ctx.exception).lower())

    def test_packages_path_applies_tools_override(self) -> None:
        """Lines 593-595: tools_override on packages path."""
        adapter = _make_adapter()
        server_info = {
            "id": "pkg1",
            "name": "npm-srv",
            "packages": [
                {"registry_name": "npm", "name": "@scope/tool", "environment_variables": []}
            ],
            "_apm_tools_override": ["search"],
        }
        config = adapter._format_server_config(server_info)
        self.assertEqual(config["tools"], ["search"])


# ---------------------------------------------------------------------------
# _dispatch_package_to_config
# ---------------------------------------------------------------------------


class TestDispatchPackageToConfig(unittest.TestCase):
    def _adapter(self) -> CopilotClientAdapter:
        return _make_adapter()

    def test_npm_package_sets_command_npx(self) -> None:
        """Lines 637-643: npm registry uses npx."""
        adapter = self._adapter()
        config: dict = {"type": "local", "tools": ["*"]}
        adapter._dispatch_package_to_config(
            config,
            package_name="@scope/my-tool",
            registry_name="npm",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        self.assertEqual(config["command"], "npx")
        self.assertIn("@scope/my-tool", config["args"])
        self.assertIn("-y", config["args"])

    def test_npm_package_uses_runtime_hint_when_provided(self) -> None:
        """npm with runtime_hint overrides default 'npx'."""
        adapter = self._adapter()
        config: dict = {"type": "local", "tools": ["*"]}
        adapter._dispatch_package_to_config(
            config,
            package_name="@scope/tool",
            registry_name="npm",
            runtime_hint="bunx",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        self.assertEqual(config["command"], "bunx")

    def test_npm_with_env_sets_env_key(self) -> None:
        """npm resolved_env is attached under 'env' when non-empty."""
        adapter = self._adapter()
        config: dict = {"type": "local"}
        adapter._dispatch_package_to_config(
            config,
            package_name="@scope/tool",
            registry_name="npm",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={"MY_VAR": "${MY_VAR}"},
        )
        self.assertIn("env", config)
        self.assertEqual(config["env"]["MY_VAR"], "${MY_VAR}")

    def test_npm_empty_env_not_set(self) -> None:
        """npm with empty resolved_env omits the 'env' key."""
        adapter = self._adapter()
        config: dict = {"type": "local"}
        adapter._dispatch_package_to_config(
            config,
            package_name="@scope/tool",
            registry_name="npm",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        self.assertNotIn("env", config)

    def test_docker_package_sets_command_docker(self) -> None:
        """Lines 644-653: docker registry produces docker command."""
        adapter = self._adapter()
        config: dict = {"type": "local"}
        adapter._dispatch_package_to_config(
            config,
            package_name="ghcr.io/org/image:latest",
            registry_name="docker",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        self.assertEqual(config["command"], "docker")
        self.assertIn("run", config["args"])

    def test_docker_with_runtime_args_injects_env_vars(self) -> None:
        """docker with processed_runtime_args delegates to _inject_env_vars_into_docker_args."""
        adapter = self._adapter()
        config: dict = {"type": "local"}
        adapter._dispatch_package_to_config(
            config,
            package_name="ghcr.io/org/image",
            registry_name="docker",
            runtime_hint="",
            processed_runtime_args=["run", "-i", "--rm", "ghcr.io/org/image"],
            processed_package_args=[],
            resolved_env={"API_KEY": "${API_KEY}"},
        )
        self.assertEqual(config["command"], "docker")

    def test_pypi_registry_delegates_to_apply_pypi_homebrew(self) -> None:
        """Lines 654-663: non-npm/docker delegates to _apply_pypi_homebrew_generic_config."""
        adapter = self._adapter()
        config: dict = {"type": "local"}
        with patch.object(adapter, "_apply_pypi_homebrew_generic_config") as mock_apply:
            adapter._dispatch_package_to_config(
                config,
                package_name="my-package",
                registry_name="pypi",
                runtime_hint="uvx",
                processed_runtime_args=[],
                processed_package_args=[],
                resolved_env={},
            )
        mock_apply.assert_called_once()


# ---------------------------------------------------------------------------
# _select_and_dispatch_best_package
# ---------------------------------------------------------------------------


class TestSelectAndDispatchBestPackage(unittest.TestCase):
    def test_returns_none_when_no_best_package(self) -> None:
        """Lines 691-692: _select_best_package returning None causes early return None."""
        adapter = _make_adapter()
        config: dict = {"type": "local"}
        with patch.object(adapter, "_select_best_package", return_value=None):
            result = adapter._select_and_dispatch_best_package(config, [], None, None)
        self.assertIsNone(result)

    def test_set_type_stdio_flag_sets_config_type(self) -> None:
        """Lines 709-710: set_type_stdio=True updates config['type']."""
        adapter = _make_adapter()
        config: dict = {"type": "local"}
        packages = [{"registry_name": "npm", "name": "@scope/tool", "environment_variables": []}]
        with patch.object(adapter, "_dispatch_package_to_config"):
            adapter._select_and_dispatch_best_package(
                config, packages, None, None, set_type_stdio=True
            )
        self.assertEqual(config["type"], "stdio")


# ---------------------------------------------------------------------------
# _resolve_environment_variables — legacy mode (non-translate)
# ---------------------------------------------------------------------------


class TestResolveEnvironmentVariablesLegacyMode(unittest.TestCase):
    """Tests for branches reached when _supports_runtime_env_substitution=False."""

    def _legacy_adapter(self) -> CopilotClientAdapter:
        adapter = _make_adapter()
        adapter._supports_runtime_env_substitution = False
        return adapter

    def test_dict_env_resolves_string_via_resolve_env_variable(self) -> None:
        """Lines 813-824: dict-shaped env in legacy mode calls _resolve_env_variable."""
        adapter = self._legacy_adapter()
        with patch.dict(os.environ, {"MY_SECRET": "real-secret"}, clear=False):
            result = adapter._resolve_environment_variables(
                {"MY_SECRET": "${MY_SECRET}"}, env_overrides=None
            )
        self.assertEqual(result["MY_SECRET"], "real-secret")

    def test_dict_env_legacy_stringifies_non_string_non_none(self) -> None:
        """Lines 822-823: non-string, non-None values are stringified."""
        adapter = self._legacy_adapter()
        result = adapter._resolve_environment_variables(
            {"PORT": 8080, "DEBUG": True}, env_overrides=None
        )
        self.assertEqual(result["PORT"], "8080")
        self.assertEqual(result["DEBUG"], "true")

    def test_dict_env_legacy_omits_none(self) -> None:
        """Line 821: None values are omitted from the result dict."""
        adapter = self._legacy_adapter()
        result = adapter._resolve_environment_variables({"OPTIONAL": None}, env_overrides=None)
        self.assertNotIn("OPTIONAL", result)

    def test_list_env_legacy_delegates_to_resolve_env_vars_with_prompting(self) -> None:
        """Line 826: list-shaped env in legacy mode calls _resolve_env_vars_with_prompting."""
        adapter = self._legacy_adapter()
        with patch.object(
            adapter,
            "_resolve_env_vars_with_prompting",
            return_value={"VAR": "val"},
        ) as mock_fn:
            result = adapter._resolve_environment_variables(
                [{"name": "VAR", "description": "desc", "required": True}],
                env_overrides=None,
            )
        mock_fn.assert_called_once()
        self.assertEqual(result, {"VAR": "val"})

    def test_translate_mode_list_env_default_github_env_preserved(self) -> None:
        """Lines 799-801: GITHUB_TOOLSETS/GITHUB_DYNAMIC_TOOLSETS stay literal in translate mode."""
        adapter = _make_adapter()  # translate mode (default)
        env_vars = [
            {"name": "GITHUB_TOOLSETS", "description": "", "required": False},
            {"name": "GITHUB_DYNAMIC_TOOLSETS", "description": "", "required": False},
        ]
        result = adapter._resolve_environment_variables(env_vars, env_overrides=None)
        self.assertEqual(result["GITHUB_TOOLSETS"], "context")
        self.assertEqual(result["GITHUB_DYNAMIC_TOOLSETS"], "1")

    def test_translate_mode_list_env_regular_var_gets_placeholder(self) -> None:
        """Lines 804-807: normal env vars get ${NAME} placeholder in translate mode."""
        adapter = _make_adapter()
        env_vars = [{"name": "MY_TOKEN", "description": "token", "required": True}]
        result = adapter._resolve_environment_variables(env_vars, env_overrides=None)
        self.assertEqual(result["MY_TOKEN"], "${MY_TOKEN}")
        self.assertIn("MY_TOKEN", adapter._last_env_placeholder_keys)

    def test_translate_mode_dict_env_skips_none_value(self) -> None:
        """Line 768-769: None values in dict env are skipped."""
        adapter = _make_adapter()
        result = adapter._resolve_environment_variables(
            {"OPTIONAL": None, "PRESENT": "${PRESENT}"}, env_overrides=None
        )
        self.assertNotIn("OPTIONAL", result)
        self.assertIn("PRESENT", result)

    def test_translate_mode_dict_env_skips_empty_name(self) -> None:
        """Lines 766-767: entries with empty/falsy key are skipped."""
        adapter = _make_adapter()
        result = adapter._resolve_environment_variables({"": "${SOMETHING}"}, env_overrides=None)
        self.assertEqual(result, {})

    def test_translate_mode_list_env_skips_non_dict_items(self) -> None:
        """Lines 794-795: non-dict items in the list are skipped."""
        adapter = _make_adapter()
        result = adapter._resolve_environment_variables(["not-a-dict", 42], env_overrides=None)
        self.assertEqual(result, {})

    def test_translate_mode_list_env_skips_empty_name(self) -> None:
        """Lines 797-798: items with empty name are skipped."""
        adapter = _make_adapter()
        result = adapter._resolve_environment_variables(
            [{"name": "", "description": ""}], env_overrides=None
        )
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# _resolve_env_variable — legacy mode
# ---------------------------------------------------------------------------


class TestResolveEnvVariableLegacyMode(unittest.TestCase):
    """Lines 854-901: legacy (non-translate) resolve path."""

    def _legacy_adapter(self) -> CopilotClientAdapter:
        adapter = _make_adapter()
        adapter._supports_runtime_env_substitution = False
        return adapter

    def test_resolves_from_env_overrides(self) -> None:
        """Lines 869-871: env_overrides take precedence over os.environ."""
        adapter = self._legacy_adapter()
        with patch.dict(os.environ, {"MY_VAR": "os-value"}, clear=False):
            result = adapter._resolve_env_variable(
                "MY_VAR", "${MY_VAR}", env_overrides={"MY_VAR": "override-value"}
            )
        self.assertEqual(result, "override-value")

    def test_resolves_from_os_environ_when_no_override(self) -> None:
        """Lines 889-890: falls back to os.environ when no override."""
        adapter = self._legacy_adapter()
        with patch.dict(os.environ, {"MY_ENV_VAR": "env-val"}, clear=False):
            result = adapter._resolve_env_variable("MY_ENV_VAR", "${MY_ENV_VAR}", env_overrides={})
        self.assertEqual(result, "env-val")

    def test_skips_prompt_in_non_interactive_env(self) -> None:
        """Lines 878-880: non-TTY environment skips prompting, returns placeholder."""
        adapter = self._legacy_adapter()
        # Unset the variable to force the prompt path
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNSET_LEGACY_VAR", None)
            with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
                mock_stdin.isatty.return_value = False
                mock_stdout.isatty.return_value = False
                result = adapter._resolve_env_variable(
                    "UNSET_LEGACY_VAR", "${UNSET_LEGACY_VAR}", env_overrides={}
                )
        # Returns original placeholder since no value found and prompting skipped
        self.assertEqual(result, "${UNSET_LEGACY_VAR}")

    def test_skips_prompt_when_e2e_flag_set(self) -> None:
        """Line 874-875: APM_E2E_TESTS=1 forces skip_prompting=True."""
        adapter = self._legacy_adapter()
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}, clear=False):
            os.environ.pop("E2E_UNSET_VAR", None)
            result = adapter._resolve_env_variable(
                "E2E_UNSET_VAR", "${E2E_UNSET_VAR}", env_overrides={}
            )
        # Should return original placeholder, not prompt
        self.assertEqual(result, "${E2E_UNSET_VAR}")

    def test_skips_prompt_when_env_overrides_provided(self) -> None:
        """Line 871: skip_prompting=True when env_overrides dict is non-empty."""
        adapter = self._legacy_adapter()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNSET_VAR_2", None)
            result = adapter._resolve_env_variable(
                "UNSET_VAR_2", "${UNSET_VAR_2}", env_overrides={"OTHER": "x"}
            )
        # env_overrides provided (even without this key), so skip_prompting=True
        self.assertEqual(result, "${UNSET_VAR_2}")

    def test_legacy_angle_bracket_resolves(self) -> None:
        """Legacy <VAR> syntax resolves via os.environ in legacy mode."""
        adapter = self._legacy_adapter()
        with patch.dict(os.environ, {"LEGACYVAR": "resolved-val"}, clear=False):
            result = adapter._resolve_env_variable("LEGACYVAR", "<LEGACYVAR>", env_overrides={})
        self.assertEqual(result, "resolved-val")


# ---------------------------------------------------------------------------
# _inject_env_vars_into_docker_args
# ---------------------------------------------------------------------------


class TestInjectEnvVarsIntoDockerArgs(unittest.TestCase):
    def _adapter(self) -> CopilotClientAdapter:
        return _make_adapter()

    def test_adds_i_and_rm_flags_when_missing(self) -> None:
        """Lines 938-944: -i and --rm added after 'run' when absent."""
        adapter = self._adapter()
        result = adapter._inject_env_vars_into_docker_args(["run", "my-image"], env_vars={})
        self.assertIn("-i", result)
        self.assertIn("--rm", result)

    def test_does_not_duplicate_i_flag(self) -> None:
        """Lines 938-939: -i not added when already present."""
        adapter = self._adapter()
        result = adapter._inject_env_vars_into_docker_args(
            ["run", "-i", "--rm", "my-image"], env_vars={}
        )
        self.assertEqual(result.count("-i"), 1)
        self.assertEqual(result.count("--rm"), 1)

    def test_env_var_name_placeholder_replaced_with_e_flag(self) -> None:
        """Lines 947-950: env var name in args becomes -e NAME=value."""
        adapter = self._adapter()
        result = adapter._inject_env_vars_into_docker_args(
            ["run", "MY_VAR", "my-image"],
            env_vars={"MY_VAR": "secret"},
        )
        self.assertIn("-e", result)
        self.assertIn("MY_VAR=secret", result)
        # Original placeholder removed
        self.assertNotIn("MY_VAR", [a for a in result if a not in ["-e", "MY_VAR=secret"]])

    def test_e_flag_followed_by_var_name_replaced(self) -> None:
        """Lines 951-956: -e followed by env var name is replaced with -e NAME=value."""
        adapter = self._adapter()
        result = adapter._inject_env_vars_into_docker_args(
            ["run", "-e", "API_KEY", "my-image"],
            env_vars={"API_KEY": "key-value"},
        )
        self.assertIn("API_KEY=key-value", result)

    def test_env_vars_not_in_template_appended(self) -> None:
        """Lines 964-981: extra env vars not in template are appended."""
        adapter = self._adapter()
        result = adapter._inject_env_vars_into_docker_args(
            ["run", "--rm", "my-image"],
            env_vars={"EXTRA_VAR": "extra-value"},
        )
        self.assertIn("-e", result)
        self.assertIn("EXTRA_VAR=extra-value", result)

    def test_empty_env_vars_returns_base_args_with_flags(self) -> None:
        """Empty env_vars still adds -i and --rm."""
        adapter = self._adapter()
        result = adapter._inject_env_vars_into_docker_args(["run", "img"], env_vars=None)
        self.assertIn("-i", result)

    def test_interactive_flag_recognised(self) -> None:
        """Lines 927-928: '--interactive' is recognised as the -i flag."""
        adapter = self._adapter()
        result = adapter._inject_env_vars_into_docker_args(
            ["run", "--interactive", "--rm", "img"], env_vars={}
        )
        self.assertEqual(result.count("-i"), 0)  # was not added since --interactive present


# ---------------------------------------------------------------------------
# _inject_docker_env_vars
# ---------------------------------------------------------------------------


class TestInjectDockerEnvVars(unittest.TestCase):
    def _adapter(self) -> CopilotClientAdapter:
        return _make_adapter()

    def test_injects_env_vars_after_run(self) -> None:
        """Lines 1010-1019: env vars injected after 'run' in args."""
        adapter = self._adapter()
        result = adapter._inject_docker_env_vars(["docker", "run", "my-image"], {"SECRET": "value"})
        run_idx = result.index("run")
        self.assertIn("-e", result[run_idx:])
        self.assertIn("SECRET=value", result[run_idx:])

    def test_no_injection_when_no_run_command(self) -> None:
        """No modification when 'run' is not in args."""
        adapter = self._adapter()
        result = adapter._inject_docker_env_vars(["docker", "exec", "container"], {"KEY": "val"})
        self.assertNotIn("-e", result)

    def test_empty_env_vars_no_modification(self) -> None:
        """Empty env_vars dict: args returned as-is with no injection."""
        adapter = self._adapter()
        original = ["docker", "run", "my-image"]
        result = adapter._inject_docker_env_vars(original, {})
        self.assertEqual(result, original)


# ---------------------------------------------------------------------------
# _process_arguments
# ---------------------------------------------------------------------------


class TestProcessArguments(unittest.TestCase):
    def _adapter(self) -> CopilotClientAdapter:
        return _make_adapter()

    def test_empty_arguments_returns_empty_list(self) -> None:
        adapter = self._adapter()
        self.assertEqual(adapter._process_arguments([]), [])

    def test_positional_arg_resolved(self) -> None:
        """Lines 1043-1050: positional arg extracted and placeholders resolved."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "positional", "value": "my-value"}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["my-value"])

    def test_positional_arg_with_runtime_var(self) -> None:
        """Lines 1043-1050: positional arg uses runtime_vars."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "positional", "value": "--org={ado_org}"}],
            resolved_env={},
            runtime_vars={"ado_org": "myorg"},
        )
        self.assertEqual(result, ["--org=myorg"])

    def test_positional_arg_uses_default_when_no_value(self) -> None:
        """Lines 1044: default is used when value is absent."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "positional", "default": "fallback"}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["fallback"])

    def test_positional_empty_value_skipped(self) -> None:
        """Line 1045: empty value (no default) produces no output."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "positional", "value": ""}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, [])

    def test_named_arg_appends_name_and_value(self) -> None:
        """Lines 1051-1062: named arg appends both the flag and its value."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "named", "name": "--host", "value": "localhost"}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["--host", "localhost"])

    def test_named_arg_with_empty_value_only_appends_name(self) -> None:
        """Lines 1058: empty value means only the flag name is appended."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "named", "name": "--verbose", "value": ""}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["--verbose"])

    def test_named_arg_skips_when_no_name(self) -> None:
        """Lines 1054: empty name → item skipped."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "named", "name": "", "value": "something"}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, [])

    def test_string_arg_passed_through_with_placeholder_translation(self) -> None:
        """Lines 1063-1068: plain string args go through _resolve_variable_placeholders."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            ["--token=${env:MY_TOKEN}"],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["--token=${MY_TOKEN}"])

    def test_named_arg_value_same_as_name_not_appended(self) -> None:
        """Lines 1058: value == name means no separate value append."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "named", "name": "--flag", "value": "--flag"}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["--flag"])

    def test_named_arg_value_starts_with_dash_not_appended(self) -> None:
        """Lines 1058: value starting with '-' is not appended as a value."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"type": "named", "name": "--opt", "value": "--other"}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["--opt"])


# ---------------------------------------------------------------------------
# _resolve_variable_placeholders
# ---------------------------------------------------------------------------


class TestResolveVariablePlaceholders(unittest.TestCase):
    def _adapter(self) -> CopilotClientAdapter:
        return _make_adapter()

    def _legacy_adapter(self) -> CopilotClientAdapter:
        adapter = _make_adapter()
        adapter._supports_runtime_env_substitution = False
        return adapter

    def test_empty_string_returns_empty_string(self) -> None:
        """Line 1101-1102: empty / falsy value is returned as-is."""
        adapter = self._adapter()
        self.assertEqual(adapter._resolve_variable_placeholders("", {}, {}), "")
        self.assertEqual(adapter._resolve_variable_placeholders(None, {}, {}), None)

    def test_translate_mode_translates_env_placeholder(self) -> None:
        """Lines 1106-1110: translate mode converts ${env:X} → ${X}."""
        adapter = self._adapter()
        result = adapter._resolve_variable_placeholders("--token=${env:MY_TOKEN}", {}, {})
        self.assertEqual(result, "--token=${MY_TOKEN}")

    def test_legacy_mode_resolves_angle_bracket_from_resolved_env(self) -> None:
        """Lines 1112-1119: legacy mode replaces <VAR> from resolved_env."""
        adapter = self._legacy_adapter()
        result = adapter._resolve_variable_placeholders(
            "--key=<API_KEY>", {"API_KEY": "literal"}, {}
        )
        self.assertEqual(result, "--key=literal")

    def test_legacy_mode_keeps_angle_bracket_when_not_in_resolved_env(self) -> None:
        """Lines 1116-1117: unresolved <VAR> is kept as-is in legacy mode."""
        adapter = self._legacy_adapter()
        result = adapter._resolve_variable_placeholders("--key=<UNKNOWN>", {}, {})
        self.assertEqual(result, "--key=<UNKNOWN>")

    def test_runtime_var_resolved_at_install_time(self) -> None:
        """Lines 1125-1132: {runtime_var} is always substituted."""
        adapter = self._adapter()
        result = adapter._resolve_variable_placeholders("--org={my_org}", {}, {"my_org": "acme"})
        self.assertEqual(result, "--org=acme")

    def test_runtime_var_not_substituted_when_no_runtime_vars(self) -> None:
        """Lines 1125: runtime_vars is empty so {} template is left unchanged."""
        adapter = self._adapter()
        result = adapter._resolve_variable_placeholders("--org={my_org}", {}, {})
        self.assertEqual(result, "--org={my_org}")

    def test_dollar_brace_not_confused_with_runtime_var(self) -> None:
        """The negative lookbehind prevents ${VAR} from matching as {VAR}."""
        adapter = self._adapter()
        result = adapter._resolve_variable_placeholders(
            "--token=${MY_TOKEN}", {}, {"MY_TOKEN": "should-not-match"}
        )
        # ${MY_TOKEN} should NOT be resolved via runtime_vars
        self.assertEqual(result, "--token=${MY_TOKEN}")


# ---------------------------------------------------------------------------
# _resolve_env_placeholders (compat wrapper)
# ---------------------------------------------------------------------------


class TestResolveEnvPlaceholders(unittest.TestCase):
    def test_delegates_to_resolve_variable_placeholders(self) -> None:
        """Line 1138: _resolve_env_placeholders calls _resolve_variable_placeholders."""
        adapter = _make_adapter()
        with patch.object(adapter, "_resolve_variable_placeholders", return_value="ok") as mock_rvp:
            result = adapter._resolve_env_placeholders("${env:X}", {"X": "${X}"})
        mock_rvp.assert_called_once_with("${env:X}", {"X": "${X}"}, {})
        self.assertEqual(result, "ok")


# ---------------------------------------------------------------------------
# _select_best_package
# ---------------------------------------------------------------------------


class TestSelectBestPackage(unittest.TestCase):
    def _adapter(self) -> CopilotClientAdapter:
        return _make_adapter()

    def test_empty_packages_returns_none(self) -> None:
        adapter = self._adapter()
        self.assertIsNone(adapter._select_best_package([]))

    def test_npm_preferred_over_docker(self) -> None:
        """Lines 1169-1174: npm is prioritised before docker."""
        adapter = self._adapter()
        packages = [
            {"registry_name": "docker", "name": "img"},
            {"registry_name": "npm", "name": "@scope/pkg"},
        ]
        best = adapter._select_best_package(packages)
        self.assertEqual(best["registry_name"], "npm")

    def test_docker_preferred_over_pypi(self) -> None:
        adapter = self._adapter()
        packages = [
            {"registry_name": "pypi", "name": "pypkg"},
            {"registry_name": "docker", "name": "img"},
        ]
        best = adapter._select_best_package(packages)
        self.assertEqual(best["registry_name"], "docker")

    def test_first_package_returned_when_none_in_priority(self) -> None:
        """Lines 1176-1177: no priority match → first package returned."""
        adapter = self._adapter()
        packages = [{"registry_name": "homebrew", "name": "brew-pkg"}]
        best = adapter._select_best_package(packages)
        self.assertEqual(best["registry_name"], "homebrew")


# ---------------------------------------------------------------------------
# _is_github_server — edge cases
# ---------------------------------------------------------------------------


class TestIsGithubServerEdgeCases(unittest.TestCase):
    def _adapter(self) -> CopilotClientAdapter:
        return _make_adapter()

    def test_githubcopilot_com_hostname_accepted(self) -> None:
        """Lines 1210-1212: githubcopilot.com subdomains are accepted."""
        adapter = self._adapter()
        self.assertTrue(
            adapter._is_github_server("github-mcp-server", "https://api.githubcopilot.com/mcp")
        )

    def test_githubcopilot_com_root_hostname_accepted(self) -> None:
        adapter = self._adapter()
        self.assertTrue(
            adapter._is_github_server("github-mcp-server", "https://githubcopilot.com/mcp")
        )

    def test_api_github_com_subdomain_accepted(self) -> None:
        """Lines 1208-1209: *.github.com subdomains are accepted."""
        adapter = self._adapter()
        self.assertTrue(
            adapter._is_github_server("github-mcp-server", "https://api.github.com/mcp")
        )

    def test_non_https_url_rejected(self) -> None:
        """Lines 1224-1225: non-HTTPS URL returns False immediately."""
        adapter = self._adapter()
        self.assertFalse(adapter._is_github_server("github-mcp-server", "ftp://api.github.com/mcp"))

    def test_empty_url_returns_false(self) -> None:
        """Lines 1220: empty/None URL → hostname is None → host_matches=False."""
        adapter = self._adapter()
        self.assertFalse(adapter._is_github_server("github-mcp-server", ""))
        self.assertFalse(adapter._is_github_server("github-mcp-server", None))

    def test_name_mismatch_returns_false_even_with_valid_host(self) -> None:
        """Lines 1214-1216: name not in allowlist → False."""
        adapter = self._adapter()
        self.assertFalse(adapter._is_github_server("evil-server", "https://api.github.com/mcp"))

    def test_unknown_hostname_returns_false(self) -> None:
        """host_matches=False when hostname is not in GitHub allowlist."""
        adapter = self._adapter()
        self.assertFalse(
            adapter._is_github_server("github-mcp-server", "https://attacker.example.com/mcp")
        )


if __name__ == "__main__":
    unittest.main()
