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

import contextlib
import contextvars
import functools
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


# Versioned: verdicts an earlier namespace recorded are never read, so a commit
# an older build admitted without a check is verified again.
_VERDICTS = "verified-v2"


def _marker(cache_root: Path, shard_key: str, sha: str) -> Path:
    return Path(cache_root) / "reachability" / shard_key / _VERDICTS / sha


class VerdictStage:
    """Verdicts and reachability fetches of one install attempt.

    Nothing reaches the cache until the attempt commits: a refused or
    previewed install leaves no reachability state behind.
    """

    def __init__(self) -> None:
        self._pending: dict[Path, str] = {}
        self._lock = threading.Lock()
        self._scratch: str | None = None
        self._memo: dict[str, object] = {}
        # One origin listing per remote for this attempt (see branch_and_tag_refs).
        self.listings: dict = {}

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

    def remember(self, key: str, compute):
        """*compute()*'s value for digest *key*, computed once per attempt."""
        with self._lock:
            if key in self._memo:
                return self._memo[key]
        value = compute()
        with self._lock:
            return self._memo.setdefault(key, value)

    def commit(self) -> None:
        """Write the staged verdicts (best effort: a missing one is re-checked)."""
        with self._lock:
            pending, self._pending = self._pending, {}
            self.listings = {}
        for marker, canonical in pending.items():
            try:
                _write_marker(marker, canonical)
            except OSError:
                continue
        self._close()

    def discard(self) -> None:
        with self._lock:
            self._pending = {}
            self.listings = {}
        self._close()

    def _close(self) -> None:
        global _active_stage
        import shutil

        with self._lock:
            scratch, self._scratch = self._scratch, None
            self._memo = {}
            self.listings = {}
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


def _linked_dirs(path: Path, depth: int = 3) -> bool:
    """Whether any of the *depth* directories above *path* in the ref cache is a link."""
    return any(part.is_symlink() for part in list(path.parents)[:depth])


_FRESH_LISTING = contextvars.ContextVar("apm_fresh_listing", default=False)


@contextlib.contextmanager
def _fresh_listing():
    """Ask the origin again inside: a recheck after a failure needs its state now."""
    token = _FRESH_LISTING.set(True)
    try:
        yield
    finally:
        _FRESH_LISTING.reset(token)


def _attempt_listings() -> dict | None:
    """The open install attempt's listing memo; None outside an attempt or when fresh."""
    stage = _stage()
    return None if stage is None or _FRESH_LISTING.get() else stage.listings


def _write_marker(marker: Path, canonical: str) -> None:
    """Write a verdict as a new regular file, never through a planted link.

    A linked verdict, shard or ``reachability`` directory records
    nothing; the marker itself is replaced atomically, so a link at its
    path is replaced, never followed.
    """
    import tempfile

    if _linked_dirs(marker):
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    if _linked_dirs(marker):
        return
    fd, staged = tempfile.mkstemp(prefix=".verdict-", dir=marker.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical)
        os.replace(staged, marker)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


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
    if _linked_dirs(marker):
        return False
    try:
        # Without O_NOFOLLOW (Windows) the open below would follow a link.
        if not stat.S_ISREG(os.lstat(marker).st_mode):
            return False
    except OSError:
        return False
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
    root = Path(root if isinstance(root, (str, Path)) else get_cache_root(ensure=False))
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
            # No verdict was reached: admission still refuses, never "gone".
            raise (contact[-1] if contact else _Unverified(str(exc))) from exc
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


_REWRITES: dict[tuple, tuple[tuple[str, str], ...] | None] = {}


def effective_contact(contact: str, env) -> str | None:
    """Return the normalized URL git actually contacts for *contact* under *env*.

    Applies the ``url.<base>.insteadOf`` rewrites git would apply. Memoized
    per run by the config-bearing environment, so keying a verdict costs no
    extra git process per package.
    """
    merged = {**os.environ, **(env or {})}
    key = (_rewrite_config(merged), _config_files_state(merged))
    if key not in _REWRITES:
        _REWRITES[key] = _probe_rewrites(env)
    return _effective_contact(contact, _REWRITES[key])


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


def _probe_rewrites(env) -> tuple[tuple[str, str], ...] | None:
    """The rewrites git applies under *env* (one config probe), or None if unknown."""
    from ..utils.git_env import git_url_rewrites

    try:
        return git_url_rewrites(env)
    except Exception:
        return None


def _effective_contact(contact: str, rewrites) -> str | None:
    from ..utils.git_env import resolve_git_url_rewrite
    from .url_normalize import normalize_repo_url

    if rewrites is None:
        # Unknown contact: callers neither trust nor record a verdict for it.
        return None
    try:
        effective = resolve_git_url_rewrite(contact, rewrites) or contact
    except Exception:
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


def _record_observed_tip(downloader, dep_ref, sha: str, url: str, env) -> None:
    """Record a tip read from *url* under *env*: the destination that answered."""
    from .url_normalize import cache_shard_key

    root, origin = _origin(downloader, dep_ref)
    key = _verdict_key(origin, url, env)
    if key is None or not _persists(downloader):
        return
    _record(_marker(root, cache_shard_key(origin), sha.lower()), key)


def _record_tip(downloader, dep_ref, sha: str) -> None:
    from ..deps.transport_selection import initial_transport_scheme
    from .url_normalize import cache_shard_key

    root, origin = _origin(downloader, dep_ref)
    ssh = not dep_ref.artifactory_prefix and initial_transport_scheme(dep_ref) == "ssh"
    contact = downloader._build_repo_url(dep_ref.repo_url, use_ssh=ssh, dep_ref=dep_ref)
    if not _persists(downloader):
        return
    # The resolver may have listed *contact* plainly or cloned it, which can
    # activate a remote-dependent rewrite; which one is unknown here. A tip is
    # recorded only when both name the same server, else it is verified at
    # the fetch destination when next admitted.
    transport = _url_only_env(getattr(downloader, "git_env", None))
    try:
        probed = _probed_listing_env(contact, transport)
    except _Unverified:
        return
    key = _verdict_key(origin, contact, probed)
    if key is None or key != _verdict_key(origin, contact, transport):
        return
    _record(_marker(root, cache_shard_key(origin), sha.lower()), key)


def _url_only_env(env) -> dict[str, str]:
    """*env* without credentials, untranslated: enough to tell which URL git contacts."""
    from ..utils.git_env import clear_git_auth_env, clear_git_platform_token_env

    scrubbed = {**os.environ, **(env or {}), "LC_ALL": "C", "LANGUAGE": "C"}
    clear_git_platform_token_env(scrubbed, remove=True)
    # A url rewrite carried in GIT_CONFIG_PARAMETERS still decides the server (#5246).
    clear_git_auth_env(scrubbed, keep_url_rewrites=True)
    return scrubbed


def admit_pinned_commit(downloader, dep_ref, sha: str | None, resolved=None) -> None:
    """Admit a dependency's commit before any cache, bare or clone fetch.

    *sha* is the dependency's own ref. A full commit is checked as written.
    Any other ref, including a hex-like name (a branch or tag first, else a
    commit abbreviation), is judged by the full commit *resolved* (or
    ``downloader.resolve_git_reference``) names: a commit this run read from
    the origin's current branch or tag listing is a tip, so it is recorded as
    reachable without a check; a hex-like name (which the commits API may
    have expanded), or a commit replayed from the lockfile, is checked at the
    Git destination like any pin. Runs under the credentials the
    fetch would use.

    Raises:
        CommitNotReachableError: The commit is not admitted.
    """
    if sha and len(sha) in (40, 64) and _COMMIT_RE.match(sha):
        _require_at_origin(downloader, dep_ref, sha.lower())
        return
    commit, fresh_tip = _resolved_commit(downloader, dep_ref, resolved)
    hex_name = bool(sha) and bool(_COMMIT_RE.match(sha))
    if commit is None:
        if hex_name:
            raise CommitNotReachableError(f"Could not resolve {sha!r} to an origin commit.")
        return
    # A hex-like name may have been expanded by the hosting service's commits API,
    # which observes no Git destination: its commit is verified where Git fetches.
    if fresh_tip and not hex_name:
        _record_tip(downloader, dep_ref, commit)
        return
    _require_at_origin(downloader, dep_ref, commit.lower())


def admit_fetch_ref(downloader, dep_ref, ref: str | None) -> str | None:
    """Admit *ref*'s commit and return what to fetch: any named ref by its commit.

    A branch or tag can move after admission, and ``git clone --branch``
    would fetch wherever it points then, so every declared ref other than a
    full commit is fetched by the commit it was admitted at (#5138). That
    also keeps ``refs/heads/<x>``, ``refs/tags/<x>`` and hex-like names
    exact, so admission and the fetch judge the same commit. No ref (the
    default branch) is fetched by the commit admitted for it too (#5199);
    only when the default branch resolves to no commit is it fetched by name.

    Raises:
        CommitNotReachableError: The commit is not admitted.
    """
    if not ref:
        return _admitted_default_branch_commit(downloader, dep_ref)
    by_commit = not (len(ref) in (40, 64) and _COMMIT_RE.match(ref))
    if not by_commit:
        admit_pinned_commit(downloader, dep_ref, ref)
        return ref
    try:
        resolved = downloader.resolve_git_reference(dep_ref)
    except Exception as exc:
        commit = _listed_tip_instead(downloader, dep_ref, ref, exc)
        _note_admitted(ref, commit)
        return commit
    commit = resolved.resolved_commit
    if not commit or len(commit) not in (40, 64) or not _COMMIT_RE.match(commit):
        raise CommitNotReachableError(f"Could not resolve {ref!r} to an origin commit.")
    admit_pinned_commit(downloader, dep_ref, ref, resolved=resolved)
    _note_admitted(ref, commit)
    return commit.lower()


def admitted_full_commit(resolved) -> str | None:
    """The full commit *resolved* names, lowercased, or ``None`` (fetched by name then).

    A dependency that names no ref is fetched by this commit once admitted (#5199).
    """
    commit = getattr(resolved, "resolved_commit", None)
    if not isinstance(commit, str) or len(commit) not in (40, 64):
        return None
    return commit.lower() if _COMMIT_RE.match(commit) else None


def _admitted_default_branch_commit(downloader, dep_ref) -> str | None:
    """The commit the default branch is admitted at, or ``None`` to fetch it by name.

    Admission is unchanged (#5199 scope): with the tiered resolver it judges
    the one resolution handed to it here, which is also the commit fetched;
    without it, it admits nothing, as before, and that resolution is fetched.
    """
    tiered = getattr(downloader, "_tiered_resolver", None) is not None
    try:
        resolved = downloader.resolve_git_reference(dep_ref)
    except Exception:
        if not tiered:
            admit_pinned_commit(downloader, dep_ref, None)
            return None
        # One retry (as admission always made): when it resolves, its commit
        # is the one admitted and fetched, not the branch by name, which may
        # have moved (#5199). When it cannot, nothing is admitted and the
        # branch is fetched by name, as before #5199.
        try:
            resolved = downloader.resolve_git_reference(dep_ref)
        except Exception:
            return None
    admit_pinned_commit(downloader, dep_ref, None, resolved=resolved if tiered else None)
    return admitted_full_commit(resolved)


def _listed_tip_instead(downloader, dep_ref, ref: str, failure: Exception) -> str:
    """The exact origin tip of branch or tag *ref* when resolving it by a clone failed.

    Some hosts cannot serve the resolver's shallow clone (a dumb-HTTP server);
    ``git ls-remote`` over the dependency's transport still names the commit,
    matched like the resolver's listing. A name both a branch and a tag holds
    is refused as ambiguous. A hex-like name, any other failure, an origin
    that cannot be listed, or a name no branch or tag holds raises *failure*.
    """
    if _COMMIT_RE.match(ref) or not _is_git_failure(failure):
        raise failure
    try:
        return _listed_tip(downloader, dep_ref, ref)[0].lower()
    except (_Unverified, _NoSuchRef):
        raise failure from None


# The named refs the running download admitted, as ``(ref, commit)`` (#5138).
_ADMITTED: contextvars.ContextVar[list[tuple[str, str]] | None] = contextvars.ContextVar(
    "apm_admitted_named_refs", default=None
)


def _note_admitted(ref: str | None, commit: str | None) -> None:
    admitted = _ADMITTED.get()
    if admitted is not None and ref and commit:
        admitted.append((ref, commit.lower()))


class AdmittedCommitGoneError(CommitNotReachableError):
    """A named ref's admitted commit can no longer be fetched from the origin."""


def name_a_lost_admitted_commit(download):
    """Make a download of a named ref whose admitted commit is gone fail by name (#5138).

    A named ref is fetched by the commit it was admitted at, never by name.
    When the download fails and that commit is no longer reachable from any
    branch or tag of the origin (the ref was rewritten), the failure says so
    instead of a git error. Any other failure is raised unchanged.
    """

    @functools.wraps(download)
    def _download(downloader, repo_ref, *args, **kwargs):
        token = _ADMITTED.set([])
        try:
            return download(downloader, repo_ref, *args, **kwargs)
        except CommitNotReachableError:
            raise
        except Exception as exc:
            lost = None
            if _is_git_failure(exc):
                lost = _lost_admitted_commit(downloader, repo_ref, _ADMITTED.get() or [])
            if lost is None:
                raise
            raise _commit_gone(*lost) from exc
        finally:
            _ADMITTED.reset(token)

    return _download


def _commit_gone(ref: str, admitted: str, now: str) -> AdmittedCommitGoneError:
    from ..utils.diagnostics import printable_ascii_text

    ref = printable_ascii_text(repr(ref))
    return AdmittedCommitGoneError(
        f"{ref} was admitted at commit {admitted[:12]}, which can no longer be "
        f"fetched from the origin ({ref} now points to {now}). Run the install "
        "again to admit its current commit, or 'apm update' when the lockfile "
        "pins it."
    )


def admitted_tip_name(url: str, ref: str, admitted: str, env) -> str | None:
    """The full name of *ref* at the origin when it still points at *admitted*.

    For a server that will not serve a commit by its id (#5138): the ref is
    then fetched by this name. ``None`` when the origin cannot be listed.

    Raises:
        AdmittedCommitGoneError: *ref* no longer points at *admitted*.
    """
    try:
        with _fresh_listing():
            records = _branch_and_tag_records(url, env)
    except _Unverified:
        return None
    from ..deps.git_remote_ops import exact_branch_or_tag_sha

    try:
        tip = exact_branch_or_tag_sha(records, ref, _sanitize_origin(url))
    except RuntimeError:
        tip = None
    if tip != admitted.lower():
        raise _commit_gone(ref, admitted, tip[:12] if tip else "no branch or tag")
    if ref.startswith(("refs/heads/", "refs/tags/")):
        return ref
    names = {name for _tip, name in records}
    return f"refs/heads/{ref}" if f"refs/heads/{ref}" in names else f"refs/tags/{ref}"


def _is_git_failure(exc: BaseException) -> bool:
    """Whether *exc* (or what it wraps) is a git fetch, clone or checkout failure.

    Validation and filesystem errors after a successful fetch are not.
    """
    from ..deps.git_lazy import git_command_error

    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, (subprocess.CalledProcessError, git_command_error())):
            return True
        if isinstance(seen, RuntimeError) and str(seen).startswith(
            ("Failed to clone repository", "Failed to checkout commit")
        ):
            return True
        seen = seen.__cause__
    return False


def _lost_admitted_commit(downloader, repo_ref, admitted) -> tuple[str, str, str] | None:
    """``(ref, commit, now)`` when a named ref's admitted commit left the origin."""
    if not admitted:
        return None
    ref, commit = admitted[-1]
    try:
        from ..models.dependency.reference import DependencyReference

        dep_ref = DependencyReference.parse(repo_ref) if isinstance(repo_ref, str) else repo_ref
        with _fresh_listing():
            if _reachable_now(downloader, dep_ref, commit) is not False:
                return None
            found: list[str] = []
            with contextlib.suppress(CommitNotReachableError):
                _at_origin(
                    downloader, dep_ref, lambda url, env: found.append(_exact_tip(url, ref, env))
                )
    except Exception:
        return None
    return ref, commit, found[0][:12] if found else "no branch or tag"


def _reachable_now(downloader, dep_ref, sha: str) -> bool | None:
    """Whether an origin branch or tag reaches *sha* now, ignoring recorded verdicts."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="apm-reach-check-") as scratch:
        try:
            _at_origin(
                downloader,
                dep_ref,
                lambda url, env: require_reachable_commit(
                    Path(scratch), url, "check", sha, env=env, persist=False
                ),
            )
        except _Unverified:
            return None
        except CommitNotReachableError:
            return False
    return True


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
    _note_admitted(ref, getattr(resolved, "resolved_commit", None))
    return resolved


def admit_ref_commit(downloader, dep_ref, ref: str) -> tuple[str, str | None]:
    """Return ``(fetch_ref, resolved_commit)`` for a single-file dependency.

    A declared ref is fetched by its admitted commit, never by name. A full
    commit is admitted as a pin. A branch or tag name resolves only exactly,
    by ``git ls-remote`` over the dependency's own transport. An abbreviated
    commit resolves through the commits API and is then admitted as a pin.

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
    # A name is an exact origin branch or tag first, as Git prefers a ref. Only a
    # hex name no ref holds expands, through the commits API, as an abbreviated
    # commit, which is then admitted as a pin.
    try:
        return _listed_tip(downloader, dep_ref, ref)
    except _NoSuchRef:
        if not _ABBREVIATED_COMMIT_RE.match(ref):
            raise
        for query in _exact_ref_queries(ref):
            try:
                sha = downloader._resolve_commit_sha_for_ref(dep_ref, query)
            except Exception:
                sha = None
            if sha and len(sha) == 40 and _COMMIT_RE.match(sha):
                _require_at_origin(downloader, dep_ref, sha.lower())
                return sha.lower(), sha.lower()
        raise


class _NoSuchRef(CommitNotReachableError):
    """The origin has no branch or tag of that name."""


def _listed_tip(downloader, dep_ref, ref: str) -> tuple[str, str]:
    """Resolve *ref* to an exact origin branch or tag over the dependency's transport.

    The tip is recorded under the environment that listed it, so a verdict a
    remote-activated mirror gave is keyed by that mirror, never the origin.
    """
    found: list[tuple[str, str, dict]] = []

    def _list(url: str, env) -> None:
        listing = _probed_listing_env(url, env)
        found.append((_exact_tip(url, ref, listing, probed=True), url, listing))

    _at_origin(downloader, dep_ref, _list)
    sha, url, env = found[0]
    _record_observed_tip(downloader, dep_ref, sha, url, env)
    return sha, sha


def _sanitize_origin(url: str) -> str:
    from .git_cache import _sanitize_url

    return _sanitize_url(url)


def _listing_env(url: str, env) -> dict[str, str] | None:
    """The env a clone of *url* gets, so config its remote URL activates applies.

    Only an ``includeIf`` section can make config depend on the remote, so
    without one *env* already is that env and nothing is probed. With one
    (or when that cannot be told), a scratch repository with *url* as its
    origin is probed, so a remote-activated mirror rewrite sends the listing
    where the fetch would go. An install attempt's verdict stage keeps each
    answer, keyed by a digest of the URL and the whole environment
    (credentials included, never stored in clear), and drops them when the
    attempt ends: git config is read once per attempt. Without an open stage
    nothing is kept.
    """
    import tempfile

    from ..utils.git_env import git_clone_env

    def _probe() -> dict[str, str]:
        with tempfile.TemporaryDirectory(prefix="apm-clone-config-") as probe:
            return git_clone_env(url, env, Path(probe) / "origin")

    merged = {**os.environ, **(env or {})}
    if not _remembered(("includeif", *sorted(_config_scope(merged))), _has_include_if, merged):
        return env
    return dict(_remembered(("probe", url, *sorted(merged.items())), _probe))


def _config_scope(env: dict[str, str]) -> list[tuple[str, str]]:
    """The environment entries that choose which git config is read."""
    return [
        (str(k), str(v))
        for k, v in env.items()
        if str(k).startswith("GIT_CONFIG") or k in ("HOME", "XDG_CONFIG_HOME")
    ]


def _remembered(parts: tuple, compute, *args):
    """``compute(*args)``, kept by the open verdict stage for the attempt."""
    import hashlib

    stage = _stage()
    if stage is None:
        return compute(*args)
    digest = hashlib.sha256()
    for part in parts:
        text = repr(part)
        digest.update(f"{len(text)}:{text}".encode())
    return stage.remember(digest.hexdigest(), lambda: compute(*args))


def _has_include_if(env: dict[str, str]) -> bool:
    """Whether any git config scope *env* reads has an ``includeIf`` entry.

    *env* is the process environment with the call's overrides: the config
    a listing applies is the parent's, snapshotted, plus those overrides.

    True when it cannot be told: the caller then probes (fails closed).
    """
    import tempfile

    from ..utils.git_env import get_git_executable, git_subprocess_env, run_git_text

    try:
        found = run_git_text(
            [get_git_executable(), "config", "--get-regexp", r"^includeif\."],
            capture_output=True,
            cwd=tempfile.gettempdir(),
            env=git_subprocess_env(env),
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return True
    return found.returncode != 1


def _probed_listing_env(url: str, env) -> dict[str, str]:
    """:func:`_listing_env`, unverified (never refused) when the probe fails."""
    try:
        return _listing_env(url, env)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise _Unverified(f"git ls-remote failed for {_sanitize_origin(url)}", exc) from exc


def _branch_and_tag_records(url: str, env, *, probed: bool = False) -> list[tuple[str, str]]:
    """The origin's ``(commit, name)`` branch and tag records, from ``git ls-remote``.

    *env* is already the listing env when *probed*.
    """
    from ..deps.anonymous_ref_listing import branch_and_tag_refs
    from .git_cache import _safe_git_args

    where = _sanitize_origin(url)
    listing_env = env if probed else _probed_listing_env(url, env)
    try:
        listing = branch_and_tag_refs(
            url,
            env=listing_env,
            timeout=60,
            git_args=_safe_git_args(),
            memo=_attempt_listings(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise _Unverified(f"git ls-remote failed for {where}", exc) from exc
    if listing.returncode != 0:
        raise _Unverified(f"git ls-remote failed for {where}", _git_failure(listing, url))
    records = []
    for line in (listing.stdout or "").splitlines():
        tip, _, name = line.partition("\t")
        records.append((tip.strip().lower(), name.strip()))
    return records


def _exact_tip(url: str, ref: str, env, *, probed: bool = False) -> str:
    """Resolve *ref* to an exact origin branch or tag with ``git ls-remote``."""
    from ..deps.git_remote_ops import exact_branch_or_tag_sha, refuse_non_branch_ref

    where = _sanitize_origin(url)
    try:
        refuse_non_branch_ref(ref, where)
    except RuntimeError as exc:
        raise CommitNotReachableError(str(exc)) from exc
    records = _branch_and_tag_records(url, env, probed=probed)
    wanted = {ref} if ref.startswith("refs/") else {f"refs/heads/{ref}", f"refs/tags/{ref}"}
    try:
        return exact_branch_or_tag_sha(records, ref, where)
    except RuntimeError as exc:
        if wanted & {name for _tip, name in records}:
            raise CommitNotReachableError(str(exc)) from exc
        raise _NoSuchRef(str(exc)) from exc


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

    *url* is the remote contacted, under the env a clone of it gets; *origin* (default *url*) is the remote
    the verdict is recorded for. With *persist* false (a read-only cache)
    an existing verdict is reused, but verification state lives in scratch
    storage and no verdict is written.

    Raises:
        RuntimeError: The commit is not reachable from any origin branch or
            tag, or reachability cannot be verified (for example offline on
            the first install of this commit).
    """
    from ..deps.anonymous_ref_listing import branch_and_tag_refs
    from ..deps.git_remote_ops import tag_commit_shas
    from .git_cache import _safe_git_args, _sanitize_url

    sha = sha.lower()
    where = _sanitize_url(url)
    unverifiable = (
        f"Cannot verify that commit {sha} of {where} is reachable from a branch or tag: "
        "the first install of a pinned commit needs network access once. "
        "Re-run with network access, or pin a branch or tag instead."
    )
    # The verdict key, the listing and the reachability fetch (whose persisted
    # repository has an origin remote) all use the env a clone of *url* gets,
    # so each names the server that answers, a remote-activated mirror included.
    try:
        env = _listing_env(url, env)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise _Unverified(unverifiable, exc) from exc
    canonical = _verdict_key(origin or url, url, env)
    marker = _marker(cache_root, shard_key, sha)
    if canonical is not None and _recorded(marker, canonical):
        return
    try:
        listing = branch_and_tag_refs(
            url, env=env, timeout=60, git_args=_safe_git_args(), memo=_attempt_listings()
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
    from ..utils.file_ops import robust_rmtree
    from ..utils.git_config_memo import invalidate as invalidate_git_config
    from ..utils.git_env import get_git_executable, git_network_env, run_git_text
    from .cached_repo import cached_repo_env, cached_repo_is_trusted, inert_git_args
    from .git_cache import _safe_git_args, _sanitize_url
    from .locking import shard_lock

    repo = Path(cache_root) / "reachability" / shard_key / "refs.git"
    git = [get_git_executable(), *_safe_git_args(), *inert_git_args(), "--git-dir", str(repo)]
    try:
        # The ref cache never follows a link: a linked parent fails closed,
        # and a linked repository is unlinked (its target is never touched).
        if _linked_dirs(repo, depth=2):
            raise OSError("the reachability cache path contains a link")
        repo.parent.mkdir(parents=True, exist_ok=True)
        with shard_lock(repo):
            if repo.is_symlink():
                repo.unlink()
            # A persisted repository holds only Git state APM writes; any
            # other configuration is discarded before Git reads it.
            if repo.is_dir() and not cached_repo_is_trusted(
                repo, bare=True, origin_ok=lambda _url: True
            ):
                robust_rmtree(repo)
            fresh = not repo.is_dir()
            if fresh:
                repo.mkdir()
                os.chmod(repo, 0o700)
            child_env = cached_repo_env(git_network_env(url, env, git_dir=repo), repo)
            if fresh:
                run_git_text(
                    [*git, "init", "-q", "--bare"],
                    capture_output=True,
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
            run_git_text(
                [*git, "config", "remote.origin.url", url],
                capture_output=True,
                env=child_env,
                stdin=subprocess.DEVNULL,
                check=True,
            )
            invalidate_git_config(repo)
            run_git_text(
                [*git, "fetch", "-q", "--filter=tree:0", "--prune", "origin", *_REACH_REFSPECS],
                capture_output=True,
                timeout=600,
                env=child_env,
                stdin=subprocess.DEVNULL,
                check=True,
            )
            _drop_promisor(git, child_env)
            invalidate_git_config(repo)
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
