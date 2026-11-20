"""Unit tests for MCP runtime detection functionality (pure logic cases)."""

import shutil  # noqa: F401
import unittest

import pytest

from apm_cli.integration.mcp_integrator import MCPIntegrator

pytestmark = pytest.mark.unit


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
            "llm": "llm hello.md -m gpt-4",
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
            "ai": "export MODEL=gpt-4 && llm prompt.md",
        }
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(set(detected), {"codex", "copilot", "llm"})

    def test_detect_same_runtime_multiple_times(self):
        """Test that same runtime is only detected once."""
        scripts = {
            "start": "copilot -p hello.md",
            "dev": "copilot -p dev.md",
            "test": "copilot -p test.md",
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
            "dev": "copilot -p hello.md",  # Should match
        }
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(detected, ["copilot"])

    def test_detect_runtime_word_boundaries(self):
        """Test that runtime detection respects word boundaries."""
        scripts = {
            "start": "mycopilot -p hello.md",  # Should not match
            "dev": "copilot-cli -p hello.md",  # Should not match
            "test": "copilot -p hello.md",  # Should match
        }
        detected = MCPIntegrator._detect_runtimes(scripts)
        self.assertEqual(detected, ["copilot"])


class TestRuntimeDetectionIntegration(unittest.TestCase):
    """Integration tests for runtime detection workflow."""

    def test_full_detection_workflow(self):
        """Test complete workflow from scripts to available runtimes."""
        scripts = {
            "start": "copilot --log-level all -p hello.md",
            "debug": "codex --verbose hello.md",
            "build": "npm run build",  # Non-runtime command
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


if __name__ == "__main__":
    unittest.main()
