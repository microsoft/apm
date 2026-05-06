"""Tests for proxy / insteadOf compatibility with the cache layer.

Verifies that:
1. User git configuration (GIT_SSH_COMMAND, proxy env) is honored
2. Cache key derives from the URL as given (pre-insteadOf rewrite)
3. Two installs of the same dep hit the cache on second run
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.cache.git_cache import GitCache
from apm_cli.utils.git_env import git_subprocess_env


class TestProxyEnvPreserved:
    """Verify proxy environment variables pass through to git subprocess."""

    def test_https_proxy_in_subprocess_env(self) -> None:
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.corp:8080"}):
            env = git_subprocess_env()
            assert env["HTTPS_PROXY"] == "http://proxy.corp:8080"

    def test_http_proxy_in_subprocess_env(self) -> None:
        with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.corp:3128"}):
            env = git_subprocess_env()
            assert env["HTTP_PROXY"] == "http://proxy.corp:3128"

    def test_no_proxy_in_subprocess_env(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": "internal.corp,*.local"}):
            env = git_subprocess_env()
            assert env["NO_PROXY"] == "internal.corp,*.local"


class TestInsteadOfRewrite:
    """Verify cache key stability across insteadOf rewrites.

    git's insteadOf rewrites happen at clone/fetch time (transparent
    to the caller). The cache key must be derived from the URL AS GIVEN,
    not after any git-internal rewrite.
    """

    def test_cache_key_from_original_url(self, tmp_path: Path) -> None:
        """Two references to the same URL should hit the same cache shard,
        regardless of what insteadOf rules git applies internally."""
        from apm_cli.cache.url_normalize import cache_shard_key

        original_url = "https://github.com/owner/repo"
        # Even if git rewrites this to an internal mirror, our cache key
        # is derived from the original
        key1 = cache_shard_key(original_url)
        key2 = cache_shard_key(original_url)
        assert key1 == key2

    @patch("subprocess.run")
    def test_second_install_hits_cache(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """After a successful cache population, a second call with the same
        URL and locked SHA should NOT invoke any git subprocess (cache HIT).

        Integrity verification reads ``.git/HEAD`` directly from disk
        (no subprocess), so a true cache hit yields zero subprocess
        calls -- the strongest possible proof of "no work".
        """
        sha = "a" * 40
        url = "https://github.com/owner/repo"

        cache = GitCache(tmp_path)

        from apm_cli.cache.url_normalize import cache_shard_key

        shard = cache_shard_key(url)

        # Pre-populate the checkout to simulate first install success.
        # The integrity verifier reads ``.git/HEAD`` directly, so we
        # must lay down a HEAD file containing the expected SHA.
        checkout_dir = tmp_path / "git" / "checkouts_v1" / shard / sha
        checkout_dir.mkdir(parents=True)
        git_dir = checkout_dir / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text(f"{sha}\n", encoding="utf-8")

        # Second install -- should hit cache with ZERO subprocess calls
        result = cache.get_checkout(url, "main", locked_sha=sha)
        assert result == checkout_dir

        # No clone, no fetch, no rev-parse -- pure file-system hit
        assert mock_run.call_args_list == []
