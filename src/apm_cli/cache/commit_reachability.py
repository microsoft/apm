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
import threading
from pathlib import Path

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

_REACH_REFSPECS = (
    "+refs/heads/*:refs/heads/*",
    "+refs/tags/*:refs/tags/*",
)


def _marker(cache_root: Path, shard_key: str, sha: str) -> Path:
    return Path(cache_root) / "reachability" / shard_key / "verified" / sha


class VerdictStage:
    """Verdicts and reachability fetches of one install attempt.

    Nothing reaches the cache until the attempt commits: a refused or
    previewed install leaves no reachability state behind.
    """

    def __init__(self) -> None:
        self._pending: dict[Path, str] = {}
        self._lock = threading.Lock()
        self._scratch: str | None = None

    def add(self, marker: Path, canonical: str) -> None:
        with self._lock:
            self._pending[marker] = canonical

    def holds(self, marker: Path, canonical: str) -> bool:
        with self._lock:
            return self._pending.get(marker) == canonical

    def scratch_root(self) -> Path:
        import tempfile

        with self._lock:
            if self._scratch is None:
                self._scratch = tempfile.mkdtemp(prefix="apm-reach-")
            return Path(self._scratch)

    def commit(self) -> None:
        """Write the staged verdicts (best effort: a missing one is re-checked)."""
        with self._lock:
            pending, self._pending = self._pending, {}
        for marker, canonical in pending.items():
            try:
                _write_marker(marker, canonical)
            except OSError:
                continue
        self._close()

    def discard(self) -> None:
        with self._lock:
            self._pending = {}
        self._close()

    def _close(self) -> None:
        global _active_stage
        import shutil

        with self._lock:
            scratch, self._scratch = self._scratch, None
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        with _stage_lock:
            if _active_stage is self:
                _active_stage = None


_active_stage: VerdictStage | None = None
_stage_lock = threading.Lock()


def open_verdict_stage() -> VerdictStage:
    """Open the stage that holds this install attempt's verdicts until it commits."""
    global _active_stage
    stage = VerdictStage()
    with _stage_lock:
        _active_stage = stage
    return stage


def _stage() -> VerdictStage | None:
    with _stage_lock:
        return _active_stage


def _write_marker(marker: Path, canonical: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(canonical, encoding="utf-8")


def _record(marker: Path, canonical: str) -> None:
    stage = _stage()
    if stage is not None:
        stage.add(marker, canonical)
    else:
        _write_marker(marker, canonical)


def _recorded(marker: Path, canonical: str) -> bool:
    """Read a marker without following links; only a regular file counts."""
    stage = _stage()
    if stage is not None and stage.holds(marker, canonical):
        return True
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
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


class _Unverified(CommitNotReachableError):
    """The origin could not be contacted; *git_error* is the failed git call."""

    def __init__(self, message: str, git_error: Exception | None = None) -> None:
        super().__init__(message)
        self.git_error = git_error


def _git_failure(listing, url: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        listing.returncode, ["git", "ls-remote", url], listing.stdout, listing.stderr
    )


def _persists(downloader) -> bool:
    """A read-only cache (``apm audit`` replay) reads verdicts but never writes them."""
    if getattr(downloader, "cache_read_only", False) is True:
        return False
    return not getattr(getattr(downloader, "persistent_git_cache", None), "_read_only", False)


def _origin(downloader, dep_ref) -> tuple[Path, str]:
    """Return ``(cache_root, origin_url)``: the cache root and the remote the cache keys by."""
    from .paths import get_cache_root

    cache = getattr(downloader, "persistent_git_cache", None)
    root = getattr(cache, "_cache_root", None)
    root = Path(root if isinstance(root, (str, Path)) else get_cache_root())
    return root, downloader._persistent_cache_remote(dep_ref, dep_ref.to_github_url())


def _at_origin(downloader, dep_ref, fn) -> None:
    """Run *fn(url, env)* over the transports and credentials a clone would use.

    A verdict (admitted or refused) ends the attempts. A failed contact
    re-raises its git error so the clone plan tries its next transport or
    credential; when every attempt fails the commit is unverified.
    """
    import tempfile

    verdicts: list[CommitNotReachableError] = []
    contact: list[_Unverified] = []

    def _attempt(url: str, env: dict[str, str], _target: Path) -> None:
        try:
            fn(url, env)
        except _Unverified as exc:
            contact.append(exc)
            raise exc.git_error or subprocess.CalledProcessError(
                1, ["git"], stderr=str(exc)
            ) from exc
        except CommitNotReachableError as exc:
            verdicts.append(exc)

    with tempfile.TemporaryDirectory(prefix="apm-admit-") as scratch:
        try:
            downloader._execute_transport_plan(
                dep_ref.repo_url, Path(scratch) / "unused", dep_ref=dep_ref, clone_action=_attempt
            )
        except CommitNotReachableError:
            raise
        except Exception as exc:
            if verdicts:
                raise verdicts[-1] from None
            raise (contact[-1] if contact else CommitNotReachableError(str(exc))) from exc
    if verdicts:
        raise verdicts[-1]


def _require_at_origin(downloader, dep_ref, sha: str) -> None:
    from .url_normalize import cache_shard_key

    root, origin = _origin(downloader, dep_ref)
    _at_origin(
        downloader,
        dep_ref,
        lambda url, env: require_reachable_commit(
            root,
            url,
            cache_shard_key(origin),
            sha,
            env=env,
            origin=origin,
            persist=_persists(downloader),
        ),
    )


_CONTACTS: dict[tuple, str] = {}


def effective_contact(contact: str, env) -> str | None:
    """Return the normalized URL git actually contacts for *contact* under *env*.

    Applies the ``url.<base>.insteadOf`` rewrites git would apply. Memoized
    per run by the config-bearing environment, so keying a verdict costs no
    extra git process per package.
    """
    merged = {**os.environ, **(env or {})}
    key = (contact, _rewrite_config(merged), _config_files_state(merged))
    if key not in _CONTACTS:
        _CONTACTS[key] = _effective_contact(contact, env)
    return _CONTACTS[key]


def _rewrite_config(env: dict[str, str]) -> tuple:
    """Only what can rewrite a URL: config file locations and ``url.*`` entries.

    Per-attempt credentials and headers never change the contacted URL, so a
    run computes the contact once per origin.
    """
    files = tuple(
        (k, env.get(k))
        for k in (
            *("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "HOME"),
            *("XDG_CONFIG_HOME", "GIT_CONFIG_PARAMETERS"),
        )
    )
    entries = []
    for k, v in env.items():
        if k.startswith("GIT_CONFIG_KEY_") and v.lower().startswith("url."):
            entries.append((v, env.get("GIT_CONFIG_VALUE_" + k.removeprefix("GIT_CONFIG_KEY_"))))
    return files + tuple(sorted(entries, key=str))


def _config_files_state(env: dict[str, str]) -> tuple:
    """Stat the git config files that can carry rewrites (an edit invalidates the memo)."""
    home = env.get("HOME") or os.path.expanduser("~")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    paths = (
        env.get("GIT_CONFIG_GLOBAL") or os.path.join(home, ".gitconfig"),
        os.path.join(xdg, "git", "config"),
        env.get("GIT_CONFIG_SYSTEM") or "/etc/gitconfig",
    )
    state = []
    for path in paths:
        try:
            info = os.stat(path)
            state.append((path, info.st_mtime_ns, info.st_size))
        except OSError:
            state.append((path, None, None))
    return tuple(state)


def _effective_contact(contact: str, env) -> str | None:
    from ..utils.git_env import effective_git_url
    from .url_normalize import normalize_repo_url

    try:
        effective = effective_git_url(contact, env)
    except Exception:
        # Unknown contact: callers neither trust nor record a verdict for it.
        return None
    try:
        return normalize_repo_url(effective)
    except Exception:
        return effective


def _verdict_key(origin: str, contact: str, env) -> str | None:
    """Key a verdict by the origin AND the server actually contacted.

    A ``url.<base>.insteadOf`` rewrite can send the contact to a mirror; a
    verdict from that mirror never stands for the origin once the rewrite
    is gone (or vice versa).
    """
    from .url_normalize import normalize_repo_url

    effective = effective_contact(contact, env)
    return None if effective is None else f"{normalize_repo_url(origin)}\n{effective}"


def _record_tip(downloader, dep_ref, sha: str) -> None:
    from ..deps.transport_selection import initial_transport_scheme
    from .url_normalize import cache_shard_key

    root, origin = _origin(downloader, dep_ref)
    ssh = not dep_ref.artifactory_prefix and initial_transport_scheme(dep_ref) == "ssh"
    contact = downloader._build_repo_url(dep_ref.repo_url, use_ssh=ssh, dep_ref=dep_ref)
    key = _verdict_key(origin, contact, getattr(downloader, "git_env", None))
    if key is None or not _persists(downloader):
        return
    _record(_marker(root, cache_shard_key(origin), sha.lower()), key)


def admit_pinned_commit(downloader, dep_ref, sha: str | None, resolved=None) -> None:
    """Admit a dependency's commit before any cache, bare or clone fetch.

    *sha* is the dependency's own ref. When it is not a commit, the commit
    *resolved* (or ``downloader.resolve_git_reference``) names is used: a
    commit this run read from the origin's current branch or tag is a tip, so
    it is recorded as reachable without a check; a commit replayed from the
    lockfile is checked like any pin. Runs under the credentials the fetch
    would use.

    Raises:
        CommitNotReachableError: The commit is not admitted.
    """
    if not sha or not _COMMIT_RE.match(sha):
        sha, fresh_tip = _resolved_commit(downloader, dep_ref, resolved)
        if sha is None:
            return
        if fresh_tip:
            _record_tip(downloader, dep_ref, sha)
            return
    _require_at_origin(downloader, dep_ref, sha)


def admit_and_resolve(downloader, dep_ref):
    """Resolve *dep_ref*'s ref, admitting its commit before anything can fetch it.

    A declared full commit is admitted before resolution (a legacy resolver
    may clone to resolve it); any other ref is admitted once resolved.

    Raises:
        CommitNotReachableError: The commit is not admitted.
    """
    ref = dep_ref.reference
    if ref and len(ref) in (40, 64) and _COMMIT_RE.match(ref):
        admit_pinned_commit(downloader, dep_ref, ref)
        return downloader.resolve_git_reference(dep_ref)
    resolved = downloader.resolve_git_reference(dep_ref)
    admit_pinned_commit(downloader, dep_ref, ref, resolved=resolved)
    return resolved


def admit_ref_commit(downloader, dep_ref, ref: str) -> tuple[str, str | None]:
    """Return ``(fetch_ref, resolved_commit)`` for a single-file dependency.

    A declared ref is fetched by its admitted commit, never by name. A full
    commit is admitted as a pin. A branch or tag name resolves only exactly:
    through the commits API by ``heads/`` or ``tags/`` namespace, else by
    ``git ls-remote`` over the dependency's own transport. An abbreviated
    commit resolves through the API and is then admitted as a pin.

    An undeclared ref (the default branch), an Azure DevOps branch name
    (resolved exactly by the items API) and an Artifactory route (no Git
    origin) keep fetching by name.

    Raises:
        CommitNotReachableError: The ref names no origin branch or tag, its
            commit is not admitted, or it cannot be resolved.
    """
    from ..deps.tiered_ref_resolver import _ABBREVIATED_COMMIT_RE, _exact_ref_queries

    full = len(ref) in (40, 64) and bool(_COMMIT_RE.match(ref))
    if (
        not dep_ref.reference
        or (dep_ref.is_azure_devops() and not full)
        or dep_ref.is_artifactory()
        or (
            downloader._parse_artifactory_base_url()
            and downloader._should_use_artifactory_proxy(dep_ref)
        )
    ):
        return ref, downloader._resolve_commit_sha_for_ref(dep_ref, ref)
    if full:
        _require_at_origin(downloader, dep_ref, ref.lower())
        return ref.lower(), ref.lower()
    for query in _exact_ref_queries(ref):
        try:
            sha = downloader._resolve_commit_sha_for_ref(dep_ref, query)
        except Exception:
            sha = None
        if sha and len(sha) == 40 and _COMMIT_RE.match(sha):
            if _ABBREVIATED_COMMIT_RE.match(ref):
                _require_at_origin(downloader, dep_ref, sha.lower())
            else:
                _record_tip(downloader, dep_ref, sha)
            return sha.lower(), sha.lower()
    found: list[str] = []
    _at_origin(downloader, dep_ref, lambda url, env: found.append(_exact_tip(url, ref, env)))
    _record_tip(downloader, dep_ref, found[0])
    return found[0], found[0]


def _exact_tip(url: str, ref: str, env) -> str:
    """Resolve *ref* to an exact origin branch or tag with ``git ls-remote``."""
    from ..deps.git_remote_ops import exact_branch_or_tag_sha, refuse_non_branch_ref
    from ..utils.git_env import git_remote_refs
    from .git_cache import _safe_git_args, _sanitize_url

    where = _sanitize_url(url)
    try:
        refuse_non_branch_ref(ref, where)
    except RuntimeError as exc:
        raise CommitNotReachableError(str(exc)) from exc
    try:
        listing = git_remote_refs(
            url, "refs/heads/*", "refs/tags/*", env=env, timeout=60, git_args=_safe_git_args()
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise _Unverified(f"git ls-remote failed for {where}", exc) from exc
    if listing.returncode != 0:
        raise _Unverified(f"git ls-remote failed for {where}", _git_failure(listing, url))
    records = []
    for line in (listing.stdout or "").splitlines():
        tip, _, name = line.partition("\t")
        records.append((tip.strip().lower(), name.strip()))
    try:
        return exact_branch_or_tag_sha(records, ref, where)
    except RuntimeError as exc:
        raise CommitNotReachableError(str(exc)) from exc


def _resolved_commit(downloader, dep_ref, resolved) -> tuple[str | None, bool]:
    """Return ``(commit, fresh_tip)`` for a branch or tag ref.

    ``fresh_tip`` is true when this run read the commit from the origin's
    current refs (not from the lockfile).
    """
    from ..models.dependency.types import GitReferenceType

    tiered = getattr(downloader, "_tiered_resolver", None)
    if resolved is None:
        if tiered is None:
            # No lockfile seed without the tiered resolver: a fresh resolution.
            return None, False
        try:
            resolved = downloader.resolve_git_reference(dep_ref)
        except Exception:
            return None, False
    sha = getattr(resolved, "resolved_commit", None)
    if not sha or not _COMMIT_RE.match(sha):
        return None, False
    if getattr(resolved, "ref_type", None) == GitReferenceType.COMMIT:
        return sha, False
    return sha, tiered is None or tiered.remotely_resolved(dep_ref, sha) is True


def require_reachable_commit(
    cache_root: Path,
    url: str,
    shard_key: str,
    sha: str,
    *,
    env: dict[str, str] | None = None,
    origin: str | None = None,
    persist: bool = True,
) -> None:
    """Refuse *sha* unless an origin branch or tag reaches it.

    *url* is the remote contacted; *origin* (default *url*) is the remote
    the verdict is recorded for. With *persist* false (a read-only cache)
    an existing verdict is reused, but verification state lives in scratch
    storage and no verdict is written.

    Raises:
        RuntimeError: The commit is not reachable from any origin branch or
            tag, or reachability cannot be verified (for example offline on
            the first install of this commit).
    """
    from ..deps.git_remote_ops import tag_commit_shas
    from ..utils.git_env import git_remote_refs
    from .git_cache import _safe_git_args, _sanitize_url

    sha = sha.lower()
    canonical = _verdict_key(origin or url, url, env)
    marker = _marker(cache_root, shard_key, sha)
    if canonical is not None and _recorded(marker, canonical):
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
        raise _Unverified(unverifiable, exc) from exc
    if listing.returncode != 0:
        raise _Unverified(unverifiable, _git_failure(listing, url))
    records = []
    for line in (listing.stdout or "").splitlines():
        tip, _, name = line.partition("\t")
        records.append((tip.strip().lower(), name.strip()))
    tips = {tip for tip, name in records if name.startswith("refs/heads/")}
    tips.update(tag_commit_shas(records)[0].values())
    if sha in tips or (records and _reached_in(cache_root, url, shard_key, sha, env, persist)):
        if canonical is not None and persist:
            _record(marker, canonical)
        return
    raise CommitNotReachableError(
        f"Commit {sha} of {where} is not reachable from any branch or tag of the origin. "
        "Pin a commit on a branch or tag of the source repository, or the branch or tag itself."
    )


def _reached_in(
    cache_root: Path,
    url: str,
    shard_key: str,
    sha: str,
    env: dict[str, str] | None,
    persist: bool,
) -> bool:
    stage = _stage()
    if persist and stage is not None:
        # An open install attempt fetches into its scratch, never the cache.
        return _reached_by_refs(stage.scratch_root(), url, shard_key, sha, env)
    if persist:
        return _reached_by_refs(cache_root, url, shard_key, sha, env)
    import tempfile

    with tempfile.TemporaryDirectory(prefix="apm-reach-") as scratch:
        return _reached_by_refs(Path(scratch), url, shard_key, sha, env)


def _reached_by_refs(
    cache_root: Path, url: str, shard_key: str, sha: str, env: dict[str, str] | None
) -> bool:
    """Fetch the origin's heads and tags (commits only) and ask whether one contains *sha*.

    Containment, not object presence: objects a since-deleted ref brought in
    stay in the repository but no current head or tag contains them. The
    per-origin repository is held under its shard lock for the whole check.

    Raises:
        RuntimeError: The fetch failed.
    """
    from ..utils.git_env import get_git_executable, git_network_env, run_git_text
    from .git_cache import _safe_git_args, _sanitize_url
    from .locking import shard_lock

    repo = Path(cache_root) / "reachability" / shard_key / "refs.git"
    git = [get_git_executable(), *_safe_git_args(), "--git-dir", str(repo)]
    try:
        repo.parent.mkdir(parents=True, exist_ok=True)
        with shard_lock(repo):
            fresh = not repo.is_dir()
            if fresh:
                repo.mkdir()
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
            # Only the current heads and tags may answer: never lazy-fetch.
            probe = run_git_text(
                [
                    *git,
                    *("for-each-ref", "--count=1", "--format=%(refname)"),
                    *("--contains", sha, "refs/heads/", "refs/tags/"),
                ],
                capture_output=True,
                env={**child_env, "GIT_NO_LAZY_FETCH": "1"},
                stdin=subprocess.DEVNULL,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        raise _Unverified(
            f"Cannot verify that commit {sha} of {_sanitize_url(url)} is reachable from a "
            "branch or tag: the first install of a pinned commit needs network access once. "
            "Re-run with network access, or pin a branch or tag instead.",
            exc,
        ) from exc
    return probe.returncode == 0 and bool((probe.stdout or "").strip())


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
