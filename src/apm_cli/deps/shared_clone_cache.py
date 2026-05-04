"""Per-run shared clone cache for subdirectory dependency deduplication.

When multiple subdirectory deps reference the same upstream repository at
the same ref (e.g. ``github:owner/repo/skills/X@main`` and
``github:owner/repo/agents/Y@main``), a single clone is shared across all
consumers within one install run.  This mirrors uv's strategy of caching
Git repos by fully-resolved commit hash.

The cache is instance-scoped (NOT module-level) to avoid races between
parallel test invocations.  Thread-safety is guaranteed via per-key locks.

Lifecycle: create at install start, call ``cleanup()`` at end (or use as
a context manager).  Failed clones are NOT cached -- subsequent requests
for the same key retry with a fresh clone.
"""

import logging
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

_log = logging.getLogger(__name__)


class SharedCloneCache:
    """Thread-safe per-run cache of shared Git clones.

    Keys are ``(host, owner, repo, ref_or_None)`` tuples. The first
    caller for a given key performs the clone; concurrent callers block
    until the clone completes and then reuse the result.

    Args:
        base_dir: Parent directory for all temp clone dirs.  If None,
            uses the system temp directory.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir
        self._lock = threading.Lock()
        # Maps cache_key -> _CacheEntry
        self._entries: dict[tuple[str, str, str, str | None], _CacheEntry] = {}
        self._temp_dirs: list[str] = []

    def __enter__(self) -> "SharedCloneCache":
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()

    def get_or_clone(
        self,
        host: str,
        owner: str,
        repo: str,
        ref: str | None,
        clone_fn: Callable[[Path], None],
    ) -> Path:
        """Return a path to a shared clone, cloning on first access.

        Args:
            host: Git host (e.g. "github.com").
            owner: Repository owner.
            repo: Repository name.
            ref: Git ref (branch/tag/sha) or None for default branch.
            clone_fn: Callable that performs the clone into the given
                directory.  Called at most once per unique key.  Must
                raise on failure so the entry is not cached.

        Returns:
            Path to the cloned repo directory.

        Raises:
            Whatever ``clone_fn`` raises on failure.
        """
        key = (host, owner, repo, ref)
        entry = self._get_or_create_entry(key)

        with entry.lock:
            if entry.path is not None:
                # Already cloned successfully -- reuse.
                return entry.path
            if entry.error is not None:
                # A previous attempt failed.  Clear error to allow retry.
                entry.error = None

            # First caller (or retry after failure): perform the clone.
            temp_dir = tempfile.mkdtemp(
                dir=str(self._base_dir) if self._base_dir else None,
                prefix=f"apm_shared_{owner}_{repo}_",
            )
            clone_path = Path(temp_dir) / "repo"
            with self._lock:
                self._temp_dirs.append(temp_dir)
            try:
                clone_fn(clone_path)
                entry.path = clone_path
                return clone_path
            except Exception as exc:
                entry.error = exc
                raise

    def _get_or_create_entry(self, key: tuple) -> "_CacheEntry":
        """Retrieve or create a cache entry (thread-safe)."""
        with self._lock:
            if key not in self._entries:
                self._entries[key] = _CacheEntry()
            return self._entries[key]

    def cleanup(self) -> None:
        """Remove all temporary clone directories."""
        with self._lock:
            dirs_to_remove = list(self._temp_dirs)
            self._temp_dirs.clear()
            self._entries.clear()
        for d in dirs_to_remove:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                _log.debug("Failed to clean shared clone dir: %s", d, exc_info=True)


class _CacheEntry:
    """Internal: holds per-key state with its own lock for blocking waiters."""

    __slots__ = ("error", "lock", "path")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.path: Path | None = None
        self.error: Exception | None = None
