"""Unit tests for MCP runtime detection functionality."""

import shutil  # noqa: F401
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.integration.mcp_integrator import MCPIntegrator

pytestmark = pytest.mark.component


class TestRuntimeFiltering(unittest.TestCase):
    """Test cases for filtering available runtimes."""

    @patch("apm_cli.runtime.manager.RuntimeManager")
    @patch("apm_cli.factory.ClientFactory")
    def test_filter_available_runtimes_all_available(self, mock_factory_class, mock_manager_class):
        """Test filtering when all detected runtimes are available."""
        # Mock ClientFactory to accept all runtimes
        mock_factory_class.create_client.return_value = MagicMock()

        # Mock RuntimeManager to report all as available
        mock_manager = MagicMock()
        mock_manager.is_runtime_available.return_value = True
        mock_manager_class.return_value = mock_manager

        detected = ["copilot", "codex", "llm"]
        available = MCPIntegrator._filter_runtimes(detected)

        self.assertEqual(set(available), set(detected))

    @patch("apm_cli.runtime.manager.RuntimeManager")
    @patch("apm_cli.factory.ClientFactory")
    def test_filter_available_runtimes_partial_available(
        self, mock_factory_class, mock_manager_class
    ):
        """Test filtering when only some runtimes are available."""
        # Mock ClientFactory to accept all runtimes
        mock_factory_class.create_client.return_value = MagicMock()

        # Mock RuntimeManager to report only copilot as available
        mock_manager = MagicMock()
        mock_manager.is_runtime_available.side_effect = lambda rt: rt == "copilot"
        mock_manager_class.return_value = mock_manager

        detected = ["copilot", "codex", "llm"]
        available = MCPIntegrator._filter_runtimes(detected)

        self.assertEqual(available, ["copilot"])

    @patch("apm_cli.runtime.manager.RuntimeManager")
    @patch("apm_cli.factory.ClientFactory")
    def test_filter_available_runtimes_none_available(self, mock_factory_class, mock_manager_class):
        """Test filtering when no runtimes are available."""
        # Mock ClientFactory to accept all runtimes
        mock_factory_class.create_client.return_value = MagicMock()

        # Mock RuntimeManager to report none as available
        mock_manager = MagicMock()
        mock_manager.is_runtime_available.return_value = False
        mock_manager_class.return_value = mock_manager

        detected = ["copilot", "codex", "llm"]
        available = MCPIntegrator._filter_runtimes(detected)

        self.assertEqual(available, [])

    @patch("apm_cli.factory.ClientFactory")
    def test_filter_unsupported_runtime_types(self, mock_factory_class):
        """Test filtering out unsupported runtime types."""

        # Mock ClientFactory to reject unsupported runtime
        def mock_create_client(runtime):
            if runtime == "unsupported":
                raise ValueError("Unsupported client type")
            return MagicMock()

        mock_factory_class.create_client.side_effect = mock_create_client

        # Mock missing RuntimeManager to trigger fallback
        with patch("apm_cli.runtime.manager.RuntimeManager", side_effect=ImportError):
            with patch("shutil.which") as mock_which:
                mock_which.side_effect = lambda cmd: cmd in ["copilot", "codex"]

                detected = ["copilot", "codex", "unsupported"]
                available = MCPIntegrator._filter_runtimes(detected)

                # Should filter out unsupported runtime
                self.assertEqual(set(available), {"copilot", "codex"})

    def test_filter_empty_list(self):
        """Test filtering empty list of detected runtimes."""
        detected = []
        available = MCPIntegrator._filter_runtimes(detected)
        self.assertEqual(available, [])


class TestVSCodeRuntimeDetection(unittest.TestCase):
    """Tests for the _is_vscode_available() production helper."""

    MODULE = "apm_cli.integration.mcp_integrator"

    def _run(self, code_on_path: bool, vscode_dir_exists: bool) -> bool:
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        which_result = "/usr/local/bin/code" if code_on_path else None
        with patch(f"{self.MODULE}.shutil.which", return_value=which_result):
            with patch(f"{self.MODULE}.Path.cwd") as mock_cwd:
                mock_vscode = MagicMock()
                mock_vscode.__truediv__ = lambda self, other: mock_vscode
                mock_vscode.is_dir.return_value = vscode_dir_exists
                mock_cwd.return_value = mock_vscode
                return _is_vscode_available()

    def test_vscode_detected_via_explicit_project_root(self):
        """Explicit project_root should be used instead of CWD for .vscode detection."""
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        root = Path("/tmp/project-root")

        with (
            patch(f"{self.MODULE}.shutil.which", return_value=None),
            patch(f"{self.MODULE}.Path.cwd", return_value=Path("/tmp/other-cwd")),
            patch.object(Path, "is_dir", autospec=True) as mock_is_dir,
        ):
            mock_is_dir.side_effect = lambda path: path == (root / ".vscode")
            self.assertTrue(_is_vscode_available(project_root=root))

    def test_vscode_detected_via_code_binary(self):
        """`code` binary on PATH is sufficient to detect VS Code."""
        self.assertTrue(self._run(code_on_path=True, vscode_dir_exists=False))

    def test_vscode_detected_via_vscode_directory(self):
        """.vscode/ directory presence detects VS Code even without `code` on PATH."""
        self.assertTrue(self._run(code_on_path=False, vscode_dir_exists=True))

    def test_vscode_not_detected_without_binary_or_dir(self):
        """VS Code is NOT detected when neither `code` is on PATH nor .vscode/ exists."""
        self.assertFalse(self._run(code_on_path=False, vscode_dir_exists=False))

    def test_vscode_detected_when_both_binary_and_dir_present(self):
        """_is_vscode_available() returns True (not duplicated) when both are present."""
        self.assertTrue(self._run(code_on_path=True, vscode_dir_exists=True))


if __name__ == "__main__":
    unittest.main()
