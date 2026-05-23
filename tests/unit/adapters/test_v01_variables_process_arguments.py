"""Regression tests for MCP v0.1 runtimeArguments.variables handling in non-vscode adapters.

Issue #1452: copilot and codex adapters' _process_arguments do not handle
the v0.1 format where a ``variables`` dict is a sibling of ``value_hint``
(no ``type`` key). This causes Docker mount args with {workspaceFolder}
placeholders to be silently dropped.

gemini, cursor, and claude inherit from CopilotClientAdapter, so fixing
copilot fixes all three.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter

# ---------------------------------------------------------------------------
# Shared v0.1 Docker fixture (same shape as real registry data)
# ---------------------------------------------------------------------------

V01_DOCKER_RUNTIME_ARGS = [
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
]


# ---------------------------------------------------------------------------
# Adapter factories
# ---------------------------------------------------------------------------


def _make_copilot(**kwargs) -> CopilotClientAdapter:
    with (
        patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
    ):
        return CopilotClientAdapter(**kwargs)


def _make_codex(tmp_path: Path | None = None) -> CodexClientAdapter:
    with (
        patch("apm_cli.adapters.client.codex.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.codex.RegistryIntegration"),
    ):
        return CodexClientAdapter(project_root=tmp_path)


# ---------------------------------------------------------------------------
# Copilot adapter
# ---------------------------------------------------------------------------


class TestCopilotProcessArgumentsV01Variables(unittest.TestCase):
    """_process_arguments must handle v0.1 value_hint + variables args."""

    def _adapter(self) -> CopilotClientAdapter:
        return _make_copilot()

    def test_v01_plain_value_hint_args_extracted(self):
        """Plain value_hint args (no variables, no type) are extracted."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"value_hint": "run"}, {"value_hint": "--rm"}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["run", "--rm"])

    def test_v01_variables_placeholder_resolved_from_runtime_vars(self):
        """v0.1 arg with variables dict resolves {workspaceFolder} from runtime_vars."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            V01_DOCKER_RUNTIME_ARGS,
            resolved_env={},
            runtime_vars={"workspaceFolder": "/home/user/project"},
        )
        self.assertIn("/home/user/project:/workspace", result)

    def test_v01_variables_unknown_var_gets_placeholder(self):
        """Unknown variable gets a ${varName} placeholder (same as vscode)."""
        adapter = self._adapter()
        args = [
            {
                "value_hint": "{customVar}:/data",
                "variables": {"customVar": {"description": "Custom path", "is_required": True}},
            }
        ]
        result = adapter._process_arguments(args, resolved_env={}, runtime_vars={})
        self.assertEqual(result, ["${customVar}:/data"])

    def test_v01_full_docker_arg_set_preserved(self):
        """All 8 args from the v0.1 Docker fixture are present."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            V01_DOCKER_RUNTIME_ARGS,
            resolved_env={},
            runtime_vars={"workspaceFolder": "/ws"},
        )
        self.assertEqual(len(result), 8)
        self.assertEqual(result[0], "run")
        self.assertEqual(result[1], "-i")
        self.assertEqual(result[2], "--rm")
        self.assertEqual(result[3], "-v")
        self.assertEqual(result[4], "/ws:/workspace")
        self.assertEqual(result[5], "-w")
        self.assertEqual(result[6], "/workspace")
        self.assertEqual(result[7], "ghcr.io/example/playwright-mcp:1.2.3")

    def test_legacy_optional_hint_skipped(self):
        """Legacy entries with is_required: False must not be appended."""
        adapter = self._adapter()
        args = [
            {"value_hint": "--optional-flag", "is_required": False},
            {"value_hint": "required-arg"},
        ]
        result = adapter._process_arguments(args, resolved_env={}, runtime_vars={})
        self.assertEqual(result, ["required-arg"])
        self.assertNotIn("--optional-flag", result)

    def test_legacy_optional_hint_with_variables_skipped(self):
        """Legacy entries with is_required: False and a variables dict are skipped."""
        adapter = self._adapter()
        args = [
            {
                "value_hint": "{optionalPath}:/data",
                "is_required": False,
                "variables": {
                    "optionalPath": {"description": "Optional mount", "is_required": False}
                },
            },
            {"value_hint": "required-arg"},
        ]
        result = adapter._process_arguments(args, resolved_env={}, runtime_vars={})
        self.assertEqual(result, ["required-arg"])


# ---------------------------------------------------------------------------
# Codex adapter
# ---------------------------------------------------------------------------


class TestCodexProcessArgumentsV01Variables(unittest.TestCase):
    """_process_arguments must handle v0.1 value_hint + variables args."""

    def _adapter(self) -> CodexClientAdapter:
        return _make_codex()

    def test_v01_plain_value_hint_args_extracted(self):
        """Plain value_hint args (no variables, no type) are extracted."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            [{"value_hint": "run"}, {"value_hint": "--rm"}],
            resolved_env={},
            runtime_vars={},
        )
        self.assertEqual(result, ["run", "--rm"])

    def test_v01_variables_placeholder_resolved_from_runtime_vars(self):
        """v0.1 arg with variables dict resolves {workspaceFolder} from runtime_vars."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            V01_DOCKER_RUNTIME_ARGS,
            resolved_env={},
            runtime_vars={"workspaceFolder": "/home/user/project"},
        )
        self.assertIn("/home/user/project:/workspace", result)

    def test_v01_variables_unknown_var_gets_placeholder(self):
        """Unknown variable gets a ${varName} placeholder (same as vscode)."""
        adapter = self._adapter()
        args = [
            {
                "value_hint": "{customVar}:/data",
                "variables": {"customVar": {"description": "Custom path", "is_required": True}},
            }
        ]
        result = adapter._process_arguments(args, resolved_env={}, runtime_vars={})
        self.assertEqual(result, ["${customVar}:/data"])

    def test_v01_full_docker_arg_set_preserved(self):
        """All 8 args from the v0.1 Docker fixture are present."""
        adapter = self._adapter()
        result = adapter._process_arguments(
            V01_DOCKER_RUNTIME_ARGS,
            resolved_env={},
            runtime_vars={"workspaceFolder": "/ws"},
        )
        self.assertEqual(len(result), 8)
        self.assertEqual(result[0], "run")
        self.assertEqual(result[4], "/ws:/workspace")

    def test_legacy_optional_hint_skipped(self):
        """Legacy entries with is_required: False must not be appended."""
        adapter = self._adapter()
        args = [
            {"value_hint": "--optional-flag", "is_required": False},
            {"value_hint": "required-arg"},
        ]
        result = adapter._process_arguments(args, resolved_env={}, runtime_vars={})
        self.assertEqual(result, ["required-arg"])
        self.assertNotIn("--optional-flag", result)

    def test_legacy_optional_hint_with_variables_skipped(self):
        """Legacy entries with is_required: False and a variables dict are skipped."""
        adapter = self._adapter()
        args = [
            {
                "value_hint": "{optionalPath}:/data",
                "is_required": False,
                "variables": {
                    "optionalPath": {"description": "Optional mount", "is_required": False}
                },
            },
            {"value_hint": "required-arg"},
        ]
        result = adapter._process_arguments(args, resolved_env={}, runtime_vars={})
        self.assertEqual(result, ["required-arg"])


if __name__ == "__main__":
    unittest.main()
