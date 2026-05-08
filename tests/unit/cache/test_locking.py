"""Tests for cache locking and atomic landing primitives."""

import os
import threading
from pathlib import Path

from apm_cli.cache.locking import (
    atomic_land,
    cleanup_incomplete,
    shard_lock,
    stage_path,
)


class TestShardLock:
    """Test per-shard file lock creation."""

    def test_lock_file_adjacent_to_shard(self, tmp_path: Path) -> None:
        shard = tmp_path / "abc123"
        lock = shard_lock(shard)
        assert lock.lock_file == str(shard.with_suffix(".lock"))

    def test_lock_can_be_acquired(self, tmp_path: Path) -> None:
        shard = tmp_path / "abc123"
        lock = shard_lock(shard, timeout=5)
        with lock:
            assert lock.is_locked

    def test_per_shard_isolation(self, tmp_path: Path) -> None:
        """Lock on shard A does not block shard B."""
        shard_a = tmp_path / "shard_a"
        shard_b = tmp_path / "shard_b"
        lock_a = shard_lock(shard_a, timeout=1)
        lock_b = shard_lock(shard_b, timeout=1)

        with lock_a:
            # Should be able to acquire lock_b while lock_a is held
            with lock_b:
                assert lock_a.is_locked
                assert lock_b.is_locked


class TestStagePath:
    """Test staging path generation."""

    def test_format_contains_pid(self, tmp_path: Path) -> None:
        final = tmp_path / "final_dir"
        staged = stage_path(final)
        assert str(os.getpid()) in staged.name

    def test_same_parent_as_final(self, tmp_path: Path) -> None:
        final = tmp_path / "final_dir"
        staged = stage_path(final)
        assert staged.parent == final.parent

    def test_contains_incomplete_marker(self, tmp_path: Path) -> None:
        final = tmp_path / "final_dir"
        staged = stage_path(final)
        assert ".incomplete." in staged.name


class TestAtomicLand:
    """Test atomic landing protocol."""

    def test_successful_land(self, tmp_path: Path) -> None:
        final = tmp_path / "shard"
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "content.txt").write_text("hello")

        lock = shard_lock(final)
        result = atomic_land(staged, final, lock)

        assert result is True
        assert final.is_dir()
        assert (final / "content.txt").read_text() == "hello"
        assert not staged.exists()

    def test_race_condition_final_exists(self, tmp_path: Path) -> None:
        """If final already exists, staged is cleaned up and False returned."""
        final = tmp_path / "shard"
        final.mkdir()
        (final / "winner.txt").write_text("first")

        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "loser.txt").write_text("second")

        lock = shard_lock(final)
        result = atomic_land(staged, final, lock)

        assert result is False
        assert (final / "winner.txt").read_text() == "first"
        assert not (final / "loser.txt").exists()
        # Staged should be cleaned up
        assert not staged.exists()

    def test_concurrent_landing(self, tmp_path: Path) -> None:
        """Two threads racing to land the same shard -- exactly one wins."""
        final = tmp_path / "shard"
        results = []

        def land_thread(thread_id: int) -> None:
            staged = tmp_path / f"staged_{thread_id}"
            staged.mkdir()
            (staged / "marker.txt").write_text(f"thread_{thread_id}")
            lock = shard_lock(final, timeout=10)
            result = atomic_land(staged, final, lock)
            results.append((thread_id, result))

        t1 = threading.Thread(target=land_thread, args=(1,))
        t2 = threading.Thread(target=land_thread, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should succeed
        winners = [r for r in results if r[1] is True]
        losers = [r for r in results if r[1] is False]
        assert len(winners) == 1
        assert len(losers) == 1
        assert final.is_dir()


class TestCleanupIncomplete:
    """Test stale .incomplete.* cleanup."""

    def test_removes_incomplete_dirs(self, tmp_path: Path) -> None:
        # Create stale incomplete dirs
        (tmp_path / "shard1.incomplete.1234.5678").mkdir()
        (tmp_path / "shard2.incomplete.9999.1111").mkdir()
        # Create a valid shard (should NOT be removed)
        (tmp_path / "valid_shard").mkdir()

        removed = cleanup_incomplete(tmp_path)

        assert removed == 2
        assert not (tmp_path / "shard1.incomplete.1234.5678").exists()
        assert not (tmp_path / "shard2.incomplete.9999.1111").exists()
        assert (tmp_path / "valid_shard").exists()

    def test_no_incomplete_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "valid_shard").mkdir()
        removed = cleanup_incomplete(tmp_path)
        assert removed == 0

    def test_nonexistent_parent(self, tmp_path: Path) -> None:
        removed = cleanup_incomplete(tmp_path / "nonexistent")
        assert removed == 0
