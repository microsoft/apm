"""Tests for VS Code adapter handling of Docker runtimeArguments with variables (MCP registry v0.1).

Issue #1391: VS Code adapter drops Docker `runtimeArguments` with `variables`.
"""

import unittest
from unittest.mock import patch

from apm_cli.adapters.client.vscode import VSCodeClientAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# v0.1 real shape: `variables` is a SIBLING of `value_hint`.
# The `value_hint` string contains {var_name} placeholders that get substituted.
V01_DOCKER_PACKAGE = {
    "name": "ghcr.io/example/playwright-mcp:1.2.3",
    "registry_name": "docker",
    "runtime_hint": "docker",
    "runtime_arguments": [
        {"value_hint": "run"},
        {"value_hint": "-i"},
        {"value_hint": "--rm"},
        {"value_hint": "-v"},
        {
            "value_hint": "{workspaceFolder}:/workspace",
            "variables": {
                "workspaceFolder": {
                    "description": "Workspace folder path",
                    "is_required": True,
                }
            },
        },
        {"value_hint": "-w"},
        {"value_hint": "/workspace"},
        {"value_hint": "ghcr.io/example/playwright-mcp:1.2.3"},
    ],
}

LEGACY_PACKAGE = {
    "name": "some-server",
    "registry_name": "npm",
    "runtime_hint": "npx",
    "runtime_arguments": [
        {"is_required": True, "value_hint": "server"},
        {"is_required": True, "value_hint": "start"},
    ],
}

PACKAGE_ARGUMENTS_PACKAGE = {
    "name": "@mcp/fetch",
    "registry_name": "npm",
    "runtime_hint": "npx",
    "package_arguments": [
        {"type": "positional", "value": "--port"},
        {"type": "positional", "value": "3000"},
    ],
}


# ---------------------------------------------------------------------------
# Unit tests for _extract_package_args
# ---------------------------------------------------------------------------


class TestExtractPackageArgsVariables(unittest.TestCase):
    """Tests for _extract_package_args with v0.1 variables shape."""

    def test_variables_workspaceFolder_defaults_to_vscode_token(self):
        """workspaceFolder placeholder in value_hint -> ${workspaceFolder}:/workspace."""
        result = VSCodeClientAdapter._extract_package_args(V01_DOCKER_PACKAGE)
        self.assertIn("run", result)
        self.assertIn("-i", result)
        self.assertIn("--rm", result)
        self.assertIn("-v", result)
        self.assertIn("${workspaceFolder}:/workspace", result)
        self.assertIn("-w", result)
        self.assertIn("/workspace", result)
        self.assertIn("ghcr.io/example/playwright-mcp:1.2.3", result)

    def test_variables_workspaceFolder_substituted_in_place(self):
        """workspaceFolder placeholder produces a single combined mount arg."""
        result = VSCodeClientAdapter._extract_package_args(V01_DOCKER_PACKAGE)
        mount_args = [a for a in result if "workspaceFolder" in a or ":/workspace" in a]
        self.assertEqual(len(mount_args), 1)
        self.assertEqual(mount_args[0], "${workspaceFolder}:/workspace")

    def test_variables_with_runtime_vars_substitution(self):
        """Provided runtime_vars values are substituted into the value_hint string."""
        runtime_vars = {"workspaceFolder": "/home/user/project"}
        result = VSCodeClientAdapter._extract_package_args(
            V01_DOCKER_PACKAGE, runtime_vars=runtime_vars
        )
        self.assertNotIn("${workspaceFolder}:/workspace", result)
        self.assertIn("/home/user/project:/workspace", result)

    def test_variables_unknown_var_uses_placeholder(self):
        """Unknown variable names get a ${varName} placeholder inside the value_hint."""
        pkg = {
            "name": "some-image",
            "registry_name": "docker",
            "runtime_hint": "docker",
            "runtime_arguments": [
                {
                    "value_hint": "{customVar}:/data",
                    "variables": {
                        "customVar": {"description": "Some custom var", "is_required": True}
                    },
                }
            ],
        }
        result = VSCodeClientAdapter._extract_package_args(pkg)
        self.assertEqual(result, ["${customVar}:/data"])

    def test_variables_unknown_var_resolved_from_runtime_vars(self):
        """Unknown variable is resolved from runtime_vars into the value_hint string."""
        pkg = {
            "name": "some-image",
            "registry_name": "docker",
            "runtime_hint": "docker",
            "runtime_arguments": [
                {
                    "value_hint": "{customVar}:/data",
                    "variables": {"customVar": {"description": "Custom", "is_required": True}},
                }
            ],
        }
        result = VSCodeClientAdapter._extract_package_args(
            pkg, runtime_vars={"customVar": "/custom/path"}
        )
        self.assertEqual(result, ["/custom/path:/data"])

    def test_legacy_is_required_still_works(self):
        """Existing is_required/value_hint format still produces correct args (regression guard)."""
        result = VSCodeClientAdapter._extract_package_args(LEGACY_PACKAGE)
        self.assertEqual(result, ["server", "start"])

    def test_package_arguments_still_take_priority(self):
        """package_arguments format takes precedence over runtime_arguments."""
        pkg = {
            "name": "something",
            "package_arguments": [{"type": "positional", "value": "run"}],
            "runtime_arguments": [{"is_required": True, "value_hint": "old"}],
        }
        result = VSCodeClientAdapter._extract_package_args(pkg)
        self.assertEqual(result, ["run"])

    def test_package_arguments_format(self):
        """package_arguments values are extracted correctly."""
        result = VSCodeClientAdapter._extract_package_args(PACKAGE_ARGUMENTS_PACKAGE)
        self.assertEqual(result, ["--port", "3000"])

    def test_is_required_false_entries_skipped(self):
        """Entries with is_required=False and value_hint are excluded (backward compat)."""
        pkg = {
            "name": "tool",
            "runtime_arguments": [
                {"is_required": True, "value_hint": "run"},
                {"is_required": False, "value_hint": "--optional-flag"},
            ],
        }
        result = VSCodeClientAdapter._extract_package_args(pkg)
        self.assertEqual(result, ["run"])

    def test_empty_package(self):
        self.assertEqual(VSCodeClientAdapter._extract_package_args({}), [])

    def test_none_package(self):
        self.assertEqual(VSCodeClientAdapter._extract_package_args(None), [])


# ---------------------------------------------------------------------------
# Integration tests for _format_server_config with Docker + variables
# ---------------------------------------------------------------------------


def _make_adapter():
    """Create a VSCodeClientAdapter with mocked registry."""
    with (
        patch("apm_cli.adapters.client.vscode.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.vscode.RegistryIntegration"),
    ):
        adapter = VSCodeClientAdapter(project_root="/tmp/workspace")
    return adapter


class TestFormatServerConfigDockerVariables(unittest.TestCase):
    """End-to-end _format_server_config tests for Docker MCP servers with variables."""

    def setUp(self):
        self.adapter = _make_adapter()
        self.server_info = {
            "id": "playwright-mcp",
            "name": "playwright-mcp",
            "packages": [V01_DOCKER_PACKAGE],
        }

    def test_docker_server_config_uses_workspaceFolder_token(self):
        """Docker server config substitutes {workspaceFolder} -> ${workspaceFolder}:/workspace."""
        config, _input_vars = self.adapter._format_server_config(self.server_info)
        self.assertEqual(config.get("command"), "docker")
        self.assertIn("${workspaceFolder}:/workspace", config.get("args", []))

    def test_docker_server_config_with_runtime_vars(self):
        """Docker server config substitutes workspaceFolder from runtime_vars."""
        runtime_vars = {"workspaceFolder": "/workspace/myproject"}
        config, _ = self.adapter._format_server_config(self.server_info, runtime_vars=runtime_vars)
        self.assertEqual(config.get("command"), "docker")
        args = config.get("args", [])
        self.assertIn("/workspace/myproject:/workspace", args)
        self.assertNotIn("${workspaceFolder}:/workspace", args)

    def test_docker_server_config_arg_order(self):
        """Docker args preserve the order defined in runtime_arguments."""
        config, _ = self.adapter._format_server_config(self.server_info)
        args = config.get("args", [])
        run_idx = args.index("run")
        rm_idx = args.index("--rm")
        v_idx = args.index("-v")
        self.assertLess(run_idx, rm_idx)
        self.assertLess(rm_idx, v_idx)

    def test_format_server_config_threads_runtime_vars(self):
        """Verify runtime_vars passed to _format_server_config reaches _extract_package_args."""
        runtime_vars = {"workspaceFolder": "/some/path"}
        with patch.object(
            VSCodeClientAdapter,
            "_extract_package_args",
            wraps=VSCodeClientAdapter._extract_package_args,
        ) as mock_extract:
            self.adapter._format_server_config(self.server_info, runtime_vars=runtime_vars)
            mock_extract.assert_called_once()
            _, kwargs = mock_extract.call_args
            self.assertEqual(kwargs.get("runtime_vars"), runtime_vars)


# ---------------------------------------------------------------------------
# configure_mcp_server passes runtime_vars through to _format_server_config
# ---------------------------------------------------------------------------


class TestConfigureMcpServerPassesRuntimeVars(unittest.TestCase):
    """Verify configure_mcp_server threads runtime_vars into _format_server_config."""

    def setUp(self):
        self.adapter = _make_adapter()
        self.server_info = {
            "id": "playwright-mcp",
            "name": "playwright-mcp",
            "packages": [V01_DOCKER_PACKAGE],
        }
        self.adapter.registry_client.find_server_by_reference.return_value = self.server_info

    def test_runtime_vars_forwarded_to_format_server_config(self):
        runtime_vars = {"workspaceFolder": "/repo"}

        with (
            patch.object(
                self.adapter,
                "_format_server_config",
                wraps=self.adapter._format_server_config,
            ) as mock_fmt,
            patch.object(self.adapter, "update_config", return_value=True),
            patch.object(
                self.adapter,
                "get_current_config",
                return_value={"servers": {}, "inputs": []},
            ),
        ):
            self.adapter.configure_mcp_server(
                "playwright-mcp",
                runtime_vars=runtime_vars,
            )
            mock_fmt.assert_called_once_with(
                self.server_info,
                runtime_vars=runtime_vars,
            )


if __name__ == "__main__":
    unittest.main()
