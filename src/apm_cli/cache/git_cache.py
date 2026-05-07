"""Persistent content-addressable git cache.

Two-tier structure:
- ``git/db_v1/<shard>/`` -- bare git repositories (full clones)
- ``git/checkouts_v1/<shard>/<sha>/`` -- per-SHA working copies

Cache keys are derived from normalized repository URLs (see
:mod:`url_normalize`). Checkouts are keyed by resolved SHA, never
by mutable ref strings.

Resolution flow:
1. If lockfile provides SHA for this dep -> use directly
2. If ref looks like full SHA (40 hex chars) -> use as-is
3. Else ``git ls-remote <url> <ref>`` to resolve ref -> SHA

On every cache HIT:
- Run integrity check (verify HEAD == expected SHA)
- Mismatch -> evict shard, fall through to fresh fetch, log warning

Concurrency:
- Per-shard file locks (via filelock) for atomic operations
- Atomic landing protocol for safe concurrent installs
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
from pathlib import Path

from ..utils.path_security import ensure_path_within
from .integrity import verify_checkout_sha
from .locking import atomic_land, cleanup_incomplete, shard_lock, stage_path
from .paths import get_git_checkouts_path, get_git_db_path
from .url_normalize import cache_shard_key

_log = logging.getLogger(__name__)

# Full SHA pattern: 40 hex characters
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


class GitCache:
    """Content-addressable git cache with integrity verification.

    Args:
        cache_root: Root cache directory (from :func:`get_cache_root`).
        refresh: If True, force revalidation even on cache hit.
    """

    def __init__(self, cache_root: Path, *, refresh: bool = False) -> None:
        self._cache_root = cache_root
        self._refresh = refresh
        self._db_root = get_git_db_path(cache_root)
        self._checkouts_root = get_git_checkouts_path(cache_root)

        # Ensure bucket directories exist
        self._db_root.mkdir(parents=True, exist_ok=True)
        self._checkouts_root.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self._db_root), 0o700)
        os.chmod(str(self._checkouts_root), 0o700)

        # Clean up any stale incomplete operations from previous crashes
        cleanup_incomplete(self._db_root)
        cleanup_incomplete(self._checkouts_root)

    def get_checkout(
        self,
        url: str,
        ref: str | None,
        *,
        locked_sha: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Path:
        """Return path to a cached checkout for the given repo+ref.

        Args:
            url: Repository URL (any supported form).
            ref: Git ref (branch, tag, SHA) or None for default branch.
            locked_sha: If provided (from lockfile), skip resolution and
                use this SHA directly.
            env: Environment dict for git subprocesses.

        Returns:
            Path to the checkout directory (guaranteed to contain valid
            git working copy at the expected SHA).
        """
        shard_key = cache_shard_key(url)
        sha = self._resolve_sha(url, ref, locked_sha=locked_sha, env=env)

        checkout_dir = self._checkouts_root / shard_key / sha

        # Cache hit path (skip if refresh requested)
        if not self._refresh and checkout_dir.is_dir():
            if verify_checkout_sha(checkout_dir, sha):
                _log.debug("Cache HIT: %s @ %s", url, sha[:12])
                return checkout_dir
            else:
                # Integrity failure -- evict
                _log.warning(
                    "[!] Evicting corrupt cache entry: %s @ %s",
                    _sanitize_url(url),
                    sha[:12],
                )
                self._evict_checkout(checkout_dir)

        # Cache miss: ensure we have the bare repo, then create checkout
        self._ensure_bare_repo(url, shard_key, sha, env=env)
        return self._create_checkout(url, shard_key, sha, env=env)

    def _resolve_sha(
        self,
        url: str,
        ref: str | None,
        *,
        locked_sha: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Resolve a ref to a full SHA.

        Priority:
        1. locked_sha from lockfile (trusted, no network)
        2. ref already looks like a full SHA
        3. git ls-remote to resolve ref -> SHA
        """
        if locked_sha and _SHA_RE.match(locked_sha):
            return locked_sha.lower()

        if ref and _SHA_RE.match(ref):
            return ref.lower()

        # Need to resolve via ls-remote
        return self._ls_remote_resolve(url, ref, env=env)

    def _ls_remote_resolve(
        self,
        url: str,
        ref: str | None,
        *,
        env: dict[str, str] | None = None,
    ) -> str:
        """Resolve a ref to SHA via git ls-remote.

        Args:
            url: Repository URL.
            ref: Ref to resolve (branch, tag, or None for HEAD).
            env: Environment for subprocess.

        Returns:
            40-char lowercase hex SHA.

        Raises:
            RuntimeError: If resolution fails.
        """
        from ..utils.git_env import get_git_executable, git_subprocess_env

        git_exe = get_git_executable()
        cmd = [git_exe, "ls-remote", url]
        if ref:
            cmd.append(ref)

        subprocess_env = env if env is not None else git_subprocess_env()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=subprocess_env,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                f"Failed to resolve ref '{ref}' for {_sanitize_url(url)}: {exc}"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"git ls-remote failed for {_sanitize_url(url)}: {result.stderr.strip()}"
            )

        # Parse ls-remote output: first column is SHA
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) >= 1 and _SHA_RE.match(parts[0]):
                sha = parts[0].lower()
                # If no ref specified, return HEAD (first line)
                if not ref:
                    return sha
                # Match exact ref or refs/heads/ref or refs/tags/ref
                if len(parts) == 2:
                    remote_ref = parts[1]
                    if remote_ref in (
                        ref,
                        f"refs/heads/{ref}",
                        f"refs/tags/{ref}",
                    ):
                        return sha
        # If we have any SHA from output, use the first one
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) >= 1 and _SHA_RE.match(parts[0]):
                return parts[0].lower()

        raise RuntimeError(f"Could not resolve ref '{ref}' for {_sanitize_url(url)}")

    def _ensure_bare_repo(
        self,
        url: str,
        shard_key: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
    ) -> Path:
        """Ensure a bare repo clone exists for the given shard, fetching if needed.

        Returns the path to the bare repo directory.
        """
        from ..utils.git_env import get_git_executable, git_subprocess_env

        bare_dir = self._db_root / shard_key
        # Containment guard: defends against pathological shard_key
        # values bypassing the cache root.
        ensure_path_within(bare_dir, self._db_root)
        lock = shard_lock(bare_dir)

        # Acquire the shard lock BEFORE the existence probe so that two
        # concurrent processes hitting a cold shard cannot both perform
        # a full network clone (one would lose the atomic_land race
        # later, but only after wasting bandwidth + wall time).
        with lock:
            if bare_dir.is_dir():
                # Repo exists -- check if we have the required SHA
                if self._bare_has_sha(bare_dir, sha, env=env):
                    return bare_dir
                # Need to fetch the SHA (lock already held; call the
                # inner helper that does NOT re-acquire).
                self._fetch_into_bare_locked(bare_dir, url, sha, env=env)
                return bare_dir

            # Cold miss: clone bare repo
            git_exe = get_git_executable()
            staged = stage_path(bare_dir)
            ensure_path_within(staged, self._db_root)
            staged.mkdir(parents=True, exist_ok=True)
            os.chmod(str(staged), 0o700)

            subprocess_env = env if env is not None else git_subprocess_env()
            try:
                # Full bare clone (no --filter): we extract file contents at
                # checkout time, so all blobs must be present locally.  A
                # partial clone would leave the working tree empty after
                # `git clone --local --shared` + `git checkout`, because the
                # alternates pointer would resolve trees but not blobs.
                subprocess.run(
                    [git_exe, "clone", "--bare", url, str(staged)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=subprocess_env,
                    check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                # Clean up staged on failure
                from ..utils.file_ops import robust_rmtree

                robust_rmtree(staged, ignore_errors=True)
                raise RuntimeError(f"Failed to clone {_sanitize_url(url)}: {exc}") from exc

            # Atomic land (lock is already held; pass it through so the
            # rename completes under the same critical section).
            if not atomic_land(staged, bare_dir, lock):
                # Another process won between our staging and rename
                # (possible only on lock-acquisition timeout fallthrough);
                # verify it has our SHA.
                if not self._bare_has_sha(bare_dir, sha, env=env):
                    self._fetch_into_bare_locked(bare_dir, url, sha, env=env)

            return bare_dir

    def _create_checkout(
        self,
        url: str,
        shard_key: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
    ) -> Path:
        """Create a checkout at the specified SHA from the bare repo.

        Uses ``git clone --local --shared`` from the bare repo for
        efficiency (no network, hardlinks objects).

        Concurrency / write-deduplication
        ---------------------------------
        Acquires the shard lock BEFORE staging any work. On lock entry
        we re-probe the final shard and short-circuit if another
        process populated it while we were waiting on the lock.  This
        collapses N racing installs of the same SHA from N concurrent
        ``git clone`` operations to ~1: only the lock winner pays the
        clone cost; all losers see a populated shard the moment they
        get the lock and return immediately. Critical for CI matrix
        builds where multiple jobs hit the same uncached repo.
        """
        from ..utils.git_env import get_git_executable, git_subprocess_env

        bare_dir = self._db_root / shard_key
        checkout_parent = self._checkouts_root / shard_key
        # Containment guards: the shard_key + sha components are
        # derived from sha256 / hex but defend at the boundary anyway.
        ensure_path_within(checkout_parent, self._checkouts_root)
        checkout_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(checkout_parent), 0o700)

        final_dir = checkout_parent / sha
        ensure_path_within(final_dir, self._checkouts_root)
        lock = shard_lock(final_dir)

        # Acquire the lock BEFORE doing any work so that a concurrent
        # install of the same shard does not duplicate the clone work.
        # The lock winner clones; every other process re-probes after
        # the lock and short-circuits.
        with lock:
            # Write-dedup re-probe: another process may have populated
            # this shard while we were waiting. Verify integrity to
            # rule out a poisoned half-write (atomic_land guards
            # against that, but we re-check defensively).
            if final_dir.is_dir() and verify_checkout_sha(final_dir, sha):
                _log.debug("Write-dedup HIT under lock: %s @ %s", url, sha[:12])
                return final_dir

            staged = stage_path(final_dir)
            ensure_path_within(staged, self._checkouts_root)
            staged.mkdir(parents=True, exist_ok=True)
            os.chmod(str(staged), 0o700)

            git_exe = get_git_executable()
            subprocess_env = env if env is not None else git_subprocess_env()

            try:
                # Clone from local bare repo (fast, no network)
                subprocess.run(
                    [
                        git_exe,
                        "clone",
                        "--local",
                        "--shared",
                        "--no-checkout",
                        str(bare_dir),
                        str(staged),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=subprocess_env,
                    check=True,
                )
                # Checkout the specific SHA
                subprocess.run(
                    [git_exe, "-C", str(staged), "checkout", sha],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=subprocess_env,
                    check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                from ..utils.file_ops import robust_rmtree

                robust_rmtree(staged, ignore_errors=True)
                raise RuntimeError(
                    f"Failed to create checkout for {_sanitize_url(url)} @ {sha[:12]}: {exc}"
                ) from exc

            # We hold the shard lock, so atomic_land's re-acquire is a
            # reentrant no-op (filelock supports same-process recursion).
            if not atomic_land(staged, final_dir, lock):
                # Another process landed first between our re-probe and
                # the rename (only possible if our lock dropped, which
                # it didn't); verify integrity defensively.
                if not verify_checkout_sha(final_dir, sha):
                    self._evict_checkout(final_dir)
                    raise RuntimeError(
                        f"Race condition: concurrent checkout failed integrity "
                        f"for {_sanitize_url(url)} @ {sha[:12]}"
                    )
            return final_dir

    def _bare_has_sha(self, bare_dir: Path, sha: str, *, env: dict[str, str] | None = None) -> bool:
        """Check if the bare repo contains the specified commit."""
        from ..utils.git_env import get_git_executable, git_subprocess_env

        git_exe = get_git_executable()
        subprocess_env = env if env is not None else git_subprocess_env()
        try:
            result = subprocess.run(
                [git_exe, "-C", str(bare_dir), "cat-file", "-t", sha],
                capture_output=True,
                text=True,
                timeout=10,
                env=subprocess_env,
            )
            return result.returncode == 0 and "commit" in result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _fetch_into_bare(
        self,
        bare_dir: Path,
        url: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Fetch a specific SHA into an existing bare repo (acquires lock)."""
        lock = shard_lock(bare_dir)
        with lock:
            if self._bare_has_sha(bare_dir, sha, env=env):
                return
            self._fetch_into_bare_locked(bare_dir, url, sha, env=env)

    def _fetch_into_bare_locked(
        self,
        bare_dir: Path,
        url: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Fetch a specific SHA into a bare repo. Caller MUST hold the shard lock."""
        from ..utils.git_env import get_git_executable, git_subprocess_env

        git_exe = get_git_executable()
        subprocess_env = env if env is not None else git_subprocess_env()
        try:
            subprocess.run(
                [git_exe, "-C", str(bare_dir), "fetch", url, sha],
                capture_output=True,
                text=True,
                timeout=120,
                env=subprocess_env,
                check=True,
            )
        except subprocess.CalledProcessError:
            # Some servers don't allow fetching by SHA -- fetch all refs
            subprocess.run(
                [git_exe, "-C", str(bare_dir), "fetch", "--all"],
                capture_output=True,
                text=True,
                timeout=120,
                env=subprocess_env,
                check=True,
            )

    def _evict_checkout(self, checkout_dir: Path) -> None:
        """Safely remove a corrupt checkout shard."""
        from ..utils.file_ops import robust_rmtree

        try:
            robust_rmtree(checkout_dir, ignore_errors=True)
        except Exception as exc:
            _log.debug("Failed to evict checkout %s: %s", checkout_dir, exc)

    def get_cache_stats(self) -> dict[str, int]:
        """Return cache statistics for ``apm cache info``.

        Returns:
            Dict with keys: db_count, checkout_count, total_size_bytes.
        """
        db_count = 0
        checkout_count = 0
        total_size = 0

        if self._db_root.is_dir():
            for entry in os.scandir(str(self._db_root)):
                if entry.is_dir(follow_symlinks=False) and not entry.name.endswith(".lock"):
                    db_count += 1
                    total_size += _dir_size(Path(entry.path))

        if self._checkouts_root.is_dir():
            for shard_entry in os.scandir(str(self._checkouts_root)):
                if shard_entry.is_dir(follow_symlinks=False):
                    for sha_entry in os.scandir(shard_entry.path):
                        if sha_entry.is_dir(follow_symlinks=False):
                            checkout_count += 1
                            total_size += _dir_size(Path(sha_entry.path))

        return {
            "db_count": db_count,
            "checkout_count": checkout_count,
            "total_size_bytes": total_size,
        }

    def clean_all(self) -> None:
        """Remove ALL cache content (db + checkouts). Used by ``apm cache clean``."""
        from ..utils.file_ops import robust_rmtree

        for bucket in (self._db_root, self._checkouts_root):
            if bucket.is_dir():
                for entry in os.scandir(str(bucket)):
                    if entry.is_dir(follow_symlinks=False):
                        robust_rmtree(Path(entry.path), ignore_errors=True)
                    elif entry.is_file(follow_symlinks=False):
                        with contextlib.suppress(OSError):
                            os.unlink(entry.path)

    def prune(self, *, max_age_days: int = 30) -> int:
        """Remove checkout entries older than *max_age_days*.

        Uses mtime of the checkout directory as the access indicator.

        Returns:
            Number of entries pruned.
        """
        import time

        from ..utils.file_ops import robust_rmtree

        cutoff = time.time() - (max_age_days * 86400)
        pruned = 0

        if not self._checkouts_root.is_dir():
            return 0

        for shard_entry in os.scandir(str(self._checkouts_root)):
            if not shard_entry.is_dir(follow_symlinks=False):
                continue
            for sha_entry in os.scandir(shard_entry.path):
                if not sha_entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    stat = sha_entry.stat(follow_symlinks=False)
                    if stat.st_mtime < cutoff:
                        robust_rmtree(Path(sha_entry.path), ignore_errors=True)
                        pruned += 1
                except OSError:
                    continue

        return pruned


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory (non-recursive symlink-safe)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(str(path)):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.lstat(fp)
                    total += st.st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _sanitize_url(url: str) -> str:
    """Strip credentials from URL for safe logging."""
    import urllib.parse

    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.password:
            # Replace password with ***
            netloc = parsed.hostname or ""
            if parsed.username:
                netloc = f"{parsed.username}:***@{netloc}"
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return url
