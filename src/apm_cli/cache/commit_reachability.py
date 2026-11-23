"""A pinned commit is admitted only when an origin branch or tag reaches it.

A commit SHA (a ``#sha`` dependency ref or a lockfile ``locked_sha``) must be
reachable from one of the origin's branches or tags. A commit that only a
pull request, merge request or fork advertises is refused.

The check runs once per origin and commit. Fast path: the commit is the tip
of an origin branch or tag (one ``git ls-remote``). Otherwise a commits-only
fetch of every origin head and tag into a per-origin reachability repository
answers it: that repository holds only objects those refs reach. A verified
commit is recorded under the cache root, keyed by the origin's cache shard
(its canonical URL) and the commit, so a re-install works offline and a
record for one origin never admits the same commit from another.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

_REACH_REFSPECS = (
    "+refs/heads/*:refs/heads/*",
    "+refs/tags/*:refs/tags/*",
)


def _marker(cache_root: Path, shard_key: str, sha: str) -> Path:
    return Path(cache_root) / "reachability" / shard_key / "verified" / sha


def _record(marker: Path, canonical: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(canonical, encoding="utf-8")


def _recorded(marker: Path, canonical: str) -> bool:
    """Read a marker without following links; only a regular file counts."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(marker, flags)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1
            return handle.read() == canonical
    except (OSError, UnicodeDecodeError):
        return False
    finally:
        if fd >= 0:
            os.close(fd)


class CommitNotReachableError(RuntimeError):
    """A pinned commit is not admitted: no origin branch or tag reaches it, or that is unverified.

    Never a cache miss: callers must not fall back to another fetch path.
    """


class _Admission:
    """A ``GitCache`` stand-in whose checkout only admits the pinned commit."""

    def __init__(self, cache_root: Path) -> None:
        self._root = cache_root

    def get_checkout(self, url, ref, *, locked_sha=None, env=None, sparse_paths=None) -> Path:
        from .url_normalize import cache_shard_key

        require_reachable_commit(self._root, url, cache_shard_key(url), ref, env=env)
        return self._root

    def remember_resolved_ref(self, *_args) -> None:
        return None


def admit_pinned_commit(downloader, dep_ref, sha: str | None) -> None:
    """Admit a commit-pinned dependency before any cache, bare or clone fetch.

    Runs under the downloader's own persistent-cache credential fallback,
    so the verdict uses the credentials the fetch would. A non-commit ref
    (branch or tag) is not checked here.

    Raises:
        CommitNotReachableError: The commit is not admitted.
    """
    if not sha or not _COMMIT_RE.match(sha):
        return
    from .paths import get_cache_root

    cache = getattr(downloader, "persistent_git_cache", None)
    root = Path(getattr(cache, "_cache_root", None) or get_cache_root())
    downloader._persistent_cache_checkout(
        _Admission(root), dep_ref, dep_ref.to_github_url(), sha, locked_sha=None
    )


def require_reachable_commit(
    cache_root: Path,
    url: str,
    shard_key: str,
    sha: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Refuse *sha* unless an origin branch or tag reaches it.

    Raises:
        RuntimeError: The commit is not reachable from any origin branch or
            tag, or reachability cannot be verified (for example offline on
            the first install of this commit).
    """
    from ..deps.git_remote_ops import tag_commit_shas
    from ..utils.git_env import git_remote_refs
    from .git_cache import _safe_git_args, _sanitize_url
    from .url_normalize import normalize_repo_url

    sha = sha.lower()
    canonical = normalize_repo_url(url)
    marker = _marker(cache_root, shard_key, sha)
    if _recorded(marker, canonical):
        return
    where = _sanitize_url(url)
    unverifiable = (
        f"Cannot verify that commit {sha} of {where} is reachable from a branch or tag: "
        "the first install of a pinned commit needs network access once. "
        "Re-run with network access, or pin a branch or tag instead."
    )
    try:
        listing = git_remote_refs(
            url, "refs/heads/*", "refs/tags/*", env=env, timeout=60, git_args=_safe_git_args()
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise CommitNotReachableError(unverifiable) from exc
    if listing.returncode != 0:
        raise CommitNotReachableError(unverifiable)
    records = []
    for line in (listing.stdout or "").splitlines():
        tip, _, name = line.partition("\t")
        records.append((tip.strip().lower(), name.strip()))
    tips = {tip for tip, name in records if name.startswith("refs/heads/")}
    tips.update(tag_commit_shas(records)[0].values())
    if sha in tips or (records and _reached_by_refs(cache_root, url, shard_key, sha, env)):
        _record(marker, canonical)
        return
    raise CommitNotReachableError(
        f"Commit {sha} of {where} is not reachable from any branch or tag of the origin. "
        "Pin a commit on a branch or tag of the source repository, or the branch or tag itself."
    )


def _reached_by_refs(
    cache_root: Path, url: str, shard_key: str, sha: str, env: dict[str, str] | None
) -> bool:
    """Fetch the origin's heads and tags (commits only) and ask whether *sha* is among them.

    Raises:
        RuntimeError: The fetch failed.
    """
    from ..utils.git_env import get_git_executable, git_network_env, run_git_text
    from .git_cache import _safe_git_args, _sanitize_url

    repo = Path(cache_root) / "reachability" / shard_key / "refs.git"
    git = [get_git_executable(), *_safe_git_args(), "--git-dir", str(repo)]
    try:
        fresh = not repo.is_dir()
        if fresh:
            repo.mkdir(parents=True)
            os.chmod(repo, 0o700)
        child_env = git_network_env(url, env, git_dir=repo)
        if fresh:
            run_git_text(
                [*git, "init", "-q", "--bare"],
                capture_output=True,
                env=child_env,
                stdin=subprocess.DEVNULL,
                check=True,
            )
        run_git_text(
            [*git, "fetch", "-q", "--filter=tree:0", "--prune", url, *_REACH_REFSPECS],
            capture_output=True,
            timeout=600,
            env=child_env,
            stdin=subprocess.DEVNULL,
            check=True,
        )
        _drop_promisor(git, child_env)
        # Only objects the heads and tags reach may answer: never lazy-fetch.
        probe = run_git_text(
            [*git, "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            env={**child_env, "GIT_NO_LAZY_FETCH": "1"},
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise CommitNotReachableError(
            f"Cannot verify that commit {sha} of {_sanitize_url(url)} is reachable from a "
            "branch or tag: the first install of a pinned commit needs network access once. "
            "Re-run with network access, or pin a branch or tag instead."
        ) from exc
    return probe.returncode == 0


def _drop_promisor(git: list[str], env: dict[str, str]) -> None:
    """Forget the promisor remote a filtered fetch records, so nothing lazy-fetches."""
    from ..utils.git_env import run_git_text

    listed = run_git_text(
        [*git, "config", "--get-regexp", r"^remote\..*\.(promisor|partialclonefilter)$"],
        capture_output=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    for line in (listed.stdout or "").splitlines():
        key = line.split(" ", 1)[0]
        run_git_text(
            [*git, "config", "--unset-all", key],
            capture_output=True,
            env=env,
            stdin=subprocess.DEVNULL,
            check=True,
        )
