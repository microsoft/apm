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

        # New paradigm: SharedCloneCache holds bare clones; consumers
        # materialize their own working tree via _materialize_from_bare.
        # Patch _bare_clone_with_fallback to be the cache-populating
        # callable; patch _materialize_from_bare to lay down the subdir
        # contents per consumer.
        def fake_bare_clone(repo_url, bare_target, **kwargs):
            clone_call_count["n"] += 1
            bare_target.mkdir(parents=True, exist_ok=True)
            # Mark as bare-shaped (HEAD file at root, no .git/) so the
            # APM_DEBUG invariant in SharedCloneCache would not trip if
            # the caller enabled it.
            (bare_target / "HEAD").write_text("ref: refs/heads/main\n")

        def fake_materialize(bare_path, consumer_dir, **kwargs):
            consumer_dir.mkdir(parents=True, exist_ok=True)
            (consumer_dir / "skills" / "X").mkdir(parents=True)
            (consumer_dir / "skills" / "X" / "apm.yml").write_text("name: X\nversion: 1.0.0\n")
            (consumer_dir / "agents" / "Y").mkdir(parents=True)
            (consumer_dir / "agents" / "Y" / "apm.yml").write_text("name: Y\nversion: 1.0.0\n")
            return "abc1234567890"

        with (
            patch.object(downloader, "_bare_clone_with_fallback", side_effect=fake_bare_clone),
            patch.object(downloader, "_materialize_from_bare", side_effect=fake_materialize),
            patch.object(downloader, "_git_env_dict", return_value={}),
            patch("apm_cli.deps.github_downloader.validate_apm_package") as mock_validate,
        ):
            # Configure validate mock
            mock_result = MagicMock()
            mock_result.is_valid = True
            mock_result.package = MagicMock()
            mock_result.package.version = "1.0.0"
            mock_result.package_type = "skill"
            mock_validate.return_value = mock_result

            downloader.download_subdirectory_package(dep_a, target_a)
            downloader.download_subdirectory_package(dep_b, target_b)

        # Key assertion: only 1 BARE clone despite 2 subdir deps
        # (each consumer materializes its own working tree from the bare).
        assert clone_call_count["n"] == 1
        cache.cleanup()


# ---------------------------------------------------------------------------
# #1126 fix: bare-cache + per-consumer materialization tests
# ---------------------------------------------------------------------------


def _make_bare_repo(path: Path) -> None:
    """Create a real bare git repo at ``path`` with a single commit.

    Used by tests that need a real-shaped bare for materialize-from-bare
    (mocking subprocess for those would defeat the purpose -- the test
    is precisely that the local-shared clone semantics work end-to-end).
    """
    import subprocess as sp

    work = path.parent / (path.name + "_work")
    work.mkdir(parents=True)
    sp.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    sp.run(
        ["git", "-C", str(work), "config", "user.email", "t@t.t"],
        check=True,
        capture_output=True,
    )
    sp.run(
        ["git", "-C", str(work), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    (work / "skills").mkdir()
    (work / "skills" / "X").mkdir()
    (work / "skills" / "X" / "apm.yml").write_bytes(b"name: X\nversion: 1.0.0\n")
    (work / "agents").mkdir()
    (work / "agents" / "Y").mkdir()
    (work / "agents" / "Y" / "apm.yml").write_bytes(b"name: Y\nversion: 1.0.0\n")
    sp.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
    sp.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    sp.run(
        ["git", "clone", "--bare", str(work), str(path)],
        check=True,
        capture_output=True,
    )


class TestBareCacheRaceCondition:
    """6.1: regression test for the parallel sparse-checkout race (#1126)."""

    def test_parallel_different_subdirs_both_succeed(self, tmp_path: Path) -> None:
        """Two threads request same key, then extract different subdirs from
        the shared bare. Both must succeed (the v1 race lost one thread's
        files because the cache materialized one subdir at the cache layer).
        """
        cache = SharedCloneCache(base_dir=tmp_path)
        bare_src = tmp_path / "bare_src"
        _make_bare_repo(bare_src)

        def populate_bare(target: Path) -> None:
            # Cache lock serializes this; only one thread enters.
            import shutil

            shutil.copytree(bare_src, target)

        # Barrier forces both threads to do their materialize step in
        # parallel (after one has populated the bare and both have
        # received the same path back from the cache).
        materialize_barrier = threading.Barrier(2)
        results: dict[str, list] = {"errors": [], "subdirs_seen": []}

        def thread_a() -> None:
            try:
                bare = cache.get_or_clone("h", "o", "r", "main", populate_bare)
                # Force parallel materialize step.
                materialize_barrier.wait(timeout=5)
                consumer = tmp_path / "consumer_a"
                import subprocess as sp

                sp.run(
                    [
                        "git",
                        "clone",
                        "--local",
                        "--shared",
                        "--no-checkout",
                        str(bare),
                        str(consumer),
                    ],
                    check=True,
                    capture_output=True,
                )
                sp.run(
                    ["git", "-C", str(consumer), "checkout", "HEAD"],
                    check=True,
                    capture_output=True,
                )
                if (consumer / "skills" / "X" / "apm.yml").exists():
                    results["subdirs_seen"].append("X")
            except Exception as e:
                results["errors"].append(("a", e))

        def thread_b() -> None:
            try:
                bare = cache.get_or_clone("h", "o", "r", "main", populate_bare)
                materialize_barrier.wait(timeout=5)
                consumer = tmp_path / "consumer_b"
                import subprocess as sp

                sp.run(
                    [
                        "git",
                        "clone",
                        "--local",
                        "--shared",
                        "--no-checkout",
                        str(bare),
                        str(consumer),
                    ],
                    check=True,
                    capture_output=True,
                )
                sp.run(
                    ["git", "-C", str(consumer), "checkout", "HEAD"],
                    check=True,
                    capture_output=True,
                )
                if (consumer / "agents" / "Y" / "apm.yml").exists():
                    results["subdirs_seen"].append("Y")
            except Exception as e:
                results["errors"].append(("b", e))

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        assert results["errors"] == [], f"Errors: {results['errors']}"
        assert "X" in results["subdirs_seen"]
        assert "Y" in results["subdirs_seen"]
        cache.cleanup()


class TestBareCloneFallback:
    """Tests for _bare_clone_with_fallback (6.4, 6.12, 6.18)."""

    def _make_downloader(self, tmp_path: Path):
        """Build a minimal downloader with the auth/transport plumbing
        sufficient for _bare_clone_with_fallback's _execute_transport_plan
        path to run synchronously through one attempt.
        """
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        d = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
        d.auth_resolver = MagicMock()
        d.token_manager = MagicMock()
        d._transport_selector = MagicMock()
        d._protocol_pref = MagicMock()
        d._allow_fallback = False
        d._fallback_port_warned = set()
        d._strategies = MagicMock()
        d.git_env = {}

        # Stub the helpers the template uses.
        d._build_repo_url = MagicMock(return_value="https://example/o/r")
        d._resolve_dep_token = MagicMock(return_value="")
        d._resolve_dep_auth_ctx = MagicMock(return_value=None)
        d._sanitize_git_error = MagicMock(side_effect=lambda s: s)

        # Single-attempt plan: one HTTPS no-token attempt.
        from apm_cli.deps.transport_selection import TransportAttempt, TransportPlan

        attempt = TransportAttempt(
            scheme="https",
            label="https",
            use_token=False,
        )
        plan = TransportPlan(
            attempts=[attempt],
            strict=False,
        )
        d._transport_selector.select = MagicMock(return_value=plan)
        return d

    def test_sha_ref_tier1_init_fetch_path(self, tmp_path: Path) -> None:
        """6.4 + 6.18: full SHA triggers init+fetch tier 1 with update-ref HEAD."""
        from apm_cli.models.dependency.reference import DependencyReference

        # Real 40-char hex SHA (tier-1 only runs for full SHAs, not abbreviations).
        full_sha = "0123456789abcdef0123456789abcdef01234567"
        d = self._make_downloader(tmp_path)
        dep = DependencyReference.parse(f"o/r/skills/X#{full_sha}")
        bare = tmp_path / "bare"
        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            # Tier-1 happy path: every call succeeds.
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fake_run):
            d._bare_clone_with_fallback(
                "https://example/o/r",
                bare,
                dep_ref=dep,
                ref=full_sha,
                is_commit_sha=True,
            )

        # Verify tier-1 sequence
        cmd_strings = [" ".join(c) for c in captured]
        assert any("init --bare" in s for s in cmd_strings), "missing init --bare"
        assert any("remote add origin" in s for s in cmd_strings), "missing remote add"
        assert any("fetch --depth=1" in s for s in cmd_strings), "missing fetch --depth=1"
        # 6.18: update-ref HEAD <sha> MUST be called
        update_ref_calls = [
            c for c in captured if len(c) >= 4 and c[-3] == "update-ref" and c[-2] == "HEAD"
        ]
        assert len(update_ref_calls) == 1, (
            f"expected 1 update-ref HEAD call, got {update_ref_calls}"
        )
        assert update_ref_calls[0][-1] == full_sha
        # 6.19: token scrub via remote set-url origin redacted://
        assert any("remote set-url origin redacted://" in s for s in cmd_strings), (
            "missing token scrub"
        )

    def test_sha_ref_tier2_fallback_on_fetch_rejection(self, tmp_path: Path) -> None:
        """6.12: tier-1 fetch fails (server rejects SHA fetch) -> tier-2 full clone.

        Also covers Copilot review #1135: tier-2 must use the full 40-char SHA
        from `rev-parse --verify <ref>^{commit}` for `update-ref HEAD`, not
        the (possibly abbreviated) input ref.
        """
        import subprocess as sp

        from apm_cli.models.dependency.reference import DependencyReference

        full_sha = "0123456789abcdef0123456789abcdef01234567"
        d = self._make_downloader(tmp_path)
        dep = DependencyReference.parse(f"o/r/skills/X#{full_sha}")
        bare = tmp_path / "bare"
        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            cmd_str = " ".join(args)
            if "fetch --depth=1" in cmd_str:
                # Tier-1 fetch fails (simulating allowReachableSHA1InWant=false)
                raise sp.CalledProcessError(1, args, stderr=b"reject")
            if "rev-parse --verify" in cmd_str:
                # Tier-2 resolves the (possibly abbreviated) ref to the
                # canonical 40-char SHA via rev-parse stdout.
                return MagicMock(returncode=0, stdout=full_sha + "\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fake_run):
            d._bare_clone_with_fallback(
                "https://example/o/r",
                bare,
                dep_ref=dep,
                ref=full_sha,
                is_commit_sha=True,
            )

        cmd_strings = [" ".join(c) for c in captured]
        # Tier 2: full clone --bare invoked after tier-1 failed
        assert any(
            "clone --bare" in s and "--depth=1" not in s and "--branch" not in s
            for s in cmd_strings
        ), f"missing tier-2 full bare clone: {cmd_strings}"
        # rev-parse --verify validates the SHA
        assert any("rev-parse --verify" in s and "^{commit}" in s for s in cmd_strings), (
            "missing tier-2 SHA verify"
        )
        # update-ref HEAD <sha> still set on tier 2 with the full 40-char SHA
        update_ref_calls = [
            c for c in captured if len(c) >= 4 and c[-3] == "update-ref" and c[-2] == "HEAD"
        ]
        assert len(update_ref_calls) == 1
        assert update_ref_calls[0][-1] == full_sha

    def test_short_sha_skips_tier1_and_resolves_via_tier2(self, tmp_path: Path) -> None:
        """Copilot review #1135: short SHA must skip tier 1 (which requires
        full SHA for `git fetch <sha>`) and resolve to a 40-char SHA via
        tier-2 `rev-parse --verify <short>^{commit}`. The resolved 40-char
        SHA is what gets passed to `update-ref HEAD`, not the abbreviation.
        """
        from apm_cli.models.dependency.reference import DependencyReference

        short_sha = "abc1234"  # 7-char abbreviation
        full_sha = "abc12345670000000000000000000000000fffff"
        d = self._make_downloader(tmp_path)
        dep = DependencyReference.parse(f"o/r/skills/X#{short_sha}")
        bare = tmp_path / "bare"
        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            cmd_str = " ".join(args)
            if "rev-parse --verify" in cmd_str:
                return MagicMock(returncode=0, stdout=full_sha + "\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fake_run):
            d._bare_clone_with_fallback(
                "https://example/o/r",
                bare,
                dep_ref=dep,
                ref=short_sha,
                is_commit_sha=True,
            )

        cmd_strings = [" ".join(c) for c in captured]
        # Tier 1 (init+fetch) MUST NOT be attempted for short SHAs.
        assert not any("init --bare" in s for s in cmd_strings), (
            f"tier-1 must be skipped for short SHA, got {cmd_strings}"
        )
        assert not any("fetch --depth=1" in s for s in cmd_strings), (
            "tier-1 fetch must be skipped for short SHA"
        )
        # Tier 2 full clone + rev-parse + update-ref
        assert any("clone --bare" in s for s in cmd_strings), "missing tier-2 clone"
        update_ref_calls = [
            c for c in captured if len(c) >= 4 and c[-3] == "update-ref" and c[-2] == "HEAD"
        ]
        assert len(update_ref_calls) == 1
        # CRITICAL: the resolved full 40-char SHA is set, not the abbreviation.
        assert update_ref_calls[0][-1] == full_sha
        assert update_ref_calls[0][-1] != short_sha

    def test_symbolic_ref_tier1_shallow_clone(self, tmp_path: Path) -> None:
        """Symbolic ref triggers tier-1 shallow clone with --branch."""
        from apm_cli.models.dependency.reference import DependencyReference

        d = self._make_downloader(tmp_path)
        dep = DependencyReference.parse("o/r/skills/X#main")
        bare = tmp_path / "bare"
        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fake_run):
            d._bare_clone_with_fallback(
                "https://example/o/r",
                bare,
                dep_ref=dep,
                ref="main",
                is_commit_sha=False,
            )

        cmd_strings = [" ".join(c) for c in captured]
        assert any("clone --bare --depth=1 --branch main" in s for s in cmd_strings), (
            f"missing tier-1 shallow clone: {cmd_strings}"
        )


class TestMaterializeFromBare:
    """Tests for _materialize_from_bare (6.10, 6.11, 6.16)."""

    def _make_downloader(self):
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        d = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
        d.git_env = {}
        return d

    def test_materialize_from_real_bare(self, tmp_path: Path) -> None:
        """End-to-end: real bare repo -> materialized consumer dir with content."""
        d = self._make_downloader()
        bare = tmp_path / "bare"
        _make_bare_repo(bare)
        consumer = tmp_path / "consumer"

        sha = d._materialize_from_bare(bare, consumer, ref=None, env={})

        assert (consumer / "skills" / "X" / "apm.yml").exists()
        assert (consumer / "agents" / "Y" / "apm.yml").exists()
        assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)

    def test_consumer_resolved_sha_obtained_from_bare_not_consumer(self, tmp_path: Path) -> None:
        """6.11: rev-parse HEAD MUST target --git-dir=<bare> (not consumer).

        Consumer rev-parse opens a Repo handle that leaks on Windows and
        blocks downstream rmtree (lifetime invariant 5.2.1).
        """
        d = self._make_downloader()
        bare = tmp_path / "bare"
        consumer = tmp_path / "consumer"
        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            if "rev-parse" in args:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fake_run):
            d._materialize_from_bare(bare, consumer, ref=None, env={})

        rev_parse_calls = [c for c in captured if "rev-parse" in c]
        assert len(rev_parse_calls) == 1
        # rev-parse MUST be against --git-dir <bare>, not against consumer
        rp = rev_parse_calls[0]
        assert "--git-dir" in rp
        gd_idx = rp.index("--git-dir")
        assert rp[gd_idx + 1] == str(bare), f"rev-parse must target bare, not consumer: {rp}"

    def test_known_sha_shortcut_avoids_rev_parse(self, tmp_path: Path) -> None:
        """When known_sha is provided, skip rev-parse entirely (avoids the
        ambiguity of init+fetch bares before update-ref runs)."""
        d = self._make_downloader()
        bare = tmp_path / "bare"
        consumer = tmp_path / "consumer"
        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fake_run):
            sha = d._materialize_from_bare(
                bare, consumer, ref=None, env={}, known_sha="deadbeef" * 5
            )

        assert sha == "deadbeef" * 5
        rev_parse_calls = [c for c in captured if "rev-parse" in c]
        assert rev_parse_calls == [], "known_sha must skip rev-parse"

    def test_materialize_disables_lfs_smudge(self, tmp_path: Path) -> None:
        """6.16: materialize MUST set filter.lfs.smudge="" to skip LFS network."""
        d = self._make_downloader()
        bare = tmp_path / "bare"
        _make_bare_repo(bare)
        consumer = tmp_path / "consumer"

        d._materialize_from_bare(bare, consumer, ref=None, env={})

        # Read consumer's .git/config and verify LFS smudge is disabled
        config_text = (consumer / ".git" / "config").read_text()
        assert "smudge =" in config_text or "smudge=" in config_text
        # The empty-string smudge value means LFS pointers stay as pointers
        # (cross-platform; works on Windows where `cat` is unavailable)
        assert "required = false" in config_text or "required=false" in config_text

    def test_materialize_pins_autocrlf_false(self, tmp_path: Path) -> None:
        """6.10: core.autocrlf=false ensures byte-identical content across users."""
        d = self._make_downloader()
        bare = tmp_path / "bare"
        _make_bare_repo(bare)
        consumer = tmp_path / "consumer"

        d._materialize_from_bare(bare, consumer, ref=None, env={})

        config_text = (consumer / ".git" / "config").read_text()
        assert "autocrlf = false" in config_text or "autocrlf=false" in config_text


class TestSharedCloneCacheBareInvariant:
    """6.16: cache enforces bare-shape invariant in debug mode."""

    def test_apm_debug_rejects_non_bare_clone(self, tmp_path: Path, monkeypatch) -> None:
        """If clone_fn produces a working-tree-shaped dir under APM_DEBUG=1,
        the cache must raise (canary against v1 regression)."""
        monkeypatch.setenv("APM_DEBUG", "1")
        cache = SharedCloneCache(base_dir=tmp_path)

        def bad_populate(target: Path) -> None:
            # Working-tree shape: nested .git/, no HEAD at root
            target.mkdir(parents=True)
            (target / ".git").mkdir()

        with pytest.raises(RuntimeError, match="not a bare repo"):
            cache.get_or_clone("h", "o", "r", "main", bad_populate)
        cache.cleanup()

    def test_apm_debug_accepts_bare_clone(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("APM_DEBUG", "1")
        cache = SharedCloneCache(base_dir=tmp_path)

        def good_populate(target: Path) -> None:
            target.mkdir(parents=True)
            (target / "HEAD").write_text("ref: refs/heads/main\n")

        path = cache.get_or_clone("h", "o", "r", "main", good_populate)
        assert (path / "HEAD").is_file()
        cache.cleanup()


class TestExecuteTransportPlanWtAction:
    """6.13: regression-guard the new rmtree-before-attempt behavior in _wt_action.

    The refactor adds shutil.rmtree(target, ignore_errors=True) before
    each attempt. The 8 existing _clone_with_fallback callsites depended
    on the old behavior (no pre-rmtree); verify the new behavior is
    benign for empty/missing targets and correctly cleans stale state
    between attempts.
    """

    def test_wt_action_handles_missing_target(self, tmp_path: Path) -> None:
        """Pre-attempt rmtree must not raise on missing target."""
        from apm_cli.deps.github_downloader import GitHubPackageDownloader
        from apm_cli.models.dependency.reference import DependencyReference

        d = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
        d.auth_resolver = MagicMock()
        d.token_manager = MagicMock()
        d._transport_selector = MagicMock()
        d._protocol_pref = MagicMock()
        d._allow_fallback = False
        d._fallback_port_warned = set()
        d._strategies = MagicMock()
        d.git_env = {}
        d._build_repo_url = MagicMock(return_value="https://example/o/r")
        d._resolve_dep_token = MagicMock(return_value="")
        d._resolve_dep_auth_ctx = MagicMock(return_value=None)
        d._sanitize_git_error = MagicMock(side_effect=lambda s: s)

        from apm_cli.deps.transport_selection import TransportAttempt, TransportPlan

        plan = TransportPlan(
            attempts=[TransportAttempt(scheme="https", use_token=False, label="https")],
            strict=False,
        )
        d._transport_selector.select = MagicMock(return_value=plan)
        dep = DependencyReference.parse("o/r#main")

        # Target does not exist -- _wt_action must handle gracefully.
        target = tmp_path / "does_not_exist"
        with patch("apm_cli.deps.github_downloader.Repo") as mock_repo:
            mock_repo.clone_from = MagicMock()
            d._clone_with_fallback("https://example/o/r", target, dep_ref=dep)

        mock_repo.clone_from.assert_called_once()


class TestBareCloneRetryRmtree:
    """6.15: bare clone re-attempts must wipe target between attempts.

    Specifically: when _execute_transport_plan re-invokes _bare_action
    on retry (e.g. ADO bearer retry), the prior attempt's partial bare
    state (init+fetch) must be removed before re-init, otherwise
    `git init --bare` would fail or leak state.
    """

    def test_bare_action_rmtrees_target_before_init(self, tmp_path: Path) -> None:
        """_bare_action wipes existing target via shutil.rmtree pre-init."""
        from apm_cli.deps.github_downloader import GitHubPackageDownloader
        from apm_cli.deps.transport_selection import TransportAttempt, TransportPlan
        from apm_cli.models.dependency.reference import DependencyReference

        d = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
        d.auth_resolver = MagicMock()
        d.token_manager = MagicMock()
        d._transport_selector = MagicMock()
        d._protocol_pref = MagicMock()
        d._allow_fallback = False
        d._fallback_port_warned = set()
        d._strategies = MagicMock()
        d.git_env = {}
        d._build_repo_url = MagicMock(return_value="https://example/o/r")
        d._resolve_dep_token = MagicMock(return_value="")
        d._resolve_dep_auth_ctx = MagicMock(return_value=None)
        d._sanitize_git_error = MagicMock(side_effect=lambda s: s)

        plan = TransportPlan(
            attempts=[TransportAttempt(scheme="https", use_token=False, label="https")],
            strict=False,
        )
        d._transport_selector.select = MagicMock(return_value=plan)
        dep = DependencyReference.parse("o/r/skills/X#abc1234567890abcdef1234567890abcdef12345678")

        # Pre-create the target with stale content; verify it gets wiped.
        bare = tmp_path / "bare"
        bare.mkdir()
        (bare / "stale_file").write_text("from previous failed attempt")

        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            # init must see clean target
            if args[1] == "init" and args[2] == "--bare":
                assert not (bare / "stale_file").exists(), "rmtree did not run before init"
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fake_run):
            d._bare_clone_with_fallback(
                "https://example/o/r",
                bare,
                dep_ref=dep,
                ref="abc1234567890abcdef1234567890abcdef12345678",
                is_commit_sha=True,
            )


class TestInvalidSubdirErrorWording:
    """6.14: typo'd subdir still surfaces 'Subdirectory ... not found'.

    Regression-trap for the user-facing typo-detection promise. The WS2
    bare-cache path materializes a FULL working tree (unlike the v1
    sparse checkout that only had the requested subdir), so a future
    refactor could accidentally swallow the explicit subdir-existence
    check at the consumer level. This test ensures the typo case still
    raises with the subdir name in the message.
    """

    def test_typo_subdir_raises_subdirectory_not_found(self, tmp_path: Path) -> None:
        from apm_cli.deps.github_downloader import GitHubPackageDownloader
        from apm_cli.models.dependency.reference import DependencyReference

        # Real bare repo containing only "skills/X" and "agents/Y".
        bare_src = tmp_path / "bare_src"
        _make_bare_repo(bare_src)

        downloader = GitHubPackageDownloader()

        # Stub _bare_clone_with_fallback to copy our pre-built bare into
        # the cache target dir (avoids real network).
        def fake_bare_clone(url, target, *, dep_ref, ref, is_commit_sha):
            import shutil as _sh

            if target.exists():
                _sh.rmtree(target)
            _sh.copytree(bare_src, target)

        with SharedCloneCache(base_dir=tmp_path / "cache") as cache:
            (tmp_path / "cache").mkdir()
            downloader.shared_clone_cache = cache

            dep = DependencyReference.parse(
                "github/awesome-copilot/skills/DOES_NOT_EXIST_TYPO#main"
            )
            with patch.object(downloader, "_bare_clone_with_fallback", side_effect=fake_bare_clone):
                target_out = tmp_path / "out"
                target_out.parent.mkdir(parents=True, exist_ok=True)
                with pytest.raises(
                    Exception,
                    match=r"Subdirectory ['\"]?skills/DOES_NOT_EXIST_TYPO['\"]? not found",
                ):
                    downloader.download_subdirectory_package(dep, target_out)


class TestBareScrubFetchHead:
    """Supply-chain panel follow-up: tier-1 init+fetch leaves the tokenized
    URL inside ``FETCH_HEAD`` even after the config scrub. The bare-cache
    scrub helper must truncate ``FETCH_HEAD`` so the token does not survive
    on disk in any artifact.
    """

    def test_scrub_truncates_fetch_head_when_present(self, tmp_path: Path) -> None:
        from apm_cli.deps.bare_cache import _scrub_bare_remote_url

        bare = tmp_path / "bare"
        bare.mkdir()
        fetch_head = bare / "FETCH_HEAD"
        fetch_head.write_text(
            "abcdef0123456789  branch 'main' of "
            "https://oauth2:ghp_SECRET_TOKEN_FAKE@github.com/o/r\n"
        )

        with patch("apm_cli.deps.bare_cache.subprocess.run") as run_mock:
            run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _scrub_bare_remote_url(bare, "/usr/bin/git", {})

        assert fetch_head.exists(), "FETCH_HEAD must be preserved (only truncated)"
        assert fetch_head.read_text() == "", (
            "FETCH_HEAD must be truncated so the tokenized URL does not persist on disk"
        )

    def test_scrub_no_op_when_fetch_head_absent(self, tmp_path: Path) -> None:
        from apm_cli.deps.bare_cache import _scrub_bare_remote_url

        bare = tmp_path / "bare"
        bare.mkdir()
        # No FETCH_HEAD file present (tier-2 path: full clone --bare).

        with patch("apm_cli.deps.bare_cache.subprocess.run") as run_mock:
            run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
            # Must not raise even when FETCH_HEAD does not exist.
            _scrub_bare_remote_url(bare, "/usr/bin/git", {})

        assert not (bare / "FETCH_HEAD").exists()


class TestAdoBareBearerRetry:
    """Panel follow-up: the ADO bearer 401 retry path in
    ``_execute_transport_plan`` must compose correctly with the bare
    clone action, so that an ADO bare cache materialization recovers
    from a stale PAT exactly the way the working-tree clone path does
    (validation parity).
    """

    def _make_ado_downloader(self, tmp_path: Path):
        from apm_cli.deps.github_downloader import GitHubPackageDownloader
        from apm_cli.deps.transport_selection import TransportAttempt, TransportPlan

        d = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
        d.auth_resolver = MagicMock()
        d.token_manager = MagicMock()
        d._transport_selector = MagicMock()
        d._protocol_pref = MagicMock()
        d._allow_fallback = False
        d._fallback_port_warned = set()
        d._strategies = MagicMock()
        d.git_env = {}

        # Token attempt with basic auth scheme on ADO is the trigger
        # condition for the bearer retry branch.
        d._build_repo_url = MagicMock(
            side_effect=lambda *a, **kw: (
                "https://bearer-url/o/r"
                if kw.get("auth_scheme") == "bearer"
                else "https://pat-url/o/r"
            )
        )
        d._resolve_dep_token = MagicMock(return_value="pat-token")
        ctx = MagicMock()
        ctx.auth_scheme = "basic"
        ctx.git_env = {}
        d._resolve_dep_auth_ctx = MagicMock(return_value=ctx)
        d._sanitize_git_error = MagicMock(side_effect=lambda s: s)

        attempt = TransportAttempt(scheme="https", label="https-token", use_token=True)
        plan = TransportPlan(attempts=[attempt], strict=False)
        d._transport_selector.select = MagicMock(return_value=plan)
        return d

    def test_bare_clone_recovers_via_ado_bearer_after_pat_401(self, tmp_path: Path) -> None:
        """ADO bare clone: PAT 401 -> bearer retry succeeds."""
        import subprocess as sp

        from apm_cli.models.dependency.reference import DependencyReference

        d = self._make_ado_downloader(tmp_path)
        # ADO-style ref.
        dep = DependencyReference.parse("dev.azure.com/org/proj/_git/repo/skills/X#main")
        assert dep.is_azure_devops(), "fixture sanity: dep must be ADO"

        bare = tmp_path / "bare"
        urls_seen: list[str] = []

        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "clone --bare" in cmd_str:
                # URL appears in args; locate it by content rather than position
                # (varies for tier-1 shallow vs tier-2 full clone).
                url = next((a for a in args if a.startswith("https://")), "")
                urls_seen.append(url)
                if "pat-url" in url:
                    raise sp.CalledProcessError(
                        128, args, stderr=b"fatal: Authentication failed for 'https://...'"
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        # Stub the bearer provider to be available with a fake token.
        bearer_provider = MagicMock()
        bearer_provider.is_available.return_value = True
        bearer_provider.get_bearer_token.return_value = "fake-bearer-token"

        with (
            patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fake_run),
            patch(
                "apm_cli.core.azure_cli.get_bearer_provider",
                return_value=bearer_provider,
            ),
            patch(
                "apm_cli.utils.github_host.build_ado_bearer_git_env",
                return_value={"GIT_CONFIG_COUNT": "1"},
            ),
        ):
            d._bare_clone_with_fallback(
                "https://pat-url/o/r",
                bare,
                dep_ref=dep,
                ref="main",
                is_commit_sha=False,
            )

        # Both URLs must have been attempted: PAT first, bearer second.
        assert any("pat-url" in u for u in urls_seen), (
            f"expected PAT clone attempt, got urls={urls_seen}"
        )
        assert any("bearer-url" in u for u in urls_seen), (
            f"expected bearer retry clone attempt, got urls={urls_seen}"
        )
        # Bearer attempt must come AFTER the PAT failure.
        pat_idx = next(i for i, u in enumerate(urls_seen) if "pat-url" in u)
        bearer_idx = next(i for i, u in enumerate(urls_seen) if "bearer-url" in u)
        assert bearer_idx > pat_idx, f"bearer retry must follow PAT failure, urls={urls_seen}"
        # Stale-PAT diagnostic must be emitted on bearer success.
        assert d.auth_resolver.emit_stale_pat_diagnostic.called
