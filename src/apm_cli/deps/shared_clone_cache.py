"""Per-run shared clone cache for subdirectory dependency deduplication.

When multiple subdirectory deps reference the same upstream repository at
the same ref (e.g. ``github:owner/repo/skills/X@main`` and
``github:owner/repo/agents/Y@main``), a single BARE clone is shared across
all consumers within one install run. Each consumer materializes its own
working tree from the bare via ``git clone --local --shared --no-checkout``
plus ``git checkout HEAD``. This mirrors uv's strategy of caching Git repos
by fully-resolved commit hash, and the WS3 ``GitCache`` pattern internally.

Why bare (#1126): subdir-agnostic. Two parallel consumers requesting
different subdirectories of the same repo+ref share one bare without
racing on sparse-checkout state (the original v1 design materialized a
sparse working tree at the cache layer and lost the second consumer's
files when both threads raced through the cache).

The cache is instance-scoped (NOT module-level) to avoid races between
parallel test invocations.  Thread-safety is guaranteed via per-key locks.

Lifecycle: create at install start, call ``cleanup()`` at end (or use as
a context manager).  Failed clones are NOT cached -- subsequent requests
for the same key retry with a fresh clone.
"""

import logging
import os
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
        # Maps (host, owner, repo) -> list of (ref, bare_path) tuples.
        # Used to locate an existing bare for the same repo when a new ref
        # (typically a SHA pin on a transitive dep) is requested.
        self._repo_bares: dict[tuple[str, str, str], list[tuple[str | None, Path]]] = {}
        # Per-bare-path locks to serialise concurrent Tier-0 fetches into the
        # same bare (git concurrent pack-file writes are not safe).
        self._bare_fetch_locks: dict[Path, threading.Lock] = {}

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
        fetch_fn: Callable[[Path, str], bool] | None = None,
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
            fetch_fn: Optional callable ``(bare_path, sha) -> bool`` that
                tries to fetch a missing SHA into an already-cloned bare
                for the same repo (any ref).  When provided and a suitable
                bare exists, it is tried before falling back to a fresh
                clone.  Must not raise -- return False to signal failure.

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

            # Tier-0: try fetching the SHA into an existing bare for the
            # same repo (different ref).  This avoids a fresh network clone
            # when a transitive dep pins a SHA that is missing only because
            # the initial shallow bare did not include that commit.
            if ref and fetch_fn:
                existing_bare = self._find_repo_bare(host, owner, repo)
                if existing_bare is not None:
                    # Acquire a per-bare lock so concurrent Tier-0 fetches
                    # into the same bare are serialised (git pack-file writes
                    # are not concurrent-safe).
                    with self._lock:
                        if existing_bare not in self._bare_fetch_locks:
                            self._bare_fetch_locks[existing_bare] = threading.Lock()
                        bare_lock = self._bare_fetch_locks[existing_bare]
                    try:
                        with bare_lock:
                            if fetch_fn(existing_bare, ref):
                                entry.path = existing_bare
                                with self._lock:
                                    repo_key = (host, owner, repo)
                                    if repo_key not in self._repo_bares:
                                        self._repo_bares[repo_key] = []
                                    self._repo_bares[repo_key].append((ref, existing_bare))
                                return existing_bare
                    except Exception:
                        _log.info(
                            "Bare fetch miss for %s/%s/%s ref=%s, falling back to fresh clone",
                            host,
                            owner,
                            repo,
                            ref,
                        )

            # First caller (or retry after failure): perform the clone.
            temp_dir = tempfile.mkdtemp(
                dir=str(self._base_dir) if self._base_dir else None,
                prefix=f"apm_shared_{owner}_{repo}_",
            )
            clone_path = Path(temp_dir) / "bare"
            with self._lock:
                self._temp_dirs.append(temp_dir)
            try:
                clone_fn(clone_path)
                # Debug-mode shape invariant: clone_fn MUST produce a
                # bare repo. A bare has HEAD as a regular file at the
                # root and no nested .git/ dir. Working-tree clones
                # have it the other way around. This is the canary
                # that catches a regression where someone reverts to
                # the v1 "materialize-in-cache" pattern. See 6.16.
                if os.environ.get("APM_DEBUG"):
                    head_file = clone_path / "HEAD"
                    git_dir = clone_path / ".git"
                    if not head_file.is_file() or git_dir.exists():
                        raise RuntimeError(
                            f"SharedCloneCache invariant violated: "
                            f"{clone_path} is not a bare repo "
                            f"(HEAD file present: {head_file.is_file()}, "
                            f".git/ present: {git_dir.exists()})"
                        )
                entry.path = clone_path
                with self._lock:
                    repo_key = (host, owner, repo)
                    if repo_key not in self._repo_bares:
                        self._repo_bares[repo_key] = []
                    self._repo_bares[repo_key].append((ref, clone_path))
                return clone_path
            except Exception as exc:
                entry.error = exc
                raise

    def _find_repo_bare(self, host: str, owner: str, repo: str) -> Path | None:
        """Return an existing bare path for the same repo (any ref), or None.

        Searches the reverse index populated after each successful clone.
        Returns the path of the first registered bare for ``(host, owner,
        repo)`` regardless of which ref it was originally cloned at.

        Args:
            host: Git host (e.g. "github.com").
            owner: Repository owner.
            repo: Repository name.

        Returns:
            A :class:`Path` to an existing bare, or ``None`` if none is
            registered yet.
        """
        with self._lock:
            entries = self._repo_bares.get((host, owner, repo))
            if entries:
                return entries[0][1]
            return None

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
            self._repo_bares.clear()
            self._bare_fetch_locks.clear()
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
