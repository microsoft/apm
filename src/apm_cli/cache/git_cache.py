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
import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path

from ..utils.git_sparse import apply_sparse_cone
from ..utils.path_security import ensure_path_within
from ._git_cache_bare import _GitCacheBareMixin
from .integrity import verify_checkout_sha
from .locking import atomic_land, cleanup_incomplete, shard_lock, stage_path
from .paths import get_git_checkouts_path, get_git_db_path
from .url_normalize import cache_shard_key

_log = logging.getLogger(__name__)

# Full SHA pattern: 40 hex characters
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _safe_git_args() -> list[str]:
    """Return hardening ``-c`` args prepended to every git subprocess.

    - ``core.hooksPath=/dev/null`` disables any hook script that a
      malicious upstream might ship (``.git/hooks/post-checkout`` etc.)
      so a clone or checkout cannot trigger arbitrary code execution.
    - ``submodule.recurse=false`` prevents any subcommand from
      recursing into attacker-controlled submodule URLs.

    These flags are scoped per-invocation via ``-c`` and never mutate
    the user's gitconfig. The cache layer is the single source of
    truth for git subprocess invocation -- callers must use this
    helper rather than ad-hoc ``git`` argv construction.
    """
    from ..utils.git_env import git_long_paths_args

    return [
        *git_long_paths_args(),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "submodule.recurse=false",
    ]


# Partial bare-cache flavor suffix (perf #1433 follow-up).
# When a caller requests sparse_paths, we use a separate bare keyed at
# ``<shard>__p`` cloned with ``--filter=blob:none``. The partial bare
# downloads commits + trees only (~5% of repo size) and acts as a
# promisor remote; blobs are lazy-fetched at consumer checkout time
# scoped to the sparse cone. Full and partial bares coexist per URL
# so legacy full-tree callers keep today's behavior unchanged.
_PARTIAL_BARE_SUFFIX = "__p"


def _variant_key(sparse_paths: list[str] | None) -> str:
    """Return the on-disk variant segment for a checkout shard.

    Layout (perf #1433):
      - ``full`` -- full-tree checkout (sparse_paths is None / empty).
      - ``sparse-<hash16>`` -- sparse-cone checkout where ``<hash16>`` is
        the first 16 hex chars of sha256(json.dumps(sorted(paths))).
        Two consumers requesting the same set of paths share a shard;
        different sets get separate shards. We do NOT promote a full
        checkout to also satisfy a sparse subset -- that complicates
        eviction for negligible benefit (each sparse shard is ~subdir
        size, so duplication cost is small).
    """
    if not sparse_paths:
        return "full"
    # Deduplicate AND sort so callers passing [a,a] or [a,b]+[b,a]
    # all collapse to the same variant key (the "set of paths"
    # semantics the docstring promises).
    payload = json.dumps(sorted(set(sparse_paths)), separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"sparse-{digest}"


class GitCache(_GitCacheBareMixin):
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
        sparse_paths: list[str] | None = None,
    ) -> Path:
        """Return path to a cached checkout for the given repo+ref.

        Args:
            url: Repository URL (any supported form).
            ref: Git ref (branch, tag, SHA) or None for default branch.
            locked_sha: If provided (from lockfile), skip resolution and
                use this SHA directly.
            env: Environment dict for git subprocesses.
            sparse_paths: If non-empty, materialize only these top-level
                directories using ``git sparse-checkout --cone``. The
                shard is keyed by ``(sha, sparse_paths_variant)`` so
                full and sparse variants of the same SHA coexist.

        Returns:
            Path to the checkout directory (guaranteed to contain valid
            git working copy at the expected SHA).
        """
        shard_key = cache_shard_key(url)
        sha = self._resolve_sha(url, ref, locked_sha=locked_sha, env=env)
        variant = _variant_key(sparse_paths)

        checkout_dir = self._checkouts_root / shard_key / sha / variant

        # Cache hit path (skip if refresh requested)
        if not self._refresh and checkout_dir.is_dir():
            if verify_checkout_sha(checkout_dir, sha):
                _log.debug("Cache HIT: %s @ %s [%s]", url, sha[:12], variant)
                return checkout_dir
            else:
                # Integrity failure -- evict
                _log.warning(
                    "[!] Evicting corrupt cache entry: %s @ %s [%s]",
                    _sanitize_url(url),
                    sha[:12],
                    variant,
                )
                self._evict_checkout(checkout_dir)

        # Cache miss: ensure we have the bare repo, then create checkout.
        # Sparse callers use a partial bare (blob:none) + promisor consumer
        # so only the trees + the blobs reachable from the sparse cone are
        # downloaded. Full-tree callers keep the legacy non-partial bare.
        use_partial = bool(sparse_paths)
        self._ensure_bare_repo(url, shard_key, sha, env=env, partial=use_partial)
        return self._create_checkout(
            url,
            shard_key,
            sha,
            env=env,
            sparse_paths=sparse_paths,
            promisor_url=url if use_partial else None,
        )

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
        # auth-delegated: cache-layer ref resolution runs after lockfile
        # already pinned the commit; no PAT->bearer fallback applies here
        # (env is sanitized, no embedded creds).
        cmd = [git_exe, *_safe_git_args(), "ls-remote", url]
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

    def _create_checkout(
        self,
        url: str,
        shard_key: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
        sparse_paths: list[str] | None = None,
        promisor_url: str | None = None,
    ) -> Path:
        """Create a checkout at the specified SHA from the bare repo.

        Uses ``git clone --local --shared`` from the bare repo for
        efficiency (no network, hardlinks objects).

        Sparse-cone (perf #1433):
            When ``sparse_paths`` is non-empty, ``git sparse-checkout
            init --cone`` + ``set <paths...>`` runs BEFORE the SHA
            checkout, so the working tree contains only the requested
            top-level directories. The shard lives at
            ``checkouts_v1/<shard>/<sha>/sparse-<hash>/`` so it
            coexists with a possible full-tree shard at
            ``.../<sha>/full/`` for the same SHA.

        Partial-clone promisor (perf #1433 follow-up):
            When ``promisor_url`` is set, the bare lives at
            ``<shard>__p`` (cloned with ``--filter=blob:none``) and
            we configure the consumer's ``remote.origin`` to point
            at the real upstream URL with ``promisor=true`` and
            ``partialclonefilter=blob:none``. Sparse checkout then
            lazy-fetches only the blobs reachable from the cone
            (typically <2 MB instead of the full repo's blob set).

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

        bare_shard = shard_key + (_PARTIAL_BARE_SUFFIX if promisor_url else "")
        bare_dir = self._db_root / bare_shard
        variant = _variant_key(sparse_paths)
        # New layout: <shard>/<sha>/<variant>/. The <sha> level is the
        # SHA dir (parent to the variant). The <variant> level is what
        # the lock + atomic_land target so different variants of the
        # same SHA do not race each other.
        sha_parent = self._checkouts_root / shard_key / sha
        ensure_path_within(sha_parent, self._checkouts_root)
        sha_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(sha_parent), 0o700)

        final_dir = sha_parent / variant
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
                _log.debug(
                    "Write-dedup HIT under lock: %s @ %s [%s]",
                    url,
                    sha[:12],
                    variant,
                )
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
                        *_safe_git_args(),
                        "clone",
                        "--local",
                        "--shared",
                        "--no-checkout",
                        "--no-recurse-submodules",
                        str(bare_dir),
                        str(staged),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=subprocess_env,
                    check=True,
                )
                if promisor_url:
                    # Configure consumer as a promisor pointing at the
                    # real upstream URL so missing blobs (the partial
                    # bare only carries trees) are lazy-fetched during
                    # checkout. Without this, ``git checkout`` would
                    # fail with "fatal: unable to read tree/blob" for
                    # any object missing from the local alternates.
                    # The fetch goes to ``promisor_url`` directly; auth
                    # comes from the inherited subprocess_env.
                    for cfg_args in (
                        ["remote.origin.url", promisor_url],
                        ["remote.origin.promisor", "true"],
                        ["remote.origin.partialclonefilter", "blob:none"],
                    ):
                        subprocess.run(
                            [
                                git_exe,
                                *_safe_git_args(),
                                "-C",
                                str(staged),
                                "config",
                                *cfg_args,
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            env=subprocess_env,
                            check=True,
                        )
                if sparse_paths:
                    # Sparse-cone setup BEFORE checkout. Failures raise
                    # (not silently fallen back to full checkout) because
                    # a silent fallback would re-introduce the disk
                    # bloat this code path exists to avoid (#1433).
                    apply_sparse_cone(
                        git_exe,
                        staged,
                        list(sparse_paths),
                        env=subprocess_env,
                        extra_git_args=_safe_git_args(),
                    )
                # Checkout the specific SHA
                subprocess.run(
                    [
                        git_exe,
                        *_safe_git_args(),
                        "-C",
                        str(staged),
                        "checkout",
                        sha,
                    ],
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
