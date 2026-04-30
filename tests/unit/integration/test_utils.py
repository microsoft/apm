"""Tests for integration utility functions."""

from apm_cli.integration.utils import normalize_repo_url


class TestNormalizeRepoUrl:
    """Tests for normalize_repo_url utility function."""

    def test_normalize_short_form_unchanged(self):
        """Short form URLs should remain unchanged."""
        assert normalize_repo_url("owner/repo") == "owner/repo"

    def test_normalize_short_form_with_git_suffix(self):
        """Short form with .git suffix should have it removed."""
        assert normalize_repo_url("owner/repo.git") == "owner/repo"

    def test_normalize_github_https_url(self):
        """Full GitHub HTTPS URL should be normalized to owner/repo."""
        assert normalize_repo_url("https://github.com/owner/repo") == "owner/repo"

    def test_normalize_github_https_url_with_git_suffix(self):
        """Full GitHub HTTPS URL with .git should be normalized."""
        assert normalize_repo_url("https://github.com/owner/repo.git") == "owner/repo"

    def test_normalize_gitlab_url(self):
        """GitLab URLs should be normalized to owner/repo."""
        assert normalize_repo_url("https://gitlab.com/owner/repo") == "owner/repo"

    def test_normalize_enterprise_github_url(self):
        """Enterprise GitHub URLs should be normalized."""
        assert normalize_repo_url("https://github.enterprise.com/owner/repo") == "owner/repo"

    def test_normalize_enterprise_github_url_with_git(self):
        """Enterprise GitHub URLs with .git should be normalized."""
        assert normalize_repo_url("https://github.enterprise.com/owner/repo.git") == "owner/repo"

    def test_normalize_http_url(self):
        """HTTP URLs (not HTTPS) should also be normalized."""
        assert normalize_repo_url("http://github.com/owner/repo") == "owner/repo"

    def test_normalize_nested_org_path(self):
        """URLs with nested paths should extract the full path after host."""
        assert normalize_repo_url("https://gitlab.com/group/subgroup/repo") == "group/subgroup/repo"

    def test_normalize_complex_enterprise_url(self):
        """Complex enterprise URLs should be handled correctly."""
        url = "https://git.enterprise.internal/organization/team/project"
        assert normalize_repo_url(url) == "organization/team/project"

    def test_normalize_url_without_path(self):
        """URLs without a path component should be returned as-is."""
        assert normalize_repo_url("https://github.com") == "https://github.com"

    def test_normalize_empty_string(self):
        """Empty string should be returned unchanged."""
        assert normalize_repo_url("") == ""

    def test_normalize_multiple_git_suffixes(self):
        """Only the trailing .git should be removed."""
        # This is an edge case - repo name contains 'git'
        assert normalize_repo_url("owner/mygit-repo.git") == "owner/mygit-repo"

    def test_normalize_preserves_case(self):
        """Case should be preserved in the normalized URL."""
        assert normalize_repo_url("https://github.com/Owner/Repo") == "Owner/Repo"

    def test_normalize_handles_trailing_slash(self):
        """Trailing slashes should be removed for consistent matching."""
        assert normalize_repo_url("https://github.com/owner/repo/") == "owner/repo"

    def test_normalize_handles_trailing_slash_short_form(self):
        """Trailing slashes should be removed from short form URLs too."""
        assert normalize_repo_url("owner/repo/") == "owner/repo"

    def test_normalize_handles_trailing_slash_with_git(self):
        """Trailing slashes and .git suffix should both be removed."""
        assert normalize_repo_url("https://github.com/owner/repo.git/") == "owner/repo"

    def test_normalize_ssh_url_unchanged(self):
        """SSH URLs without :// shouldn't be modified (edge case)."""
        # SSH URLs like git@github.com:owner/repo.git don't have ://
        # so they're treated as short form
        assert normalize_repo_url("git@github.com:owner/repo.git") == "git@github.com:owner/repo"
