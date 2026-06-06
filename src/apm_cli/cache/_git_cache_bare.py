"""Bare-repo lifecycle mixin for :class:`~apm_cli.cache.git_cache.GitCache`.

Extracted to keep ``git_cache.py`` under the 800-line threshold while
preserving 100% behavioural equivalence.  This module is private
(``_`` prefix) and must NOT be imported directly by callers outside the
``cache`` package; the public surface lives in ``git_cache.GitCache``.

Rule B routing
--------------
Unit tests patch these names at the ``git_cache`` *module* level:
``shard_lock`` (37x), ``os`` (26x), ``atomic_land`` (19x).
Every method in this mixin that references those names resolves them
via a late import of the origin module::

    from apm_cli.cache import git_cache as _gc
    _gc.shard_lock(...)   # routes to the (possibly patched) module attr
    _gc.os.chmod(...)
    _gc.atomic_land(...)

The private helpers ``_safe_git_args``, ``_sanitize_url``, and the
constant ``_PARTIAL_BARE_SUFFIX`` remain in ``git_cache.py`` and are
similarly accessed via ``_gc.*`` so that any test-side patches on those
names also take effect here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class _GitCacheBareMixin:
    """Mixin providing bare-repo clone/fetch lifecycle for GitCache.

    Requires the host class to expose:
        ``self._db_root``  -- Path to the git bare-repo database root.
    """

    def _ensure_bare_repo(
        self,
        url: str,
        shard_key: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
        partial: bool = False,
    ) -> Path:
        """Ensure a bare repo clone exists for the given shard, fetching if needed.

        Args:
            partial: If True, clone with ``--filter=blob:none`` into a
                separate ``<shard>__p`` directory so the bare downloads
                commits + trees only (~5% of full repo size) and acts
                as a promisor remote for consumer lazy-fetch. Falls
                back to a full clone in the same directory if the
                server rejects the filter (older Gerrit / pre-2.20
                GHE). Falling back leaves the partial-flavor dir with
                full content; future sparse consumers will simply not
                trigger any lazy fetch (all blobs already present), so
                behavior degrades to today's baseline.

        Returns the path to the bare repo directory.
        """
        # Late imports: Rule B for shard_lock / os / atomic_land +
        # private helpers that stay in git_cache.py.
        from apm_cli.cache import git_cache as _gc
        from apm_cli.utils.path_security import ensure_path_within

        bare_shard = shard_key + (_gc._PARTIAL_BARE_SUFFIX if partial else "")
        bare_dir = self._db_root / bare_shard
        # Containment guard: defends against pathological shard_key
        # values bypassing the cache root.
        ensure_path_within(bare_dir, self._db_root)
        lock = _gc.shard_lock(bare_dir)

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
            from apm_cli.cache.locking import stage_path
            from apm_cli.utils.git_env import get_git_executable, git_subprocess_env

            git_exe = get_git_executable()
            staged = stage_path(bare_dir)
            ensure_path_within(staged, self._db_root)
            staged.mkdir(parents=True, exist_ok=True)
            _gc.os.chmod(str(staged), 0o700)

            subprocess_env = env if env is not None else git_subprocess_env()
            clone_args = [
                git_exe,
                *_gc._safe_git_args(),
                "clone",
                "--bare",
                "--no-tags",
                "--no-recurse-submodules",
            ]
            if partial:
                # Promisor partial clone: trees + commits only. Blobs
                # arrive lazily via the remote when the consumer needs
                # them. Github / modern GHES / ADO support this; older
                # servers reject it and we retry without --filter.
                # --no-tags above skips fetching tag objects (release
                # tags can sum to MBs on monorepos); the cache is
                # SHA-keyed and never resolves via tags.
                clone_args += ["--filter=blob:none"]
            clone_args += [url, str(staged)]
            try:
                # Full bare clone (or partial when requested above). The
                # full path extracts file contents at checkout time, so
                # all blobs must be present locally. The partial path
                # relies on the consumer being configured as a promisor
                # so missing blobs trigger an on-demand fetch.
                subprocess.run(
                    clone_args,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=subprocess_env,
                    check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                # Partial clone fallback: some servers reject --filter
                # (old Gerrit / pre-2.20 GHE). Retry once without it so
                # we never block on this optimization. The resulting
                # bare is full; future sparse consumers find all blobs
                # locally and skip lazy fetch (degrades to baseline,
                # no behavior change for the user).
                fallback_done = False
                if partial and isinstance(exc, subprocess.CalledProcessError):
                    from ..utils.console import _rich_warning

                    _rich_warning(
                        f"Partial clone (--filter=blob:none) failed for "
                        f"{_gc._sanitize_url(url)}; retrying with full bare clone. "
                        f"Server may not support filter v2."
                    )
                    from ..utils.file_ops import robust_rmtree

                    robust_rmtree(staged, ignore_errors=True)
                    staged.mkdir(parents=True, exist_ok=True)
                    _gc.os.chmod(str(staged), 0o700)
                    try:
                        subprocess.run(
                            [
                                git_exe,
                                *_gc._safe_git_args(),
                                "clone",
                                "--bare",
                                "--no-tags",
                                "--no-recurse-submodules",
                                url,
                                str(staged),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=300,
                            env=subprocess_env,
                            check=True,
                        )
                        fallback_done = True
                    except (
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                        OSError,
                    ) as exc2:
                        from ..utils.file_ops import robust_rmtree

                        robust_rmtree(staged, ignore_errors=True)
                        raise RuntimeError(
                            f"Failed to clone {_gc._sanitize_url(url)} "
                            f"(partial fallback also failed): {exc2}"
                        ) from exc2
                if not fallback_done:
                    # Clean up staged on failure
                    from ..utils.file_ops import robust_rmtree

                    robust_rmtree(staged, ignore_errors=True)
                    raise RuntimeError(f"Failed to clone {_gc._sanitize_url(url)}: {exc}") from exc

            # Atomic land (lock is already held; pass it through so the
            # rename completes under the same critical section).
            if not _gc.atomic_land(staged, bare_dir, lock):
                # Another process won between our staging and rename
                # (possible only on lock-acquisition timeout fallthrough);
                # verify it has our SHA.
                if not self._bare_has_sha(bare_dir, sha, env=env):
                    self._fetch_into_bare_locked(bare_dir, url, sha, env=env)

            return bare_dir

    def _bare_has_sha(self, bare_dir: Path, sha: str, *, env: dict[str, str] | None = None) -> bool:
        """Check if the bare repo contains the specified commit."""
        from apm_cli.cache import git_cache as _gc
        from apm_cli.utils.git_env import get_git_executable, git_subprocess_env

        git_exe = get_git_executable()
        subprocess_env = env if env is not None else git_subprocess_env()
        try:
            result = subprocess.run(
                [git_exe, *_gc._safe_git_args(), "-C", str(bare_dir), "cat-file", "-t", sha],
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
        from apm_cli.cache import git_cache as _gc

        lock = _gc.shard_lock(bare_dir)
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
        from apm_cli.cache import git_cache as _gc
        from apm_cli.utils.git_env import get_git_executable, git_subprocess_env

        git_exe = get_git_executable()
        subprocess_env = env if env is not None else git_subprocess_env()
        # If this is a partial-flavor bare, preserve the filter on fetch
        # so we don't pull all blobs reachable from the new SHA. Detected
        # via shard-suffix naming convention (cheap, no git config probe).
        is_partial = bare_dir.name.endswith(_gc._PARTIAL_BARE_SUFFIX)
        fetch_args = [git_exe, *_gc._safe_git_args(), "-C", str(bare_dir), "fetch"]
        if is_partial:
            fetch_args += ["--filter=blob:none"]
        fetch_args += [url, sha]
        try:
            subprocess.run(
                fetch_args,
                capture_output=True,
                text=True,
                timeout=120,
                env=subprocess_env,
                check=True,
            )
        except subprocess.CalledProcessError:
            # Some servers don't allow fetching by SHA -- fetch all refs
            subprocess.run(
                [git_exe, *_gc._safe_git_args(), "-C", str(bare_dir), "fetch", "--all"],
                capture_output=True,
                text=True,
                timeout=120,
                env=subprocess_env,
                check=True,
            )
