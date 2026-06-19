"""Tests for URL normalization and shard key derivation."""

from apm_cli.cache.url_normalize import cache_shard_key, normalize_repo_url

# Re-export the tests from __init__.py into a proper test file
# for pytest discovery. The __init__.py contains the test classes
# for the package marker, but pytest also finds them here.


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

    def test_equivalence_class_asserted(self) -> None:
        """Core equivalence assertion from the design spec:

        https://github.com/Owner/Repo
        == https://github.com/owner/repo.git
        == git@github.com:owner/repo.git
        (cross-protocol forms normalize differently by design)

        But: https://github.com/owner/repo != https://gitlab.com/owner/repo
        """
        # Same-protocol equivalence
        https_variants = [
            "https://github.com/Owner/Repo",
            "https://github.com/owner/repo.git",
            "https://GITHUB.COM/owner/repo",
        ]
        https_keys = {cache_shard_key(u) for u in https_variants}
        assert len(https_keys) == 1, f"HTTPS variants diverged: {https_keys}"

        ssh_variants = [
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo",
        ]
        ssh_keys = {cache_shard_key(u) for u in ssh_variants}
        assert len(ssh_keys) == 1, f"SSH variants diverged: {ssh_keys}"

        # Different hosts must differ
        github_key = cache_shard_key("https://github.com/owner/repo")
        gitlab_key = cache_shard_key("https://gitlab.com/owner/repo")
        assert github_key != gitlab_key


class TestCacheShardKey:
    """Test shard key derivation."""

    def test_length_16(self) -> None:
        key = cache_shard_key("https://github.com/owner/repo")
        assert len(key) == 16

    def test_hex_chars_only(self) -> None:
        key = cache_shard_key("https://github.com/owner/repo")
        assert all(c in "0123456789abcdef" for c in key)

    def test_deterministic(self) -> None:
        key1 = cache_shard_key("https://github.com/owner/repo")
        key2 = cache_shard_key("https://github.com/owner/repo")
        assert key1 == key2


class TestMalformedNetloc:
    """Malformed netlocs (e.g. Windows file:// URLs) must not raise.

    Regression: on Windows, `file://C:\\path\\bare.git` parses with the
    drive-letter colon interpreted as a host:port separator, so
    `parsed.port` raises ValueError. Cache-key derivation must stay
    deterministic and per-URL distinct without raising.
    """

    def test_windows_file_url_does_not_raise(self) -> None:
        url = r"file://C:\Users\runneradmin\AppData\Local\Temp\bare.git"
        # Must not raise:
        key = cache_shard_key(url)
        assert len(key) == 16

    def test_windows_file_urls_distinct_paths_distinct_keys(self) -> None:
        url_a = r"file://C:\Users\runneradmin\AppData\Local\Temp\repo_a.git"
        url_b = r"file://C:\Users\runneradmin\AppData\Local\Temp\repo_b.git"
        assert cache_shard_key(url_a) != cache_shard_key(url_b)

    def test_fallback_strips_password_from_netloc(self) -> None:
        """Malformed-netloc fallback must still drop the password (Step 4).

        Otherwise credentials end up baked into cache keys and any caller
        that logs the normalized URL leaks them.
        """
        with_secret = normalize_repo_url("https://user:secret@host:badport/repo")
        without_secret = normalize_repo_url("https://user@host:badport/repo")
        assert "secret" not in with_secret
        assert with_secret == without_secret
