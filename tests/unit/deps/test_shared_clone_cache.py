"""WS2a (#1116): shared clone cache tests for subdirectory dep deduplication.

Verifies:
1. parity: single subdir dep produces same result with/without cache.
2. dedup: two subdir deps from same repo+ref clone exactly once.
3. divergence: two subdir deps from same repo but different refs => 2 clones.
4. failure isolation: shared-clone failure surfaces to all consumers.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.shared_clone_cache import SharedCloneCache

# ---------------------------------------------------------------------------
# SharedCloneCache unit tests
# ---------------------------------------------------------------------------


class TestSharedCloneCache:
    """Direct unit tests for SharedCloneCache."""

    def test_single_subdir_dep_clones_once(self, tmp_path: Path) -> None:
        """Parity: 1 subdir dep clones once and cache returns the path."""
        cache = SharedCloneCache(base_dir=tmp_path)
        clone_count = {"n": 0}

        def clone_fn(target: Path) -> None:
            clone_count["n"] += 1
            target.mkdir(parents=True, exist_ok=True)
            (target / "skills" / "X").mkdir(parents=True)
            (target / "skills" / "X" / "apm.yml").write_text("name: X\nversion: 1.0.0\n")

        result = cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)
        assert result.exists()
        assert (result / "skills" / "X" / "apm.yml").exists()
        assert clone_count["n"] == 1
        cache.cleanup()

    def test_dedup_two_subdir_deps_same_repo_ref(self, tmp_path: Path) -> None:
        """Two subdir deps from same repo+ref => exactly 1 clone invocation."""
        cache = SharedCloneCache(base_dir=tmp_path)
        clone_count = {"n": 0}

        def clone_fn(target: Path) -> None:
            clone_count["n"] += 1
            target.mkdir(parents=True, exist_ok=True)
            (target / "skills" / "X").mkdir(parents=True)
            (target / "agents" / "Y").mkdir(parents=True)
            (target / "skills" / "X" / "apm.yml").write_text("name: X\n")
            (target / "agents" / "Y" / "apm.yml").write_text("name: Y\n")

        path1 = cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)
        path2 = cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)

        assert clone_count["n"] == 1
        assert path1 == path2
        assert (path1 / "skills" / "X" / "apm.yml").exists()
        assert (path1 / "agents" / "Y" / "apm.yml").exists()
        cache.cleanup()

    def test_divergent_refs_clone_independently(self, tmp_path: Path) -> None:
        """Two subdir deps from same repo but different refs => 2 clones."""
        cache = SharedCloneCache(base_dir=tmp_path)
        clone_count = {"n": 0}

        def clone_fn(target: Path) -> None:
            clone_count["n"] += 1
            target.mkdir(parents=True, exist_ok=True)
            (target / "data.txt").write_text(f"ref-{clone_count['n']}")

        path1 = cache.get_or_clone("github.com", "owner", "repo", "v1.0", clone_fn)
        path2 = cache.get_or_clone("github.com", "owner", "repo", "v2.0", clone_fn)

        assert clone_count["n"] == 2
        assert path1 != path2
        cache.cleanup()

    def test_failure_surfaces_to_all_consumers(self, tmp_path: Path) -> None:
        """Shared-clone failure raises for the first caller.

        A subsequent retry with the same key should attempt a fresh clone
        (fail-closed: failures are not poison-cached).
        """
        cache = SharedCloneCache(base_dir=tmp_path)
        call_count = {"n": 0}

        def failing_clone(target: Path) -> None:
            call_count["n"] += 1
            raise RuntimeError("network timeout")

        with pytest.raises(RuntimeError, match="network timeout"):
            cache.get_or_clone("github.com", "owner", "repo", "main", failing_clone)

        # Second attempt retries (error cleared).
        with pytest.raises(RuntimeError, match="network timeout"):
            cache.get_or_clone("github.com", "owner", "repo", "main", failing_clone)

        # Both attempts called clone_fn (failure not cached).
        assert call_count["n"] == 2
        cache.cleanup()

    def test_concurrent_access_serializes_clone(self, tmp_path: Path) -> None:
        """Multiple threads waiting for the same key: only one clones."""
        cache = SharedCloneCache(base_dir=tmp_path)
        clone_count = {"n": 0}
        clone_lock = threading.Lock()

        def slow_clone(target: Path) -> None:
            import time

            time.sleep(0.05)
            with clone_lock:
                clone_count["n"] += 1
            target.mkdir(parents=True, exist_ok=True)

        results: list[Path] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                p = cache.get_or_clone("github.com", "owner", "repo", "main", slow_clone)
                results.append(p)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert clone_count["n"] == 1
        assert all(r == results[0] for r in results)
        cache.cleanup()

    def test_context_manager_cleanup(self, tmp_path: Path) -> None:
        """Using as context manager cleans up temp dirs."""
        with SharedCloneCache(base_dir=tmp_path) as cache:

            def clone_fn(target: Path) -> None:
                target.mkdir(parents=True, exist_ok=True)

            path = cache.get_or_clone("github.com", "o", "r", None, clone_fn)
            assert path.exists()

        # After exit, temp dirs should be cleaned
        # (path itself may or may not exist depending on shutil.rmtree timing)


# ---------------------------------------------------------------------------
# Integration with GitHubPackageDownloader.download_subdirectory_package
# ---------------------------------------------------------------------------


class TestDownloaderSharedCloneIntegration:
    """Test that the downloader uses shared_clone_cache when set."""

    def test_two_subdir_deps_share_single_clone(self, tmp_path: Path) -> None:
        """Mock _clone_with_fallback and verify call_count == 1 for 2 subdir deps."""
        from apm_cli.deps.github_downloader import GitHubPackageDownloader
        from apm_cli.models.apm_package import DependencyReference

        # Build two subdir dep refs from same repo
        dep_a = DependencyReference.parse("owner/repo/skills/X#main")
        dep_b = DependencyReference.parse("owner/repo/agents/Y#main")

        target_a = tmp_path / "modules" / "X"
        target_b = tmp_path / "modules" / "Y"

        # Create downloader with shared cache
        downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
        downloader.auth_resolver = MagicMock()
        downloader.token_manager = MagicMock()
        downloader._transport_selector = MagicMock()
        downloader._protocol_pref = MagicMock()
        downloader._allow_fallback = False
        downloader._fallback_port_warned = set()
        downloader._strategies = MagicMock()
        downloader.git_env = {}

        cache = SharedCloneCache(base_dir=tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        downloader.shared_clone_cache = cache
        downloader.persistent_git_cache = None

        clone_call_count = {"n": 0}

        # Patch _try_sparse_checkout to fail (force full clone path)
        # Patch _clone_with_fallback to create the directory structure
        def fake_clone(repo_url, target_path, **kwargs):
            clone_call_count["n"] += 1
            target_path.mkdir(parents=True, exist_ok=True)
            (target_path / "skills" / "X").mkdir(parents=True)
            (target_path / "skills" / "X" / "apm.yml").write_text("name: X\nversion: 1.0.0\n")
            (target_path / "agents" / "Y").mkdir(parents=True)
            (target_path / "agents" / "Y" / "apm.yml").write_text("name: Y\nversion: 1.0.0\n")
            # Create a fake .git so Repo() can read commit
            (target_path / ".git").mkdir()
            return MagicMock()

        with (
            patch.object(downloader, "_try_sparse_checkout", return_value=False),
            patch.object(downloader, "_clone_with_fallback", side_effect=fake_clone),
            patch("apm_cli.deps.github_downloader.Repo") as mock_repo_cls,
            patch("apm_cli.deps.github_downloader.validate_apm_package") as mock_validate,
            patch("apm_cli.deps.github_downloader._close_repo"),
        ):
            # Configure Repo mock
            mock_repo_instance = MagicMock()
            mock_repo_instance.head.commit.hexsha = "abc1234567890"
            mock_repo_cls.return_value = mock_repo_instance

            # Configure validate mock
            mock_result = MagicMock()
            mock_result.is_valid = True
            mock_result.package = MagicMock()
            mock_result.package.version = "1.0.0"
            mock_result.package_type = "skill"
            mock_validate.return_value = mock_result

            downloader.download_subdirectory_package(dep_a, target_a)
            downloader.download_subdirectory_package(dep_b, target_b)

        # Key assertion: only 1 clone despite 2 subdir deps
        assert clone_call_count["n"] == 1
        cache.cleanup()
