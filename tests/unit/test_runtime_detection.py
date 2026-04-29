"""Unit tests for MCP runtime detection functionality."""

import shutil
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from apm_cli.integration.mcp_integrator import MCPIntegrator


class TestRuntimeDetection(unittest.TestCase):
    """Test cases for runtime detection from apm.yml scripts."""
    
    def test_detect_single_runtime(self):
        """Test detecting single runtime from scripts."""
        scripts = {"start": "copilot --log-level all -p hello.md"}
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(detected, ["copilot"])
    
    def test_detect_multiple_runtimes(self):
        """Test detecting multiple runtimes from scripts."""
        scripts = {
            "start": "copilot --log-level all -p hello.md",
            "debug": "codex --verbose hello.md", 
            "llm": "llm hello.md -m gpt-4"
        }
        detected = MCPIntegrator._detect_runtimes(scripts)
        # Order may vary due to set() usage, so check contents
        self.assertEqual(set(detected), {"copilot", "codex", "llm"})
        self.assertEqual(len(detected), 3)
    
    def test_detect_no_runtimes(self):
        """Test detecting no recognized runtimes."""
        scripts = {"start": "python hello.py", "test": "pytest"}
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(detected, [])
    
    def test_detect_runtime_in_complex_command(self):
        """Test detecting runtime in complex command lines."""
        scripts = {
            "start": "RUST_LOG=debug codex --skip-git-repo-check hello.md",
            "dev": "npm run build && copilot -p prompt.md",
            "ai": "export MODEL=gpt-4 && llm prompt.md"
        }
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(set(detected), {"codex", "copilot", "llm"})
    
    def test_detect_same_runtime_multiple_times(self):
        """Test that same runtime is only detected once."""
        scripts = {
            "start": "copilot -p hello.md",
            "dev": "copilot -p dev.md",
            "test": "copilot -p test.md"
        }
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(detected, ["copilot"])
    
    def test_detect_empty_scripts(self):
        """Test handling empty scripts dictionary."""
        scripts = {}
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(detected, [])
    
    def test_detect_runtime_case_sensitivity(self):
        """Test that runtime detection is case sensitive."""
        scripts = {
            "start": "COPILOT -p hello.md",  # Should not match
            "dev": "copilot -p hello.md"     # Should match
        }
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(detected, ["copilot"])
    
    def test_detect_runtime_word_boundaries(self):
        """Test that runtime detection respects word boundaries."""
        scripts = {
            "start": "mycopilot -p hello.md",     # Should not match
            "dev": "copilot-cli -p hello.md",    # Should not match  
            "test": "copilot -p hello.md"        # Should match
        }
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(detected, ["copilot"])


class TestRuntimeFiltering(unittest.TestCase):
    """Test cases for filtering available runtimes."""
    
    @patch('apm_cli.runtime.manager.RuntimeManager')
    @patch('apm_cli.factory.ClientFactory')
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
    
    @patch('apm_cli.runtime.manager.RuntimeManager')
    @patch('apm_cli.factory.ClientFactory')
    def test_filter_available_runtimes_partial_available(self, mock_factory_class, mock_manager_class):
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
    
    @patch('apm_cli.runtime.manager.RuntimeManager')
    @patch('apm_cli.factory.ClientFactory')
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
    
    @patch('apm_cli.factory.ClientFactory')
    def test_filter_unsupported_runtime_types(self, mock_factory_class):
        """Test filtering out unsupported runtime types."""
        # Mock ClientFactory to reject unsupported runtime
        def mock_create_client(runtime):
            if runtime == "unsupported":
                raise ValueError("Unsupported client type")
            return MagicMock()
        
        mock_factory_class.create_client.side_effect = mock_create_client
        
        # Mock missing RuntimeManager to trigger fallback
        with patch('apm_cli.runtime.manager.RuntimeManager', side_effect=ImportError):
            with patch('shutil.which') as mock_which:
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


class TestRuntimeDetectionIntegration(unittest.TestCase):
    """Integration tests for runtime detection workflow."""
    
    def test_full_detection_workflow(self):
        """Test complete workflow from scripts to available runtimes."""
        scripts = {
            "start": "copilot --log-level all -p hello.md",
            "debug": "codex --verbose hello.md", 
            "build": "npm run build"  # Non-runtime command
        }
        
        # Detect runtimes from scripts
        detected = MCPIntegrator._detect_runtimes(scripts)
        expected_detected = {"copilot", "codex"}
        self.assertEqual(set(detected), expected_detected)
        
        # Filter available runtimes (this will use real system state)
        available = MCPIntegrator._filter_runtimes(detected)
        
        # Available should be subset of detected
        self.assertTrue(set(available).issubset(set(detected)))
        
        # Each available runtime should be creatable via factory
        from apm_cli.factory import ClientFactory
        for runtime in available:
            with self.subTest(runtime=runtime):
                try:
                    client = ClientFactory.create_client(runtime)
                    self.assertIsNotNone(client)
                except ValueError:
                    self.fail(f"Runtime {runtime} reported as available but not creatable")


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

        with patch(f"{self.MODULE}.shutil.which", return_value=None), \
             patch(f"{self.MODULE}.Path.cwd", return_value=Path("/tmp/other-cwd")), \
             patch.object(Path, "is_dir", autospec=True) as mock_is_dir:
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
