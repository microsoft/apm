"""Tests for Docker arguments processing and safe installation."""

import unittest
from unittest.mock import MagicMock, patch

from apm_cli.core.docker_args import DockerArgsProcessor
from apm_cli.core.safe_installer import InstallationSummary, SafeMCPInstaller


class TestDockerArgsProcessor(unittest.TestCase):
    """Test suite for Docker args processing."""

    def test_process_docker_args_basic(self):
        """Test basic Docker args processing with environment variables."""
        base_args = ["run", "-i", "--rm", "image:latest"]
        env_vars = {"GITHUB_TOKEN": "token123", "API_KEY": "key456"}

        result = DockerArgsProcessor.process_docker_args(base_args, env_vars)

        expected = [
            "run",
            "-e",
            "GITHUB_TOKEN=token123",
            "-e",
            "API_KEY=key456",
            "-i",
            "--rm",
            "image:latest",
        ]
        self.assertEqual(result, expected)

    def test_process_docker_args_no_run_command(self):
        """Test Docker args processing when no 'run' command is present."""
        base_args = ["build", "-t", "myimage", "."]
        env_vars = {"BUILD_ARG": "value"}

        result = DockerArgsProcessor.process_docker_args(base_args, env_vars)

        # Should not inject env vars if no 'run' command
        expected = ["build", "-t", "myimage", "."]
        self.assertEqual(result, expected)

    def test_extract_env_vars_from_args(self):
        """Test extraction of environment variables from Docker args."""
        args = ["run", "-i", "-e", "TOKEN=value1", "--rm", "-e", "API_KEY=value2", "image"]

        clean_args, env_vars = DockerArgsProcessor.extract_env_vars_from_args(args)

        expected_clean = ["run", "-i", "--rm", "image"]
        expected_env = {"TOKEN": "value1", "API_KEY": "value2"}

        self.assertEqual(clean_args, expected_clean)
        self.assertEqual(env_vars, expected_env)

    def test_extract_env_vars_with_just_names(self):
        """Test extraction when only env var names are provided (no values)."""
        args = ["run", "-e", "TOKEN", "-e", "API_KEY", "image"]

        clean_args, env_vars = DockerArgsProcessor.extract_env_vars_from_args(args)

        expected_clean = ["run", "image"]
        expected_env = {"TOKEN": "${TOKEN}", "API_KEY": "${API_KEY}"}

        self.assertEqual(clean_args, expected_clean)
        self.assertEqual(env_vars, expected_env)

    def test_merge_env_vars_preserves_existing(self):
        """Test that new environment variable values override existing ones."""
        existing_env = {"GITHUB_TOKEN": "existing_token", "OLD_VAR": "old_value"}
        new_env = {"GITHUB_TOKEN": "new_token", "NEW_VAR": "new_value"}

        result = DockerArgsProcessor.merge_env_vars(existing_env, new_env)

        expected = {
            "GITHUB_TOKEN": "new_token",  # New value overrides existing
            "OLD_VAR": "old_value",
            "NEW_VAR": "new_value",
        }
        self.assertEqual(result, expected)

    def test_merge_env_vars_empty_existing(self):
        """Test merging when existing env vars is empty."""
        existing_env = {}
        new_env = {"TOKEN": "value", "KEY": "secret"}

        result = DockerArgsProcessor.merge_env_vars(existing_env, new_env)

        self.assertEqual(result, new_env)


class TestInstallationSummary(unittest.TestCase):
    """Test suite for InstallationSummary."""

    def test_empty_summary(self):
        """Test empty installation summary."""
        summary = InstallationSummary()

        self.assertEqual(len(summary.installed), 0)
        self.assertEqual(len(summary.skipped), 0)
        self.assertEqual(len(summary.failed), 0)
        self.assertFalse(summary.has_any_changes())

    def test_add_operations(self):
        """Test adding different types of operations."""
        summary = InstallationSummary()

        summary.add_installed("server1")
        summary.add_skipped("server2", "already exists")
        summary.add_failed("server3", "network error")

        self.assertEqual(summary.installed, ["server1"])
        self.assertEqual(summary.skipped, [{"server": "server2", "reason": "already exists"}])
        self.assertEqual(summary.failed, [{"server": "server3", "reason": "network error"}])
        self.assertTrue(summary.has_any_changes())

    def test_has_any_changes(self):
        """Test has_any_changes logic."""
        summary = InstallationSummary()

        # Should be False when only skipped
        summary.add_skipped("server1", "exists")
        self.assertFalse(summary.has_any_changes())

        # Should be True when installed
        summary.add_installed("server2")
        self.assertTrue(summary.has_any_changes())

        # Reset and test with failures
        summary = InstallationSummary()
        summary.add_failed("server3", "error")
        self.assertTrue(summary.has_any_changes())


class TestSafeMCPInstaller(unittest.TestCase):
    """Test suite for SafeMCPInstaller."""

    @patch("apm_cli.core.safe_installer.ClientFactory")
    @patch("apm_cli.core.safe_installer.MCPConflictDetector")
    def test_install_servers_with_conflicts(self, mock_detector_class, mock_factory):
        """Test installing servers when conflicts exist."""
        # Setup mocks
        mock_adapter = MagicMock()
        mock_factory.create_client.return_value = mock_adapter

        mock_detector = MagicMock()
        mock_detector_class.return_value = mock_detector

        # Mock conflict detection: first server exists, second doesn't
        mock_detector.check_server_exists.side_effect = lambda x: x == "existing-server"

        # Mock successful installation for new server
        mock_adapter.configure_mcp_server.return_value = True

        # Test installation
        installer = SafeMCPInstaller("copilot")
        summary = installer.install_servers(["existing-server", "new-server"])

        # Verify results
        self.assertEqual(len(summary.skipped), 1)
        self.assertEqual(len(summary.installed), 1)
        self.assertEqual(len(summary.failed), 0)

        self.assertEqual(summary.skipped[0]["server"], "existing-server")
        self.assertEqual(summary.skipped[0]["reason"], "already configured")
        self.assertEqual(summary.installed[0], "new-server")

        # Verify adapter was called only for new server
        mock_adapter.configure_mcp_server.assert_called_once_with("new-server")

    @patch("apm_cli.core.safe_installer.ClientFactory")
    @patch("apm_cli.core.safe_installer.MCPConflictDetector")
    def test_install_servers_with_failures(self, mock_detector_class, mock_factory):
        """Test installing servers when installation fails."""
        # Setup mocks
        mock_adapter = MagicMock()
        mock_factory.create_client.return_value = mock_adapter

        mock_detector = MagicMock()
        mock_detector_class.return_value = mock_detector

        # No conflicts detected
        mock_detector.check_server_exists.return_value = False

        # Mock installation failure
        mock_adapter.configure_mcp_server.side_effect = Exception("Network error")

        # Test installation
        installer = SafeMCPInstaller("copilot")
        summary = installer.install_servers(["failing-server"])

        # Verify results
        self.assertEqual(len(summary.skipped), 0)
        self.assertEqual(len(summary.installed), 0)
        self.assertEqual(len(summary.failed), 1)

        self.assertEqual(summary.failed[0]["server"], "failing-server")
        self.assertEqual(summary.failed[0]["reason"], "Network error")

    @patch("apm_cli.core.safe_installer.ClientFactory")
    @patch("apm_cli.core.safe_installer.MCPConflictDetector")
    def test_install_servers_successful(self, mock_detector_class, mock_factory):
        """Test successful server installation."""
        # Setup mocks
        mock_adapter = MagicMock()
        mock_factory.create_client.return_value = mock_adapter

        mock_detector = MagicMock()
        mock_detector_class.return_value = mock_detector

        # No conflicts detected
        mock_detector.check_server_exists.return_value = False

        # Mock successful installation
        mock_adapter.configure_mcp_server.return_value = True

        # Test installation
        installer = SafeMCPInstaller("copilot")
        summary = installer.install_servers(["github", "notion"])

        # Verify results
        self.assertEqual(len(summary.skipped), 0)
        self.assertEqual(len(summary.installed), 2)
        self.assertEqual(len(summary.failed), 0)

        self.assertIn("github", summary.installed)
        self.assertIn("notion", summary.installed)

        # Verify adapter was called for both servers
        self.assertEqual(mock_adapter.configure_mcp_server.call_count, 2)

    @patch("apm_cli.core.safe_installer.ClientFactory")
    @patch("apm_cli.core.safe_installer.MCPConflictDetector")
    def test_check_conflicts_only(self, mock_detector_class, mock_factory):
        """Test conflict checking without installation."""
        # Setup mocks
        mock_adapter = MagicMock()
        mock_factory.create_client.return_value = mock_adapter

        mock_detector = MagicMock()
        mock_detector_class.return_value = mock_detector

        # Mock conflict summary
        mock_detector.get_conflict_summary.return_value = {
            "exists": True,
            "canonical_name": "io.github.github/github-mcp-server",
            "conflicting_servers": [{"name": "existing-github", "type": "canonical_match"}],
        }

        # Test conflict checking
        installer = SafeMCPInstaller("copilot")
        conflicts = installer.check_conflicts_only(["github"])

        # Verify results
        self.assertIn("github", conflicts)
        self.assertTrue(conflicts["github"]["exists"])
        self.assertEqual(
            conflicts["github"]["canonical_name"], "io.github.github/github-mcp-server"
        )

        # Verify no installation was attempted
        mock_adapter.configure_mcp_server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
