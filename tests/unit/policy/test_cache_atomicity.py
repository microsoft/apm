"""Tests for atomic cache writes -- concurrent writers must not corrupt cache.

Verifies that parallel ``_write_cache`` calls (simulating concurrent
``apm install`` invocations) always produce a parseable cache file
and metadata sidecar.  No torn writes, no truncated JSON, no partial YAML.
"""

from __future__ import annotations

import json  # noqa: F401
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from apm_cli.policy.discovery import (
    CACHE_SCHEMA_VERSION,  # noqa: F401
    _cache_key,  # noqa: F401
    _get_cache_dir,
    _read_cache,
    _read_cache_entry,
    _write_cache,
)
from apm_cli.policy.parser import load_policy  # noqa: F401
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

NUM_WRITERS = 16


def _make_policy(idx: int) -> ApmPolicy:
    """Create a distinguishable policy for writer ``idx``."""
    return ApmPolicy(
        name=f"writer-{idx}",
        version=f"{idx}.0",
        enforcement="warn",
        dependencies=DependencyPolicy(
            deny=(f"bad-pkg-{idx}",),
        ),
    )


class TestCacheAtomicity(unittest.TestCase):
    """16 concurrent writers -- every read-after-write must yield a valid cache."""

    def test_concurrent_writers_no_torn_files(self):
        """Parallel _write_cache calls never produce an unparseable cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"

            errors: list[str] = []

            def _writer(idx: int) -> str:
                """Write cache, then immediately read back via public API."""
                policy = _make_policy(idx)
                _write_cache(repo_ref, policy, root, chain_refs=[repo_ref])

                # Validate through the public API: _read_cache_entry must
                # return either a valid entry or None (never a corrupt read).
                # During concurrent writes to the same key, the meta file
                # may momentarily be from a different writer than the policy
                # file, but both must individually be valid.
                entry = _read_cache_entry(repo_ref, root)
                if entry is not None:
                    if not entry.policy.name.startswith("writer-"):
                        return f"idx={idx}: unexpected name {entry.policy.name!r}"
                    if not entry.chain_refs:
                        return f"idx={idx}: empty chain_refs"
                # entry=None is acceptable mid-race (meta not yet written)
                return ""  # success

            with ThreadPoolExecutor(max_workers=NUM_WRITERS) as pool:
                futures = {pool.submit(_writer, i): i for i in range(NUM_WRITERS)}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        errors.append(result)

            self.assertEqual(errors, [], f"Torn writes detected:\n" + "\n".join(errors))  # noqa: F541

            # Final validation: cache must be readable by the public API
            final = _read_cache(repo_ref, root)
            self.assertIsNotNone(final, "Final cache read returned None")
            self.assertTrue(final.found, "Final cache has no policy")
            self.assertTrue(final.cached, "Final cache not marked as cached")

    def test_concurrent_writers_different_keys(self):
        """Parallel writes to DIFFERENT cache keys never interfere."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            errors: list[str] = []

            def _writer(idx: int) -> str:
                repo_ref = f"org-{idx}/.github"
                policy = _make_policy(idx)
                _write_cache(repo_ref, policy, root, chain_refs=[repo_ref])

                entry = _read_cache_entry(repo_ref, root)
                if entry is None:
                    return f"idx={idx}: cache entry is None after write"
                if entry.policy.name != f"writer-{idx}":
                    return f"idx={idx}: expected name 'writer-{idx}' got {entry.policy.name!r}"
                return ""

            with ThreadPoolExecutor(max_workers=NUM_WRITERS) as pool:
                futures = {pool.submit(_writer, i): i for i in range(NUM_WRITERS)}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        errors.append(result)

            self.assertEqual(errors, [], f"Cross-key interference:\n" + "\n".join(errors))  # noqa: F541

    def test_rapid_overwrite_cycle(self):
        """100 rapid sequential overwrites -- last writer wins, no corruption."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "rapid-test/.github"

            for i in range(100):
                policy = _make_policy(i)
                _write_cache(repo_ref, policy, root, chain_refs=[repo_ref])

            entry = _read_cache_entry(repo_ref, root)
            self.assertIsNotNone(entry)
            # Must be one of the written policies (the last one in practice)
            self.assertTrue(
                entry.policy.name.startswith("writer-"),
                f"Unexpected policy name after 100 writes: {entry.policy.name!r}",
            )

    def test_no_tmp_files_left_behind(self):
        """After successful writes, no .tmp files remain in cache dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "cleanup-test/.github"

            for i in range(10):
                _write_cache(repo_ref, _make_policy(i), root)

            cache_dir = _get_cache_dir(root)
            tmp_files = list(cache_dir.glob("*.tmp"))
            self.assertEqual(
                tmp_files,
                [],
                f"Leftover .tmp files: {[f.name for f in tmp_files]}",
            )


if __name__ == "__main__":
    unittest.main()
