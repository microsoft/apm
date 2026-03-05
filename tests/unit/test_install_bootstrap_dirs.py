"""Tests for install-time integration directory bootstrap decisions."""

from apm_cli.cli import _should_auto_create_github_dir


class TestShouldAutoCreateGithubDir:
    """Unit tests for _should_auto_create_github_dir helper."""

    def test_true_when_no_integration_dirs_exist(self, tmp_path):
        """Should bootstrap .github when no integration roots exist."""
        assert _should_auto_create_github_dir(tmp_path) is True

    def test_false_when_github_exists(self, tmp_path):
        """Should not bootstrap when .github already exists."""
        (tmp_path / ".github").mkdir()
        assert _should_auto_create_github_dir(tmp_path) is False

    def test_false_when_claude_exists(self, tmp_path):
        """Should not bootstrap when .claude already exists."""
        (tmp_path / ".claude").mkdir()
        assert _should_auto_create_github_dir(tmp_path) is False

    def test_false_when_opencode_exists(self, tmp_path):
        """Should not bootstrap when .opencode already exists."""
        (tmp_path / ".opencode").mkdir()
        assert _should_auto_create_github_dir(tmp_path) is False
