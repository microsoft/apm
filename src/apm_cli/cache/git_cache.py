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
- Sparse (promisor) shards with tracked files missing from the worktree
  are rebuilt; a fresh promisor checkout that leaves files missing raises
  ``IncompleteCheckoutError`` so the auth fallback can retry (#3071)
- Refresh the SHA directory's access timestamp after successful validation

Concurrency:
- Per-shard file locks (via filelock) for atomic operations
- Atomic landing protocol for safe concurrent installs
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from ..utils.atomic_io import atomic_write_text
from ..utils.git_env import run_git_text
from ..utils.git_sparse import apply_sparse_cone, materialize_tracked_symlinks
from ..utils.path_security import ensure_path_within
from .cached_repo import cached_repo_env, cached_repo_is_trusted
from .integrity import verify_checkout_sha
from .locking import atomic_land, cleanup_incomplete, shard_lock, stage_path
from .paths import get_git_checkouts_path, get_git_db_path
from .url_normalize import cache_shard_key

_log = logging.getLogger(__name__)

# Full SHA pattern: 40 hex characters
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_FALLBACK_REFSPECS = (
    "+refs/heads/*:refs/remotes/apm-fallback/*",
    "+refs/tags/*:refs/tags/*",
)


class CachePruneError(OSError):
    """Incomplete prune with completed-removal count and per-entry failures."""

    def __init__(self, pruned: int, failures: list[tuple[Path, OSError]]) -> None:
        self.pruned = pruned
        self.failures = tuple(failures)
        super().__init__(f"Pruned {pruned} SHA group(s); {len(failures)} failed.")


class CachedCheckoutTamperedError(RuntimeError):
    """A read-only cache holds a checkout that does not match its commit."""


class IncompleteCheckoutError(RuntimeError):
    """A checkout exited 0 but left tracked files unmaterialized (#3071).

    Git before 2.36 ignores a failed promisor blob fetch during a non-forced
    checkout. ``stderr`` keeps Git's diagnostics so the anonymous-first auth
    fallback can classify the rejection and retry with credentials.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


_HEX_DIGITS = frozenset("0123456789abcdef")


def _oid(value: str, width: int) -> str | None:
    return value if len(value) == width and set(value) <= _HEX_DIGITS else None


def _packed_branch_tips(text: str, width: int) -> dict[str, str] | None:
    """Branch entries of a packed-refs text, or None unless every line is well formed."""
    tips: dict[str, str] = {}
    previous: str | None = None
    if text and not text.endswith("\n"):
        return None  # an unterminated last line
    for number, line in enumerate(text.split("\n")[:-1]):
        if number == 0 and line.startswith("# pack-refs with:"):
            continue
        if line.startswith("^"):
            # A peel line follows one tag entry; a branch that peels names no commit.
            if previous is None or previous.startswith("refs/heads/") or not _oid(line[1:], width):
                return None
            previous = None
            continue
        oid, sep, name = line.partition(" ")
        if not sep or _oid(oid, width) is None or not name.startswith("refs/") or " " in name:
            return None
        previous = name
        if name.startswith("refs/heads/"):
            tips[name] = oid
    return tips


def _branch_tips(git_dir: Path, width: int) -> set[str] | None:
    """Branch tips of *git_dir* as git resolves them (loose over packed), or None if unusual."""
    if (git_dir / "reftable").exists():
        return None
    tips: dict[str, str] = {}
    packed = git_dir / "packed-refs"
    if packed.exists():
        found = _packed_branch_tips(packed.read_text(encoding="ascii"), width)
        if found is None:
            return None
        tips.update(found)
    heads = git_dir / "refs" / "heads"
    for ref in heads.rglob("*") if heads.is_dir() else ():
        if ref.is_symlink() or ref.name.endswith(".lock"):
            return None
        if ref.is_dir():
            continue
        name = "/".join(("refs", "heads", *ref.relative_to(heads).parts))
        text = ref.read_text(encoding="ascii")
        if not text.endswith("\n") or text.count("\n") != 1:
            return None
        if text.startswith("ref: "):
            tips.pop(name, None)  # names no commit itself; a branch target is read on its own
            continue
        oid = _oid(text[:-1], width)
        if oid is None:
            return None
        tips[name] = oid
    return set(tips.values())


def _is_ref_tip(git_dir: Path, sha: str) -> bool:
    """Whether a branch of the fresh clone *git_dir* points at *sha* (read, not run).

    Only a positive answer is used (a branch tip is a commit the clone holds).
    Refs resolve as git resolves them: a loose ref wins over its packed entry,
    and tags, peel lines and symbolic refs name nothing. Anything unreadable,
    malformed or unusual answers False, and the caller asks git.
    """
    sha = sha.lower()
    if _oid(sha, len(sha)) is None or len(sha) not in (40, 64):
        return False
    try:
        tips = _branch_tips(git_dir, len(sha))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return tips is not None and sha in tips


def _safe_git_args() -> list[str]:
    """Return hardening ``-c`` args prepended to every git subprocess.

    - The canonical no-hooks arguments disable any hook script that a
      malicious upstream might ship, so clone and checkout stay inert.
    - ``submodule.recurse=false`` prevents any subcommand from
      recursing into attacker-controlled submodule URLs.
    - ``git_line_ending_pin_args`` disables host autocrlf conversion
      and sets ``core.eol`` to ``lf`` so ``text`` / ``text=auto`` paths are
      written with the same bytes on every host (apm#2971, apm#3270).
      ``-c`` outranks host system / global config and
      ``GIT_CONFIG_KEY_n`` snapshots from ``git_network_env``, which
      otherwise win over a repo-local pin. Explicit ``.gitattributes``
      ``eol=`` requests and committed CRLF blobs are still honored.

    - ``git_symlink_pin_args`` writes in-repo links as link text on every
      host, so checkout never needs OS symlink capability; the shard then
      replaces them with their Git-object content (#3457).

    These flags are scoped per-invocation via ``-c`` and never mutate
    the user's gitconfig. The cache layer is the single source of
    truth for git subprocess invocation -- callers must use this
    helper rather than ad-hoc ``git`` argv construction.
    """
    from ..utils.git_env import (
        git_line_ending_pin_args,
        git_long_paths_args,
        git_no_hooks_args,
        git_symlink_pin_args,
    )
    from .cached_repo import inert_git_args

    return [
        *git_long_paths_args(),
        *git_no_hooks_args(),
        *inert_git_args(),
        "-c",
        "submodule.recurse=false",
        *git_line_ending_pin_args(),
        *git_symlink_pin_args(),
    ]


# Shards whose tracked links hold their Git-object content carry this local
# key. Older shards hold OS symlinks or Git link text instead (#3457), and
# ``git-objects`` shards may hold out-of-cone targets read without their
# ``.gitattributes`` (e.g. LF for ``eol=crlf``), so the value is versioned.
_LINK_MATERIALIZATION_PIN = ("apm.linkMaterialization", "git-objects-checkout")

# Sparse (promisor) shards verified to hold every tracked file carry this
# local key, so cache hits skip the ``ls-files`` probe (#3071).
_SPARSE_COMPLETENESS_PIN = ("apm.sparseMaterialization", "complete")


def _checkout_config_values(checkout_dir: Path) -> dict[str, str] | None:
    """Return the first ``section.key`` values in a checkout's local gitconfig."""
    config = checkout_dir / ".git" / "config"
    if not config.is_file():
        return None
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    values: dict[str, str] = {}
    section = None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            inner = line[1:-1].strip()
            section = inner.split(" ", 1)[0].strip().lower()
            continue
        if section is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(f"{section}.{key.strip().lower()}", value.strip().strip("\"'").lower())
    return values


def _checkout_materialization_pins() -> tuple[tuple[str, str], ...]:
    """Return every byte-determinism ``(key, value)`` a cached shard must record."""
    from ..utils.git_env import git_line_ending_pin_entries, git_symlink_pin_entry

    return (*git_line_ending_pin_entries(), git_symlink_pin_entry(), _LINK_MATERIALIZATION_PIN)


def _checkout_has_materialization_pins(checkout_dir: Path) -> bool:
    """Return whether a shard was materialized with every byte-determinism pin.

    Shards written before a pin existed may hold host-dependent bytes: CRLF
    from ``core.autocrlf`` (apm#2971) or ``core.eol`` under ``text=auto``
    (apm#3270), or OS symlinks / Windows link-text files instead of link
    content (#3457). Cache hits rematerialize them so existing caches heal
    without ``apm cache clean``.
    """
    values = _checkout_config_values(checkout_dir) or {}
    return all(
        values.get(key.lower()) == value.lower() for key, value in _checkout_materialization_pins()
    )


def _unmaterialized_paths(
    git_exe: str, checkout_dir: Path, *, env: dict[str, str], timeout: int = 60
) -> list[str]:
    """Return tracked paths the working tree is missing (skip-worktree excluded)."""
    result = run_git_text(
        [git_exe, *_safe_git_args(), "-C", str(checkout_dir), "ls-files", "--deleted", "-z"],
        capture_output=True,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def _pin_sparse_completeness(git_exe: str, checkout_dir: Path, *, env: dict[str, str]) -> None:
    """Record that a sparse shard was verified complete (#3071).

    The pin only lets later cache hits skip the probe, so a failed write is
    logged and ignored rather than discarding a verified checkout.
    """
    key, value = _SPARSE_COMPLETENESS_PIN
    try:
        run_git_text(
            [git_exe, *_safe_git_args(), "-C", str(checkout_dir), "config", key, value],
            capture_output=True,
            timeout=10,
            env=env,
            stdin=subprocess.DEVNULL,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        _log.debug("Could not pin sparse completeness for %s", checkout_dir)


def _sparse_checkout_is_complete(
    checkout_dir: Path,
    sparse_paths: list[str] | None,
    *,
    env: dict[str, str] | None,
) -> bool:
    """Return whether a cached sparse shard materialized every tracked file.

    Only promisor-backed sparse shards can lose blobs silently, so full
    shards skip the probe, and so do shards already pinned as verified.
    Unpinned shards (possibly poisoned by 0.29.1+) are probed once and
    pinned when complete. Probe failures count as incomplete so the shard
    is rebuilt rather than trusted. Callers hold the shard lock.
    """
    if not sparse_paths:
        return True
    key, value = _SPARSE_COMPLETENESS_PIN
    if (_checkout_config_values(checkout_dir) or {}).get(key.lower()) == value:
        return True
    from ..utils.git_env import get_git_executable, git_subprocess_env

    git_exe = get_git_executable()
    subprocess_env = cached_repo_env(git_subprocess_env(env), checkout_dir)
    try:
        # Local index scan only; a short bound limits shard-lock hold time.
        missing = _unmaterialized_paths(git_exe, checkout_dir, env=subprocess_env, timeout=10)
    # SubprocessError also covers GitOutputDecodeError (#3056).
    except (subprocess.SubprocessError, OSError):
        return False
    if missing:
        return False
    _pin_sparse_completeness(git_exe, checkout_dir, env=subprocess_env)
    return True


# Partial bare-cache flavor suffix (perf #1433 follow-up).
# When a caller requests sparse_paths, we use a separate bare keyed at
# ``<shard>__p`` cloned with ``--filter=blob:none``. The partial bare
# downloads commits + trees only (~5% of repo size) and acts as a
# promisor remote; blobs are lazy-fetched at consumer checkout time
# scoped to the sparse cone. Full and partial bares coexist per URL
# so legacy full-tree callers keep today's behavior unchanged.
_PARTIAL_BARE_SUFFIX = "__p"


def _partial_clone_filter_unsupported(exc: subprocess.CalledProcessError) -> bool:
    """Return whether Git diagnosed an unsupported partial-clone filter."""
    details: list[str] = []
    for value in (exc.stderr, exc.stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value:
            details.append(str(value).lower())
    diagnostic = " ".join(details)
    return any(
        signal in diagnostic
        for signal in (
            "does not support filter",
            "filtering not recognized by server",
            "filter capability",
            "filter 'blob:none' not supported",
        )
    )


def _partial_clone_fallback_warning(url: str) -> str:
    """Build a sanitized warning for a completed full-clone fallback."""
    return (
        f"Partial clone unavailable for {_sanitize_url(url)}; "
        "cached a full bare clone instead. Server may not support filter v2."
    )


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


class GitCache:
    """Content-addressable git cache with integrity verification.

    Args:
        cache_root: Root cache directory (from :func:`get_cache_root`).
        refresh: If True, force revalidation even on cache hit.
    """

    def __init__(self, cache_root: Path, *, refresh: bool = False, read_only: bool = False) -> None:
        self._cache_root = cache_root
        self._refresh = refresh
        self._read_only = read_only
        self._db_root = get_git_db_path(cache_root)
        self._checkouts_root = get_git_checkouts_path(cache_root)
        if read_only:
            # A reader (apm audit --ci) never creates, evicts or records entries.
            return

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
        label: str | None = None,
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
            label: The package name messages use for this checkout.

        Returns:
            Path to the checkout directory, verified against its commit
            (``cache/checkout_verify.py``). A checkout that fails is rebuilt
            once from a fresh fetch (its bare is discarded too, since the bare
            may have supplied the bytes), then refused. A read-only cache
            never rebuilds: it raises :class:`CachedCheckoutTamperedError`.
        """
        from ..utils.git_env import git_subprocess_env
        from .checkout_verify import inprocess_verified, verify_checkout

        for attempt in range(2):
            checkout = self._get_checkout(
                url, ref, locked_sha=locked_sha, env=env, sparse_paths=sparse_paths
            )
            sha = checkout.parent.name
            if inprocess_verified(checkout, sha, use="verify"):
                # The fresh landing: every byte was hashed to its object id as written.
                return checkout
            verify_env = git_subprocess_env(env)
            cone = bool(sparse_paths)
            if verify_checkout(checkout, sha, paths=sparse_paths, cone=cone, env=verify_env):
                return checkout
            copy = _cached_copy_name(label, url, sha)
            if self._read_only:
                from ..core.remediation import CACHED_CHECKOUT_TAMPERED

                # A read-only reader (audit) reports a tampered cache; it never repairs it.
                raise CachedCheckoutTamperedError(
                    f"Cached copy of {copy} does not match its commit; a read-only command "
                    f"does not repair the cache. {CACHED_CHECKOUT_TAMPERED.sentence()}"
                )
            if attempt == 0:
                from ..utils.console import _rich_warning

                _rich_warning(
                    f"Cached copy of {copy} failed verification "
                    "against its commit; rebuilding it from a fresh fetch.",
                    symbol="warning",
                )
            self._discard_shard(
                checkout,
                url,
                partial=bool(sparse_paths),
                healed=lambda c=checkout, s=sha, n=cone, e=verify_env: verify_checkout(
                    c, s, paths=sparse_paths, cone=n, env=e
                ),
            )
        raise RuntimeError(
            f"Cached checkout for {_cached_copy_name(label, url, sha)} does not match its "
            "commit even after a fresh fetch; refusing it"
        )

    def _get_checkout(
        self,
        url: str,
        ref: str | None,
        *,
        locked_sha: str | None,
        env: dict[str, str] | None,
        sparse_paths: list[str] | None,
    ) -> Path:
        shard_key = cache_shard_key(url)
        if self._read_only:
            return self._read_only_checkout(shard_key, ref, locked_sha, sparse_paths, env)
        sha = self._resolve_sha(url, ref, locked_sha=locked_sha, env=env)
        # Every commit pin is checked; only an origin-and-commit record (the
        # downloader writes one on admission) lets the check skip the network.
        pinned = locked_sha or ref
        if pinned and _SHA_RE.match(pinned):
            from .commit_reachability import require_reachable_commit

            require_reachable_commit(self._cache_root, url, shard_key, sha, env=env)
        variant = _variant_key(sparse_paths)

        checkout_dir = self._checkouts_root / shard_key / sha / variant

        # Cache hit path (skip if refresh requested)
        if (
            not self._refresh
            and checkout_dir.is_dir()
            # Non-destructive: eviction waits for the shard lock in _create_checkout.
            and self._checkout_trusted(checkout_dir)
        ):
            sha_ok = verify_checkout_sha(checkout_dir, sha)
            if sha_ok and _checkout_has_materialization_pins(checkout_dir):
                with shard_lock(checkout_dir):
                    # An incomplete shard falls through to _create_checkout,
                    # which rebuilds it under the same lock (#3071).
                    if _sparse_checkout_is_complete(checkout_dir, sparse_paths, env=env):
                        _log.debug("Cache HIT: %s @ %s [%s]", _sanitize_url(url), sha[:12], variant)
                        return self._record_checkout_access(checkout_dir)
            elif not sha_ok:
                # Integrity failure -- evict
                _log.warning(
                    "[!] Evicting corrupt cache entry: %s @ %s [%s]",
                    _sanitize_url(url),
                    sha[:12],
                    variant,
                )
                self._evict_checkout(checkout_dir)
            # SHA-valid unpinned trees stay until ``_create_checkout`` holds
            # ``shard_lock`` and emits the rematerialize log. A concurrent
            # consumer may still be reading the old checkout.

        # Cache miss: ensure we have the bare repo, then create checkout.
        # Sparse callers use a partial bare (blob:none) + promisor consumer
        # so only the trees + the blobs reachable from the sparse cone are
        # downloaded. Full-tree callers keep the legacy non-partial bare.
        use_partial = bool(sparse_paths)
        if not use_partial and not self._refresh:
            landed = self._inprocess_checkout(url, shard_key, sha, env=env)
            if landed is not None:
                return landed
        if use_partial and not self._refresh:
            landed = self._inprocess_sparse_checkout(url, shard_key, sha, sparse_paths, env=env)
            if landed is not None:
                return landed
        self._ensure_bare_repo(url, shard_key, sha, env=env, partial=use_partial)
        return self._create_checkout(
            url,
            shard_key,
            sha,
            env=env,
            sparse_paths=sparse_paths,
            promisor_url=url if use_partial else None,
        )

    def _inprocess_sparse_checkout(
        self,
        url: str,
        shard_key: str,
        sha: str,
        sparse_paths: list[str],
        *,
        env: dict[str, str] | None,
    ) -> Path | None:
        """Land a sparse checkout fetched in-process (pin trees + the cone's blobs), or ``None``."""
        from .inprocess_fetch import fetch_sparse_worktree

        cones = tuple(sorted({p.strip("/") for p in sparse_paths if p.strip("/")}))
        return self._land_inprocess(
            shard_key,
            sha,
            sparse_paths,
            lambda staged, pins: fetch_sparse_worktree(url, sha, staged, env, pins, cones),
            f"In-process sparse fetch: {_sanitize_url(url)} @ {sha[:12]}",
        )

    def _inprocess_checkout(
        self, url: str, shard_key: str, sha: str, *, env: dict[str, str] | None
    ) -> Path | None:
        """Land a full checkout fetched in-process, or ``None`` to run git."""
        from .inprocess_fetch import fetch_worktree

        return self._land_inprocess(
            shard_key,
            sha,
            None,
            lambda staged, pins: fetch_worktree(url, sha, staged, env, pins),
            f"In-process fetch: {_sanitize_url(url)} @ {sha[:12]}",
        )

    def _land_inprocess(
        self,
        shard_key: str,
        sha: str,
        sparse_paths: list[str] | None,
        fetch: Callable[[Path, tuple[tuple[str, str], ...]], dict[str, tuple[str, bool]] | None],
        note: str,
    ) -> Path | None:
        """Stage, fetch in process and land one checkout variant; ``None`` means run git."""
        from ..utils.file_ops import robust_rmtree
        from .checkout_verify import register_inprocess_manifest

        sha_parent = self._checkouts_root / shard_key / sha
        ensure_path_within(sha_parent, self._checkouts_root)
        sha_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(sha_parent), 0o700)
        final_dir = sha_parent / _variant_key(sparse_paths)
        lock = shard_lock(final_dir)
        with lock:
            if final_dir.exists():
                return None
            staged = stage_path(final_dir)
            ensure_path_within(staged, self._checkouts_root)
            staged.mkdir(parents=True, exist_ok=True)
            os.chmod(str(staged), 0o700)
            files = fetch(staged, _checkout_materialization_pins())
            if files is None or not atomic_land(staged, final_dir, lock):
                robust_rmtree(staged, ignore_errors=True)
                return None
        register_inprocess_manifest(final_dir, sha, files)
        _log.debug("%s", note)
        return self._record_checkout_access(final_dir)

    def _read_only_checkout(
        self,
        shard_key: str,
        ref: str | None,
        locked_sha: str | None,
        sparse_paths: list[str] | None,
        env: dict[str, str] | None,
    ) -> Path:
        """Return an existing valid checkout for a pinned SHA, or raise ``LookupError``.

        Callers fall back to a fresh clone on any miss, so nothing is written.
        """
        sha = next((v for v in (locked_sha, ref) if v and _SHA_RE.match(v)), None)
        if sha is None:
            raise LookupError("read-only git cache needs a pinned commit")
        checkout_dir = self._checkouts_root / shard_key / sha.lower() / _variant_key(sparse_paths)
        if not (
            checkout_dir.is_dir()
            and self._checkout_trusted(checkout_dir)
            and verify_checkout_sha(checkout_dir, sha.lower())
            and _checkout_has_materialization_pins(checkout_dir)
            and _sparse_checkout_is_complete(checkout_dir, sparse_paths, env=env)
        ):
            raise LookupError("no valid cached checkout for the pinned commit")
        return checkout_dir

    def _resolved_ref_path(self, url: str, ref: str) -> Path:
        digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()
        path = self._db_root / f"{cache_shard_key(url)}-{digest}.ref"
        ensure_path_within(path, self._db_root)
        return path

    def remember_resolved_ref(self, url: str, ref: str, sha: str) -> None:
        """Persist an authorized remote observation independently of bare flavor."""
        if not ref or not _SHA_RE.fullmatch(sha):
            raise ValueError("A remote ref observation requires a ref and full SHA")
        path = self._resolved_ref_path(url, ref)
        with shard_lock(path):
            atomic_write_text(path, sha.lower(), new_file_mode=0o600)

    def read_resolved_ref(self, url: str, ref: str) -> tuple[bool, str | None]:
        """Return receipt presence and SHA; corrupt receipts must not revive old refs."""
        path = self._resolved_ref_path(url, ref)
        try:
            sha = path.read_text(encoding="ascii")
        except FileNotFoundError:
            return False, None
        except (OSError, UnicodeError):
            return True, None
        return True, sha if _SHA_RE.fullmatch(sha) else None

    def _record_checkout_access(self, checkout_dir: Path) -> Path:
        """Record successful reuse of a finalized checkout under its shard lock."""
        # Pruning ages the shared SHA root, not individual checkout variants.
        try:
            os.utime(checkout_dir.parent, None)
        except PermissionError as exc:
            _log.warning(
                "[!] Cannot update Git cache recency for %s: %s. "
                "Continuing with validated checkout; cache prune may evict it. "
                "Check cache permissions or set APM_CACHE_DIR to a writable directory.",
                checkout_dir.parent,
                exc,
            )
        return checkout_dir

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
        from ..deps.git_remote_ops import exact_branch_or_tag_sha, refuse_non_branch_ref
        from ..utils.git_env import git_remote_refs

        if ref:
            refuse_non_branch_ref(ref, _sanitize_url(url))
        # auth-delegated: cache-layer ref resolution runs after lockfile
        # already pinned the commit; no PAT->bearer fallback applies here
        # (env is sanitized, no embedded creds).
        try:
            # The peeled pattern lists an annotated tag's commit too.
            result = git_remote_refs(
                url,
                *((ref, f"{ref}^{{}}") if ref else ()),
                timeout=30,
                env=env,
                git_args=_safe_git_args(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                f"Failed to resolve ref '{ref}' for {_sanitize_url(url)}: {exc}"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"git ls-remote failed for {_sanitize_url(url)}: "
                f"{_sanitize_url(result.stderr.strip())}"
            )
        records = []
        for line in result.stdout.strip().splitlines():
            sha, _, name = line.partition("\t")
            if _SHA_RE.match(sha.strip().lower()):
                records.append((sha.strip().lower(), name.strip()))
        if not ref:
            if records:
                return records[0][0]
            raise RuntimeError(f"Could not resolve ref '{ref}' for {_sanitize_url(url)}")
        return exact_branch_or_tag_sha(records, ref, _sanitize_url(url))

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
        from ..utils.git_env import get_git_executable, git_clone_env, git_no_templates_args

        bare_shard = shard_key + (_PARTIAL_BARE_SUFFIX if partial else "")
        bare_dir = self._db_root / bare_shard
        # Containment guard: defends against pathological shard_key
        # values bypassing the cache root.
        ensure_path_within(bare_dir, self._db_root)
        lock = shard_lock(bare_dir)

        # Acquire the shard lock BEFORE the existence probe so that two
        # concurrent processes hitting a cold shard cannot both perform
        # a full network clone (one would lose the atomic_land race
        # later, but only after wasting bandwidth + wall time).
        with lock:
            if bare_dir.is_dir() and not self._bare_trusted(bare_dir, url=url, env=env):
                self._evict_checkout(bare_dir)
            if bare_dir.is_dir():
                # Repo exists -- check if we have the required SHA
                if self._bare_has_sha(bare_dir, sha, env=env, url=url):
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

            subprocess_env = cached_repo_env(git_clone_env(url, env, staged, bare=True))
            clone_args = [
                git_exe,
                *_safe_git_args(),
                "clone",
                *git_no_templates_args(),
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
                run_git_text(
                    clone_args,
                    capture_output=True,
                    timeout=300,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
            # SubprocessError also covers GitOutputDecodeError (#3056), so a
            # rejected clone output still removes the staged tree.
            except (subprocess.SubprocessError, OSError) as exc:
                # Partial clone fallback: some servers reject --filter
                # (old Gerrit / pre-2.20 GHE). Retry once without it so
                # we never block on this optimization. The resulting
                # bare is full; future sparse consumers find all blobs
                # locally and skip lazy fetch (degrades to baseline,
                # no behavior change for the user).
                fallback_done = False
                if (
                    partial
                    and isinstance(exc, subprocess.CalledProcessError)
                    and _partial_clone_filter_unsupported(exc)
                ):
                    from ..utils.file_ops import robust_rmtree

                    robust_rmtree(staged, ignore_errors=True)
                    staged.mkdir(parents=True, exist_ok=True)
                    os.chmod(str(staged), 0o700)
                    try:
                        run_git_text(
                            [
                                git_exe,
                                *_safe_git_args(),
                                "clone",
                                *git_no_templates_args(),
                                "--bare",
                                "--no-tags",
                                "--no-recurse-submodules",
                                url,
                                str(staged),
                            ],
                            capture_output=True,
                            timeout=300,
                            env=subprocess_env,
                            stdin=subprocess.DEVNULL,
                            check=True,
                        )
                        fallback_done = True
                        from ..utils.console import _rich_warning

                        _rich_warning(_partial_clone_fallback_warning(url))
                    except (subprocess.SubprocessError, OSError) as exc2:
                        from ..utils.file_ops import robust_rmtree

                        robust_rmtree(staged, ignore_errors=True)
                        raise RuntimeError(
                            f"Failed to clone {_sanitize_url(url)} "
                            f"(partial fallback also failed): {exc2}"
                        ) from exc2
                if not fallback_done:
                    # Clean up staged on failure
                    from ..utils.file_ops import robust_rmtree

                    robust_rmtree(staged, ignore_errors=True)
                    raise RuntimeError(f"Failed to clone {_sanitize_url(url)}: {exc}") from exc

            # Complete the staged clone before it becomes a cache entry.
            self._fetch_tag_only_commit(staged, url, sha, env=env, partial=partial)

            # Atomic land (lock is already held; pass it through so the
            # rename completes under the same critical section).
            if not atomic_land(staged, bare_dir, lock):
                # Another process won between our staging and rename
                # (possible only on lock-acquisition timeout fallthrough);
                # verify it has our SHA.
                if not self._bare_has_sha(bare_dir, sha, env=env, url=url):
                    self._fetch_into_bare_locked(bare_dir, url, sha, env=env)

            return bare_dir

    def _fetch_tag_only_commit(
        self,
        staged: Path,
        url: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
        partial: bool = False,
    ) -> None:
        """Best effort: fetch into a staged clone a commit only a tag reaches.

        ``--no-tags`` skipped it. *staged* is this call's own clone, not yet
        a cache entry. A failure leaves it as cloned; the checkout then
        reports the missing commit as before.
        """
        from ..utils.git_env import get_git_executable, git_subprocess_env

        if _is_ref_tip(staged, sha):
            return  # a ref of the clone names it, so the commit is present
        try:
            probe = run_git_text(
                [
                    get_git_executable(),
                    *_safe_git_args(),
                    *("--git-dir", str(staged), "cat-file", "-t", sha),
                ],
                capture_output=True,
                timeout=10,
                env=cached_repo_env(git_subprocess_env(env)),
                stdin=subprocess.DEVNULL,
            )
            if probe.returncode != 0 or "commit" not in probe.stdout:
                self._fetch_sha_into(staged, url, sha, env=env, partial=partial)
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            _log.debug("Tag-only commit fetch skipped for %s: %s", _sanitize_url(url), exc)

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
        from ..utils.git_env import (
            get_git_executable,
            git_network_env,
            git_no_templates_args,
            git_subprocess_env,
        )

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
            existing_ok = (
                final_dir.is_dir()
                and self._checkout_trusted_or_evicted(final_dir)
                and verify_checkout_sha(final_dir, sha)
            )
            if (
                existing_ok
                and _checkout_has_materialization_pins(final_dir)
                and _sparse_checkout_is_complete(final_dir, sparse_paths, env=env)
            ):
                _log.debug(
                    "Write-dedup HIT under lock: %s @ %s [%s]",
                    _sanitize_url(url),
                    sha[:12],
                    variant,
                )
                return self._record_checkout_access(final_dir)
            remat_started: float | None = None
            if existing_ok:
                remat_started = time.monotonic()
                _log.info(
                    "[*] Rematerializing incomplete or unpinned git checkout: %s @ %s [%s]",
                    _sanitize_url(url),
                    sha[:12],
                    variant,
                )
                self._evict_checkout(final_dir)
                if final_dir.exists():
                    raise RuntimeError(
                        "Failed to rematerialize unpinned git checkout "
                        f"for {_sanitize_url(url)} @ {sha[:12]}"
                    )

            staged = stage_path(final_dir)
            ensure_path_within(staged, self._checkouts_root)
            staged.mkdir(parents=True, exist_ok=True)
            os.chmod(str(staged), 0o700)

            git_exe = get_git_executable()
            subprocess_env = cached_repo_env(git_subprocess_env(env), staged)

            try:
                # Clone from local bare repo (fast, no network).
                # ``promisor`` and ``partialclonefilter`` ride on the clone
                # via ``-c key=val`` (one spawn), as do the materialization
                # pins that cache hits require to recognize post-fix shards.
                # Checkout itself still needs the ``-c`` pins from
                # ``_safe_git_args`` because env-frozen host config outranks
                # these local values. ``remote.origin.url`` needs a separate
                # post-clone ``git config`` because git clone always rewrites
                # it to the clone source after applying ``-c`` overrides.
                run_git_text(
                    [
                        git_exe,
                        *_safe_git_args(),
                        "clone",
                        *git_no_templates_args(),
                        "--local",
                        "--shared",
                        "--no-checkout",
                        "--no-recurse-submodules",
                        *(
                            arg
                            for key, value in _checkout_materialization_pins()
                            for arg in ("-c", f"{key}={value}")
                        ),
                        *(
                            [
                                "-c",
                                "remote.origin.promisor=true",
                                "-c",
                                "remote.origin.partialclonefilter=blob:none",
                            ]
                            if promisor_url
                            else []
                        ),
                        str(bare_dir),
                        str(staged),
                    ],
                    capture_output=True,
                    timeout=60,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
                if promisor_url:
                    # Point origin at the real upstream (clone set it to the
                    # local bare). Single config call; the other two promisor
                    # keys were already applied via ``-c`` above.
                    run_git_text(
                        [
                            git_exe,
                            *_safe_git_args(),
                            "-C",
                            str(staged),
                            "config",
                            "remote.origin.url",
                            promisor_url,
                        ],
                        capture_output=True,
                        timeout=10,
                        env=subprocess_env,
                        stdin=subprocess.DEVNULL,
                        check=True,
                    )
                    subprocess_env = cached_repo_env(
                        git_network_env(promisor_url, subprocess_env, worktree=staged), staged
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
                # PERF LEVER C (W3d C1-C12, research): the pinned worktree from the
                # bare's pack in process; None means nothing was written, run git.
                inprocess_files = None
                if not sparse_paths and not promisor_url:
                    from .pack_objects import materialize_tree

                    inprocess_files = materialize_tree(staged, sha, env or os.environ)
            except (RuntimeError, ValueError):
                from ..utils.file_ops import robust_rmtree

                robust_rmtree(staged, ignore_errors=True)
                raise
            except (subprocess.SubprocessError, OSError) as exc:
                from ..utils.file_ops import robust_rmtree

                robust_rmtree(staged, ignore_errors=True)
                raise RuntimeError(
                    f"Failed to create checkout for {_sanitize_url(url)} @ {sha[:12]}: {exc}"
                ) from exc
            if inprocess_files is not None:
                from .checkout_verify import register_inprocess_manifest

                if atomic_land(staged, final_dir, lock):
                    register_inprocess_manifest(final_dir, sha, inprocess_files)
                    _log.debug("In-process checkout: %s @ %s", _sanitize_url(url), sha[:12])
                else:
                    from ..utils.file_ops import robust_rmtree

                    robust_rmtree(staged, ignore_errors=True)
                    self._require_landed_checkout(final_dir, url, sha)
                return final_dir
            try:
                # Checkout the specific SHA.
                checkout = run_git_text(
                    [
                        git_exe,
                        *_safe_git_args(),
                        "-C",
                        str(staged),
                        "checkout",
                        sha,
                    ],
                    capture_output=True,
                    timeout=60,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
                # Old Git exits 0 when a promisor blob fetch fails, leaving
                # an empty tree that would be cached as valid (#3071).
                missing = (
                    _unmaterialized_paths(git_exe, staged, env=subprocess_env)
                    if promisor_url
                    else []
                )
                if missing:
                    raise IncompleteCheckoutError(
                        f"Incomplete checkout for {_sanitize_url(url)} @ {sha[:12]}: "
                        f"{len(missing)} tracked file(s) not materialized "
                        f"(first: {missing[0]}). Git could not fetch file contents; "
                        "the repository may require credentials.",
                        stderr=checkout.stderr or "",
                    )
                if promisor_url:
                    _pin_sparse_completeness(git_exe, staged, env=subprocess_env)
                # Replace link text with Git-object content before landing
                # (#3457). Lenient: this shard may serve any package path or
                # a marketplace read, so an unresolvable link stays link
                # text for the package consumer to reject.
                materialize_tracked_symlinks(
                    git_exe,
                    staged,
                    list(sparse_paths) if sparse_paths else None,
                    env=subprocess_env,
                    extra_git_args=_safe_git_args(),
                    strict=False,
                )
            except (RuntimeError, ValueError):
                from ..utils.file_ops import robust_rmtree

                robust_rmtree(staged, ignore_errors=True)
                raise
            # SubprocessError also covers GitOutputDecodeError (#3056).
            except (subprocess.SubprocessError, OSError) as exc:
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
                # it didn't).
                self._require_landed_checkout(final_dir, url, sha)
            if remat_started is not None:
                _log.info(
                    "[+] Rematerialized git checkout %s @ %s [%s] in %d ms",
                    _sanitize_url(url),
                    sha[:12],
                    variant,
                    int((time.monotonic() - remat_started) * 1000),
                )
            return final_dir

    def _require_landed_checkout(self, final_dir: Path, url: str, sha: str) -> None:
        """A checkout someone else landed is used only with its integrity and pins intact."""
        if not (
            self._checkout_trusted(final_dir)
            and verify_checkout_sha(final_dir, sha)
            and _checkout_has_materialization_pins(final_dir)
        ):
            self._evict_checkout(final_dir)
            raise RuntimeError(
                f"Race condition: concurrent checkout failed integrity "
                f"for {_sanitize_url(url)} @ {sha[:12]}"
            )

    def _bare_has_sha(
        self,
        bare_dir: Path,
        sha: str,
        *,
        env: dict[str, str] | None = None,
        url: str | None = None,
    ) -> bool:
        """Check if the bare repo contains the specified commit."""
        from ..utils.git_env import get_git_executable, git_subprocess_env

        if not self._bare_trusted(bare_dir, url=url, env=env):
            return False
        git_exe = get_git_executable()
        subprocess_env = git_subprocess_env(env)
        subprocess_env = cached_repo_env(subprocess_env)
        try:
            result = run_git_text(
                [git_exe, *_safe_git_args(), "--git-dir", str(bare_dir), "cat-file", "-t", sha],
                capture_output=True,
                timeout=10,
                env=subprocess_env,
                stdin=subprocess.DEVNULL,
            )
            return result.returncode == 0 and "commit" in result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return False

    def bare_ref_sha(self, url: str, ref: str) -> str | None:
        """Resolve *ref* against the cached bare for *url* without network, or None."""
        from ..utils.git_env import exact_named_commit, get_git_executable, git_subprocess_env

        bare_dir = self._db_root / cache_shard_key(url)
        if not (bare_dir.is_dir() and self._bare_trusted(bare_dir)):
            return None

        git = (get_git_executable(), *_safe_git_args(), "--git-dir", str(bare_dir))
        try:
            # An exact branch, tag or commit: never a rev-parse name expansion.
            return exact_named_commit(git, ref, cached_repo_env(git_subprocess_env(None)))
        except (subprocess.SubprocessError, OSError):
            return None

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
            if self._bare_has_sha(bare_dir, sha, env=env, url=url):
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
        if not self._bare_trusted(bare_dir, url=url, env=env):
            raise RuntimeError(
                f"Refusing to fetch into untrusted cache entry for {_sanitize_url(url)}"
            )
        # A partial-flavor bare keeps its filter on fetch so it does not pull
        # every blob the new SHA reaches. Detected via the shard-suffix
        # naming convention (cheap, no git config probe).
        self._fetch_sha_into(
            bare_dir, url, sha, env=env, partial=bare_dir.name.endswith(_PARTIAL_BARE_SUFFIX)
        )

    @staticmethod
    def _fetch_sha_into(
        bare_dir: Path,
        url: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
        partial: bool = False,
    ) -> None:
        """Fetch *sha* into a bare repository the caller already vetted."""
        from ..utils.git_env import get_git_executable, git_network_env

        git_exe = get_git_executable()
        subprocess_env = cached_repo_env(git_network_env(url, env, git_dir=bare_dir))
        is_partial = partial
        fetch_args = [git_exe, *_safe_git_args(), "--git-dir", str(bare_dir), "fetch"]
        if is_partial:
            fetch_args += ["--filter=blob:none"]
        fetch_args += [url, sha]
        try:
            run_git_text(
                fetch_args,
                capture_output=True,
                timeout=120,
                env=subprocess_env,
                stdin=subprocess.DEVNULL,
                check=True,
            )
        except subprocess.CalledProcessError:
            # Some servers do not allow fetching by SHA. Broaden the explicit
            # validated remote without consulting any configured sibling remote.
            fallback_fetch_args = [
                git_exe,
                *_safe_git_args(),
                "--git-dir",
                str(bare_dir),
                "fetch",
            ]
            if is_partial:
                fallback_fetch_args += ["--filter=blob:none"]
            fallback_fetch_args += [url, *_FALLBACK_REFSPECS]
            run_git_text(
                fallback_fetch_args,
                capture_output=True,
                timeout=120,
                env=subprocess_env,
                stdin=subprocess.DEVNULL,
                check=True,
            )

    def _bare_trusted(
        self, bare_dir: Path, *, url: str | None = None, env: dict[str, str] | None = None
    ) -> bool:
        """Whether a cached bare holds only Git state APM writes (see ``cached_repo``).

        ``remote.origin.url`` must key to the bare's shard, or (with *url*)
        be the URL git effectively contacts for *url* under *env*: the
        canonical origin and the effective contact URL, nothing else.
        """
        shard = bare_dir.name.removesuffix(_PARTIAL_BARE_SUFFIX)
        names_shard = _names_shard(shard)
        if url is None or not names_shard(url):
            return cached_repo_is_trusted(bare_dir, bare=True, origin_ok=names_shard)
        from .commit_reachability import effective_contact

        contact = effective_contact(url, env)
        return cached_repo_is_trusted(
            bare_dir,
            bare=True,
            origin_ok=lambda value: (
                names_shard(value)
                or (contact is not None and effective_contact(value, None) == contact)
            ),
        )

    def _checkout_trusted(self, checkout_dir: Path) -> bool:
        """Whether a cached checkout holds only Git state APM writes (see ``cached_repo``)."""
        shard = checkout_dir.parent.parent.name
        bares = (self._db_root / shard, self._db_root / (shard + _PARTIAL_BARE_SUFFIX))
        names_shard = _names_shard(shard)
        return cached_repo_is_trusted(
            checkout_dir,
            bare=False,
            origin_ok=lambda url: names_shard(url) or any(_same_path(url, b) for b in bares),
            alternates_root=self._db_root,
        )

    def _discard_shard(self, checkout_dir: Path, url: str, *, partial: bool, healed) -> None:
        """Remove a checkout that failed verification and the bare it was cloned from.

        Under the shard lock the checkout is checked again: another install may
        have rebuilt it in the meantime, and a healthy rebuild is kept.
        """
        bare_dir = self._db_root / (
            cache_shard_key(url) + (_PARTIAL_BARE_SUFFIX if partial else "")
        )
        with shard_lock(checkout_dir):
            if checkout_dir.is_dir() and healed():
                return
            self._evict_checkout(checkout_dir)
        with shard_lock(bare_dir):
            self._evict_checkout(bare_dir)

    def _checkout_trusted_or_evicted(self, checkout_dir: Path) -> bool:
        """Return True for a trusted checkout; evict an untrusted one and return False."""
        if self._checkout_trusted(checkout_dir):
            return True
        self._evict_checkout(checkout_dir)
        return False

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
                    total_size += dir_size(Path(entry.path))
                elif entry.name.endswith(".ref") and entry.is_file(follow_symlinks=False):
                    total_size += entry.stat(follow_symlinks=False).st_size

        if self._checkouts_root.is_dir():
            for shard_entry in os.scandir(str(self._checkouts_root)):
                if shard_entry.is_dir(follow_symlinks=False):
                    for sha_entry in os.scandir(shard_entry.path):
                        if sha_entry.is_dir(follow_symlinks=False):
                            checkout_count += 1
                            total_size += dir_size(Path(sha_entry.path))

        return {
            "db_count": db_count,
            "checkout_count": checkout_count,
            "total_size_bytes": total_size,
        }

    def clean_all(self) -> list[str]:
        """Remove db and checkouts, returning details of every incomplete removal."""
        from .cleanup import clean_cache_buckets

        return clean_cache_buckets((self._db_root, self._checkouts_root))

    def prune(self, *, max_age_days: int = 30) -> int:
        """Remove checkout entries older than *max_age_days*.

        Uses mtime of the shared SHA directory as the access indicator.
        Successfully reusing any checkout variant refreshes that timestamp.

        Returns:
            Number of SHA groups successfully removed.

        Raises:
            ValueError: If max_age_days is negative.
            CachePruneError: Some entries could not be inspected or removed.
                Other stale entries are still attempted. Completed removals
                and partially deleted entries are not rolled back.
        """
        import time

        from ..utils.file_ops import robust_rmtree

        if max_age_days < 0:
            raise ValueError("max_age_days must be nonnegative; use 0 or a positive number of days")

        cutoff = time.time() - (max_age_days * 86400)
        pruned = 0
        failures: list[tuple[Path, OSError]] = []

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
                        robust_rmtree(Path(sha_entry.path))
                        pruned += 1
                except OSError as exc:
                    failures.append((Path(sha_entry.path), exc))

        if failures:
            raise CachePruneError(pruned, failures)
        return pruned


def _names_shard(shard: str):
    """Return a predicate: does a remote URL key to *shard*?"""

    def check(url: str) -> bool:
        try:
            return cache_shard_key(url) == shard
        except Exception:
            return False

    return check


def _same_path(value: str, path: Path) -> bool:
    from apm_cli.utils.realpath import resolve_path

    try:
        return resolve_path(Path(value)) == resolve_path(path)
    except (OSError, ValueError, RuntimeError):
        return False


def dir_size(path: Path) -> int:
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


def raise_if_tampered(exc: BaseException) -> None:
    """Re-raise the read-only cache's tamper report found in *exc*'s chain (#5159).

    A reader that falls back to a fresh clone on any cache failure calls this
    first, so a tampered cache is reported instead of cloned past.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, CachedCheckoutTamperedError):
            raise current
        seen.add(id(current))
        current = current.__cause__ or current.__context__


def _cached_copy_name(label: str | None, url: str, sha: str) -> str:
    """``<package> (<url> @ <sha12>)``, or ``<url> @ <sha12>`` without a package name."""
    where = f"{_sanitize_url(url)} @ {sha[:12]}"
    return f"{label} ({where})" if label else where


def _sanitize_url(value: str) -> str:
    """Delegate Git diagnostic redaction to its canonical owner."""
    from ..utils.git_env import redact_git_diagnostic

    return redact_git_diagnostic(value)


def attach_persistent_git_cache(
    downloader: object, *, refresh: bool = False, read_only: bool = False
) -> GitCache | None:
    """Attach the one persistent cross-run git cache to *downloader*.

    ``apm install`` (including ``--frozen`` and ``apm update``), the ``apm
    audit --ci`` replay and ``apm outdated`` read locked git sources through
    this same cache, so they recompute ``content_hash`` over the same bytes.
    ``APM_NO_CACHE`` disables it; an unusable cache root degrades to no cache.
    A *read_only* reader (``apm audit --ci``) uses existing checkouts and never
    writes the cache; any miss falls back to a fresh clone.
    """
    if read_only:
        # Commit admission reads verdicts but writes none for this reader,
        # even when no cache attaches.
        downloader.cache_read_only = True  # type: ignore[attr-defined]
    if os.environ.get("APM_NO_CACHE"):
        return None
    from .paths import get_cache_root

    try:
        root = get_cache_root(ensure=not read_only)
        if read_only and not get_git_checkouts_path(root).is_dir():
            return None
        cache = GitCache(root, refresh=refresh, read_only=read_only)
    except (OSError, ValueError):
        return None
    downloader.persistent_git_cache = cache  # type: ignore[attr-defined]
    return cache
