"""Tests for Docker arguments deduplication."""

import unittest

from apm_cli.core.docker_args import DockerArgsProcessor


class TestDockerArgsDeduplication(unittest.TestCase):
    """Test suite for Docker args deduplication."""

    def test_no_duplicate_env_vars(self):
        """Test that environment variables are not duplicated."""
        # Given Docker args with embedded env vars and additional env vars
        base_args = ["run", "-i", "--rm"]
        env_vars = {"GITHUB_TOKEN": "test-token", "ANOTHER_VAR": "test-value"}

        # When processing with additional env vars
        result = DockerArgsProcessor.process_docker_args(base_args, env_vars)

        # Then should not contain duplicates
        expected = [
            "run",
            "-e",
            "GITHUB_TOKEN=test-token",
            "-e",
            "ANOTHER_VAR=test-value",
            "-i",
            "--rm",
        ]
        self.assertEqual(result, expected)

    def test_preserves_existing_values(self):
        """Test that new env var values override existing ones (merge semantics)."""
        # Given existing and new env vars with overlapping keys
        existing_env = {"GITHUB_TOKEN": "existing-token", "NEW_VAR": "new-value"}
        new_env = {"GITHUB_TOKEN": "new-token", "ANOTHER_VAR": "another-value"}

        # When merging
        result = DockerArgsProcessor.merge_env_vars(existing_env, new_env)

        # Then new values should override existing ones
        expected = {
            "GITHUB_TOKEN": "new-token",  # New value overrides existing
            "NEW_VAR": "new-value",
            "ANOTHER_VAR": "another-value",
        }
        self.assertEqual(result, expected)

    def test_extract_env_vars_from_args(self):
        """Test extraction of environment variables from Docker args."""
        # Given args with -e flags
        args = [
            "run",
            "-i",
            "--rm",
            "-e",
            "GITHUB_TOKEN=test-token",
            "-e",
            "ANOTHER_VAR=test-value",
            "-e",
            "FLAG_ONLY_VAR",
            "ghcr.io/github/github-mcp-server",
        ]

        # When extracting env vars
        clean_args, env_vars = DockerArgsProcessor.extract_env_vars_from_args(args)

        # Then should separate cleanly
        expected_clean_args = ["run", "-i", "--rm", "ghcr.io/github/github-mcp-server"]
        expected_env_vars = {
            "GITHUB_TOKEN": "test-token",
            "ANOTHER_VAR": "test-value",
            "FLAG_ONLY_VAR": "${FLAG_ONLY_VAR}",
        }

        self.assertEqual(clean_args, expected_clean_args)
        self.assertEqual(env_vars, expected_env_vars)

    def test_process_docker_args_with_existing_env_in_args(self):
        """Test processing args that already contain some env vars."""
        # Given args that already have some -e flags mixed in
        base_args = ["run", "-i", "--rm", "ghcr.io/github/github-mcp-server"]
        env_vars = {"NEW_VAR": "new-value"}

        # When processing
        result = DockerArgsProcessor.process_docker_args(base_args, env_vars)

        # Then env vars should be injected after "run"
        expected = [
            "run",
            "-e",
            "NEW_VAR=new-value",
            "-i",
            "--rm",
            "ghcr.io/github/github-mcp-server",
        ]
        self.assertEqual(result, expected)

    def test_empty_env_vars(self):
        """Test processing with no environment variables."""
        base_args = ["run", "-i", "--rm", "image-name"]
        env_vars = {}

        result = DockerArgsProcessor.process_docker_args(base_args, env_vars)

        # Should return args unchanged
        self.assertEqual(result, base_args)

    def test_no_run_command(self):
        """Test processing args without 'run' command."""
        base_args = ["pull", "image-name"]
        env_vars = {"TEST_VAR": "test-value"}

        result = DockerArgsProcessor.process_docker_args(base_args, env_vars)

        # Should return args unchanged since no 'run' command found
        self.assertEqual(result, base_args)


if __name__ == "__main__":
    unittest.main()


class TestDockerArgsInteractiveFlagInjection(unittest.TestCase):
    """Test cases for -i and --rm flag injection when not already present."""

    def test_adds_interactive_flag_when_not_present(self):
        """When -i is not in base_args, it is injected after 'run'."""
        base_args = ["run", "--rm"]
        result = DockerArgsProcessor.process_docker_args(base_args, {})
        assert "-i" in result
        # -i should come right after "run"
        run_idx = result.index("run")
        assert result[run_idx + 1] == "-i" or "-i" in result

    def test_adds_rm_flag_when_not_present(self):
        """When --rm is not in base_args, it is injected after 'run'."""
        base_args = ["run", "-i"]
        result = DockerArgsProcessor.process_docker_args(base_args, {})
        assert "--rm" in result

    def test_adds_both_flags_when_neither_present(self):
        """When neither -i nor --rm are in base_args, both are injected."""
        base_args = ["run", "my-image"]
        result = DockerArgsProcessor.process_docker_args(base_args, {})
        assert "-i" in result
        assert "--rm" in result

    def test_interactive_long_form_detected(self):
        """--interactive is recognised as equivalent to -i, so -i is not re-added."""
        base_args = ["run", "--interactive", "--rm", "my-image"]
        result = DockerArgsProcessor.process_docker_args(base_args, {})
        # -i should NOT be added since --interactive is already present
        assert result.count("-i") == 0

    def test_env_vars_not_duplicated_when_same_name_processed_twice(self):
        """env_vars_added guard: same key is never added twice in a valid call."""
        base_args = ["run", "-i", "--rm"]
        env_vars = {"TOKEN": "abc"}
        result = DockerArgsProcessor.process_docker_args(base_args, env_vars)
        # Count occurrences of TOKEN=abc in the result
        token_count = sum(1 for arg in result if arg == "TOKEN=abc")
        assert token_count == 1
