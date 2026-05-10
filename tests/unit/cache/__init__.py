"""Tests for cache URL normalization."""

from apm_cli.cache.url_normalize import cache_shard_key, normalize_repo_url


class TestNormalizeRepoUrl:
    """Test URL normalization for cache key derivation."""

    def test_strip_trailing_git(self) -> None:
        result = normalize_repo_url("https://github.com/owner/repo.git")
        assert result == "https://github.com/owner/repo"

    def test_lowercase_hostname(self) -> None:
        result = normalize_repo_url("https://GitHub.COM/owner/repo")
        assert result == "https://github.com/owner/repo"

    def test_scp_to_ssh(self) -> None:
        result = normalize_repo_url("git@github.com:owner/repo.git")
        assert result == "ssh://git@github.com/owner/repo"

    def test_strip_default_https_port(self) -> None:
        result = normalize_repo_url("https://github.com:443/owner/repo")
        assert result == "https://github.com/owner/repo"

    def test_strip_default_ssh_port(self) -> None:
        result = normalize_repo_url("ssh://git@github.com:22/owner/repo")
        assert result == "ssh://git@github.com/owner/repo"

    def test_preserve_non_default_port(self) -> None:
        result = normalize_repo_url("https://github.example.com:8443/owner/repo")
        assert result == "https://github.example.com:8443/owner/repo"

    def test_strip_password_keep_username(self) -> None:
        result = normalize_repo_url("https://user:secret@github.com/owner/repo")
        assert result == "https://user@github.com/owner/repo"

    def test_preserve_git_username(self) -> None:
        result = normalize_repo_url("ssh://git@github.com/owner/repo")
        assert result == "ssh://git@github.com/owner/repo"

    def test_strip_trailing_slash(self) -> None:
        result = normalize_repo_url("https://github.com/owner/repo/")
        assert result == "https://github.com/owner/repo"

    def test_equivalence_https_variants(self) -> None:
        """All these should produce the same normalized URL."""
        urls = [
            "https://github.com/Owner/Repo",
            "https://github.com/owner/repo.git",
            "https://GITHUB.COM/owner/repo.git",
            "https://github.com:443/owner/repo",
        ]
        normalized = {normalize_repo_url(u) for u in urls}
        assert len(normalized) == 1, f"Expected 1 unique value, got: {normalized}"

    def test_equivalence_ssh_variants(self) -> None:
        """SSH and SCP-like forms should normalize to the same URL."""
        urls = [
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo",
            "ssh://git@github.com:22/owner/repo.git",
            "git@GitHub.COM:Owner/Repo.git",
        ]
        normalized = {normalize_repo_url(u) for u in urls}
        assert len(normalized) == 1, f"Expected 1 unique value, got: {normalized}"

    def test_equivalence_cross_protocol(self) -> None:
        """HTTPS and SSH forms of the same repo should produce different keys.

        They are different protocols and may resolve differently in
        enterprise environments, so they get separate cache entries.
        """
        https_norm = normalize_repo_url("https://github.com/owner/repo")
        ssh_norm = normalize_repo_url("git@github.com:owner/repo.git")
        assert https_norm != ssh_norm

    def test_different_hosts_different_keys(self) -> None:
        """Different hosts must produce different cache keys."""
        github_key = cache_shard_key("https://github.com/owner/repo")
        gitlab_key = cache_shard_key("https://gitlab.com/owner/repo")
        assert github_key != gitlab_key


class TestCacheShardKey:
    """Test shard key derivation."""

    def test_returns_16_hex_chars(self) -> None:
        key = cache_shard_key("https://github.com/owner/repo")
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)

    def test_deterministic(self) -> None:
        key1 = cache_shard_key("https://github.com/owner/repo")
        key2 = cache_shard_key("https://github.com/owner/repo")
        assert key1 == key2

    def test_equivalent_urls_same_key(self) -> None:
        """Equivalent URL forms must produce the same shard key."""
        key1 = cache_shard_key("https://github.com/Owner/Repo")
        key2 = cache_shard_key("https://github.com/owner/repo.git")
        assert key1 == key2
