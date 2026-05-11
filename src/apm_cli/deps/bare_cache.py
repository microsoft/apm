"""Bare-repo clone + materialization helpers for the WS2 dedup pipeline.

Extracted from :mod:`github_downloader` to keep that module under the
2400-line CI guardrail (see ``.github/workflows/build-release.yml``).

Public entry points:

* :func:`clone_with_fallback` -- working-tree clone via
  ``Repo.clone_from``, threaded through a caller-supplied transport-plan
  executor.
* :func:`bare_clone_with_fallback` -- 3-tier bare-repo clone for the
  shared cache (the core fix for #1126).
* :func:`materialize_from_bare` -- per-consumer working-tree checkout
  backed by a shared bare's object database.

All three are pure free functions; behavior is unchanged from the
original methods on :class:`GitHubPackageDownloader`. Test contracts
that patch ``downloader._clone_with_fallback`` /
``downloader._bare_clone_with_fallback`` /
``downloader._materialize_from_bare`` are preserved by keeping thin
delegating instance methods on the class.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from git import Repo

if TYPE_CHECKING:
    from ..models.apm_package import DependencyReference

_log = logging.getLogger(__name__)


def _rmtree(path: Path) -> None:
    """Remove a directory tree, handling read-only files and brief Windows locks.

    Delegates to :func:`robust_rmtree` which retries with exponential backoff
    on transient lock errors and chmod-resets read-only ``.git/objects/pack``
    files (Windows portability finding from the #1126 paper audit).
    Duplicated from :mod:`github_downloader` to avoid a circular import
    (``github_downloader`` imports from this module).
    """
    from ..utils.file_ops import robust_rmtree

    robust_rmtree(path, ignore_errors=True)


def _scrub_bare_remote_url(bare_path: Path, git_exe: str, env: dict[str, str]) -> None:
    """Redact ``remote.origin.url`` in a bare repo's ``.git/config``.

    After a successful bare clone, ``remote.origin.url`` retains the
    tokenized URL (e.g. ``https://oauth2:<TOKEN>@github.com/...``). The
    bare is read-only after this point in the WS2 dedup pipeline (no
    further fetches), so the URL is dead weight that just persists the
    token on disk. Replace with ``redacted://`` to eliminate the
    on-disk token footprint.

    Defense-in-depth: tier-1 (init + remote add + fetch) leaves
    ``FETCH_HEAD`` containing the tokenized URL on disk even after the
    config scrub. Truncate it to empty so the token does not survive
    in any on-disk artifact. Best-effort (non-fatal on OSError).

    Best-effort: ``check=False`` so a config-set failure does not abort
    the clone (the bare is still functionally correct without the
    redaction). Convergent panel finding (auth + supply-chain MAJOR).
    On exception, log at WARNING so token-leak-aware operators have an
    audit trail (supply-chain reviewer follow-up: security mechanisms
    must not fail silently).
    """
    try:
        result = subprocess.run(
            [git_exe, "-C", str(bare_path), "remote", "set-url", "origin", "redacted://"],
            env=env,
            check=False,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            logging.getLogger(__name__).warning(
                "Failed to redact remote URL from bare repo config at %s "
                "(git exit=%d). Tokenized URL may persist on disk until "
                "shared cache cleanup.",
                bare_path,
                result.returncode,
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Exception while redacting remote URL from bare repo config "
            "at %s: %s. Tokenized URL may persist on disk until shared "
            "cache cleanup.",
            bare_path,
            exc,
        )

    # Defense-in-depth: truncate FETCH_HEAD which retains the tokenized
    # URL after tier-1 init+fetch (supply-chain panel follow-up).
    fetch_head = bare_path / "FETCH_HEAD"
    try:
        if fetch_head.exists():
            fetch_head.write_text("")
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Failed to truncate FETCH_HEAD at %s: %s. Tokenized URL "
            "may persist on disk until shared cache cleanup.",
            fetch_head,
            exc,
        )


def bare_clone_with_fallback(
    execute_transport_plan: Callable[..., None],
    repo_url_base: str,
    bare_target: Path,
    *,
    dep_ref: DependencyReference,
    ref: str | None,
    is_commit_sha: bool,
) -> None:
    """Clone a repository as a bare repo, with full transport-plan fallback.

    Sibling helper to :meth:`GitHubPackageDownloader._clone_with_fallback`.
    Composes via the caller-supplied ``execute_transport_plan`` callable
    (typically ``self._execute_transport_plan``) so it inherits ADO
    bearer retry and protocol fallback automatically. The bare clone is
    subdir-agnostic (no sparse cone), so a single bare can serve N
    consumers each materializing a different subdir from the same
    repo+ref - this is the core fix for #1126.

    3-tier strategy (per design.md sec 5.2):

      - Tier 1 (SHA refs): ``git init --bare && git remote add
        origin <url> && git fetch --depth=1 origin <sha>``. Requires
        server-side ``uploadpack.allowReachableSHA1InWant=true``
        (default on github.com / GHES; some older GHE / ADO Server /
        Bitbucket Server reject this).
      - Tier 1 (symbolic / default branch): ``git clone --bare
        --depth=1 [--branch <ref>] <url>``.
      - Tier 2 (both): full ``git clone --bare <url>``, validate via
        ``git rev-parse --verify <sha>^{commit}`` for SHA refs.
      - Tier 3: re-raise.

    After every successful tier, ``git update-ref HEAD <ref>`` is
    invoked for SHA refs so consumer ``git rev-parse HEAD`` resolves
    unambiguously (the v3 BLOCKER fix). Then
    :func:`_scrub_bare_remote_url` redacts the tokenized
    ``remote.origin.url`` from ``.git/config`` to eliminate
    on-disk token persistence (panel convergent finding).

    Note: bare-integrity verification (post-clone ``rev-parse HEAD
    == known_sha``) is intentionally deferred. The bare is
    ephemeral, mode-0700, and produced by a trusted (in-tree)
    callable. If ``SharedCloneCache`` is ever opened to
    plugin/user-supplied callables, restore the integrity check at
    the cache boundary. See design.md sec 12 (Bare integrity
    verification).
    """
    from ..utils.git_env import get_git_executable

    git_exe = get_git_executable()

    def _bare_action(url: str, env: dict[str, str], target: Path) -> None:
        # Pre-attempt cleanup: any prior tier-1 partial state must be
        # wiped before re-attempting (e.g. on ADO bearer retry the
        # template re-invokes _bare_action with a fresh URL/env, and
        # the tier-1 init+fetch leaves a half-built bare on disk).
        # See 6.15.
        if target.exists():
            _rmtree(target)

        if is_commit_sha:
            # Tier 1 (init + fetch by SHA) requires a full 40-char SHA:
            # `git fetch origin <sha>` only works for full SHAs (the
            # smart-HTTP protocol does not resolve abbreviated SHAs),
            # and `git update-ref HEAD <sha>` requires a full 40-char
            # SHA-1. For short SHAs, skip directly to tier 2 where
            # `rev-parse <short>` against the full bare can resolve
            # the abbreviation. Copilot review finding (#1135).
            if len(ref) == 40:
                # Note: `git remote add origin <url>` stores the
                # tokenized URL in `.git/config`. The ADO-bearer env
                # approach relies on http.extraHeader (not stored URL
                # auth), so this is safe today. _scrub_bare_remote_url
                # below redacts the URL after a successful clone so the
                # token does not persist on disk.
                try:
                    subprocess.run(
                        [git_exe, "init", "--bare", str(target)],
                        env=env,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        [git_exe, "-C", str(target), "remote", "add", "origin", url],
                        env=env,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        [git_exe, "-C", str(target), "fetch", "--depth=1", "origin", ref],
                        env=env,
                        check=True,
                        capture_output=True,
                        timeout=300,
                    )
                    # CRITICAL (v3 BLOCKER): init+fetch leaves HEAD pointing
                    # at refs/heads/main which doesn't exist locally.
                    # Without update-ref, consumer's `git rev-parse HEAD`
                    # is ambiguous. See 6.18.
                    subprocess.run(
                        [git_exe, "-C", str(target), "update-ref", "HEAD", ref],
                        env=env,
                        check=True,
                        capture_output=True,
                    )
                    _scrub_bare_remote_url(target, git_exe, env)
                    return
                except subprocess.CalledProcessError:
                    pass  # fall through to tier 2

            # Tier 2: full bare clone, validate SHA, set HEAD.
            if target.exists():
                _rmtree(target)
            subprocess.run(
                [git_exe, "clone", "--bare", url, str(target)],
                env=env,
                check=True,
                capture_output=True,
                timeout=600,
            )
            # Resolve abbreviated SHAs (and re-validate full SHAs) against
            # the full clone. `rev-parse <short>^{commit}` returns the
            # 40-char SHA; we feed that into update-ref so HEAD never
            # holds a partial reference. Without this, short-SHA pins
            # would set resolved_commit to the abbreviation and lockfile
            # comparisons against `head.commit.hexsha` (always 40-char)
            # would never match. Copilot review finding (#1135).
            full_sha_result = subprocess.run(
                [
                    git_exe,
                    "-C",
                    str(target),
                    "rev-parse",
                    "--verify",
                    f"{ref}^{{commit}}",
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            full_sha = full_sha_result.stdout.strip()
            subprocess.run(
                [git_exe, "-C", str(target), "update-ref", "HEAD", full_sha],
                env=env,
                check=True,
                capture_output=True,
            )
            _scrub_bare_remote_url(target, git_exe, env)
            return

        # Symbolic ref or default branch.
        args = [git_exe, "clone", "--bare", "--depth=1"]
        if ref:
            args += ["--branch", ref]
        args += [url, str(target)]
        try:
            subprocess.run(args, env=env, check=True, capture_output=True, timeout=300)
            _scrub_bare_remote_url(target, git_exe, env)
            return
        except subprocess.CalledProcessError:
            # Tier 2: full bare clone (no shallow, no --branch).
            _rmtree(target)
            subprocess.run(
                [git_exe, "clone", "--bare", url, str(target)],
                env=env,
                check=True,
                capture_output=True,
                timeout=600,
            )
            _scrub_bare_remote_url(target, git_exe, env)
            return

    execute_transport_plan(
        repo_url_base,
        bare_target,
        dep_ref=dep_ref,
        clone_action=_bare_action,
    )


def fetch_sha_into_bare(
    execute_transport_plan: Callable[..., None],
    repo_url_base: str,
    bare_path: Path,
    sha: str,
    *,
    dep_ref: DependencyReference,
) -> bool:
    """Attempt to fetch a specific SHA into an existing bare repo.

    Used to hydrate shallow bare clones that are missing a transitive
    SHA-pinned commit.  Three-step strategy:

    1. **Check first** -- ``git rev-parse --verify <sha>^{commit}`` against
       the bare.  If the SHA is already present, returns ``True`` immediately
       without any network I/O.
    2. **Shallow fetch by SHA** (full 40-char SHAs only) -- invokes
       ``execute_transport_plan`` with a fetch action that runs
       ``git fetch <url> <sha>``.  Uses the authenticated URL supplied by
       the transport plan, NOT ``git fetch origin <sha>``, because
       ``remote.origin.url`` has been redacted to ``redacted://`` by
       :func:`_scrub_bare_remote_url`.  After the fetch, verifies with
       ``rev-parse --verify``.  Returns ``True`` on success.
    3. **Broaden shallow** -- invokes ``execute_transport_plan`` with a
       fetch action that runs ``git fetch <url>`` (no ref argument),
       broadening the shallow boundary to include all remote refs.  After
       the fetch, verifies with ``rev-parse --verify``.  Returns ``True``
       on success.

    On any failure in steps 2 or 3, returns ``False`` so the caller can
    fall back to a fresh bare clone.

    Note: this function deliberately does NOT call ``git update-ref HEAD``
    after a successful fetch.  The consumer's :func:`materialize_from_bare`
    handles SHA resolution independently via the ``known_sha`` parameter.

    Args:
        execute_transport_plan: Callable that orchestrates auth and protocol
            fallback (typically ``self._execute_transport_plan``).
        repo_url_base: Base repo URL (unauthenticated) passed to the
            transport plan so it can inject credentials.
        bare_path: Path to the existing bare repo on disk.
        sha: The Git commit SHA to fetch.
        dep_ref: Dependency reference used by the transport plan for
            auth context.

    Returns:
        ``True`` if the SHA is now present in the bare, ``False`` otherwise.
    """
    from ..utils.git_env import get_git_executable

    git_exe = get_git_executable()

    def _rev_parse_present() -> bool:
        """Return True if sha is already reachable in the bare."""
        try:
            result = subprocess.run(
                [
                    git_exe,
                    "--git-dir",
                    str(bare_path),
                    "rev-parse",
                    "--verify",
                    f"{sha}^{{commit}}",
                ],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _scrub_fetch_head() -> None:
        """Truncate FETCH_HEAD to remove the token-embedded URL written by fetch."""
        fetch_head = bare_path / "FETCH_HEAD"
        try:
            if fetch_head.exists():
                fetch_head.write_text("")
        except OSError as exc:
            _log.warning(
                "Failed to truncate FETCH_HEAD at %s: %s. Tokenized URL "
                "may persist on disk until shared cache cleanup.",
                fetch_head,
                exc,
            )

    # Step 1: check first -- no network if SHA already present.
    _log.debug("fetch_sha_into_bare: checking if %s is present in %s", sha[:12], bare_path)
    if _rev_parse_present():
        _log.debug("fetch_sha_into_bare: SHA %s already present, skipping fetch", sha[:12])
        return True

    # Step 2: shallow fetch by full SHA (only for full 40-char SHAs).
    if len(sha) == 40:
        _log.debug(
            "fetch_sha_into_bare: attempting shallow fetch of %s into %s", sha[:12], bare_path
        )

        def _fetch_action_sha(url: str, env: dict[str, str], target: Path) -> None:
            subprocess.run(
                [git_exe, "-C", str(bare_path), "fetch", "--depth=1", url, sha],
                env=env,
                check=True,
                capture_output=True,
                timeout=300,
            )

        try:
            execute_transport_plan(
                repo_url_base,
                bare_path,
                dep_ref=dep_ref,
                clone_action=_fetch_action_sha,
            )
            _scrub_fetch_head()
            if _rev_parse_present():
                _log.debug("fetch_sha_into_bare: shallow fetch of %s succeeded", sha[:12])
                return True
        except subprocess.CalledProcessError as exc:
            stderr_text = ""
            if exc.stderr:
                stderr_text = exc.stderr.decode(errors="replace").strip()
            _log.debug(
                "fetch_sha_into_bare: shallow fetch of %s failed: %s",
                sha[:12],
                stderr_text,
            )
        except Exception:
            _log.debug(
                "fetch_sha_into_bare: shallow fetch of %s raised unexpected error",
                sha[:12],
            )

    # Step 3: broaden shallow -- fetch all refs without a SHA argument.
    # Depth is capped to avoid unbounded history download on large repos.
    # Override via APM_BROAD_FETCH_DEPTH environment variable.
    broad_depth = os.environ.get("APM_BROAD_FETCH_DEPTH", "50")
    _log.info("Hydrating missing commit %s into shared bare for %s", sha[:12], repo_url_base)
    _log.debug("fetch_sha_into_bare: broadening shallow in %s to find %s", bare_path, sha[:12])

    def _fetch_action_broad(url: str, env: dict[str, str], target: Path) -> None:
        subprocess.run(
            [git_exe, "-C", str(bare_path), "fetch", f"--depth={broad_depth}", url],
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )

    try:
        execute_transport_plan(
            repo_url_base,
            bare_path,
            dep_ref=dep_ref,
            clone_action=_fetch_action_broad,
        )
        _scrub_fetch_head()
        if _rev_parse_present():
            _log.debug("fetch_sha_into_bare: broad fetch succeeded, %s now present", sha[:12])
            return True
    except subprocess.CalledProcessError as exc:
        stderr_text = ""
        if exc.stderr:
            stderr_text = exc.stderr.decode(errors="replace").strip()
        _log.debug(
            "fetch_sha_into_bare: broad fetch failed for %s in %s: %s",
            sha[:12],
            bare_path,
            stderr_text,
        )
    except Exception:
        _log.debug(
            "fetch_sha_into_bare: broad fetch raised unexpected error for %s",
            sha[:12],
        )

    _log.debug(
        "fetch_sha_into_bare: all fetch attempts exhausted for %s in %s",
        sha[:12],
        bare_path,
    )
    return False


def materialize_from_bare(
    bare_path: Path,
    consumer_dir: Path,
    *,
    ref: str | None,
    env: dict[str, str],
    known_sha: str | None = None,
) -> str:
    """Create a working-tree checkout from a bare repo via local-shared clone.

    Mirrors :class:`GitCache`'s ``_create_checkout`` pattern: each
    consumer gets its own working tree backed by the shared bare's
    object database (via ``objects/info/alternates``). Hardlink-cheap
    and concurrent-safe (consumer dirs are unique per call).

    SHA resolution policy (lifetime invariant 5.2.1):
      - If ``known_sha`` is provided (caller passed a 40-char SHA
        ref), use it directly. Avoids ``rev-parse HEAD`` which is
        ambiguous on init+fetch bares before update-ref runs.
      - Otherwise, resolve from the BARE via ``git --git-dir
        <bare> rev-parse HEAD``. NOT from the consumer - opening
        ``Repo(consumer_dir)`` leaks a Windows file handle that
        blocks downstream rmtree.

    CRLF + LFS pinning before checkout:
      - ``core.autocrlf=false`` guarantees byte-identical content
        across consumers regardless of the user's global git config.
      - ``filter.lfs.smudge=""`` + ``filter.lfs.required=false``
        disables LFS smudge cross-platform (the empty string trick
        works everywhere; ``cat`` is not on Windows PATH).

    Returns:
        The resolved commit SHA. Caller threads this into
        ``resolved_commit`` for the lockfile.
    """
    from ..utils.git_env import get_git_executable

    git_exe = get_git_executable()

    if known_sha:
        resolved_sha = known_sha
    else:
        sha_result = subprocess.run(
            [git_exe, "--git-dir", str(bare_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=True,
        )
        resolved_sha = sha_result.stdout.strip()

    consumer_dir.parent.mkdir(parents=True, exist_ok=True)
    # --no-checkout because we want to set core.autocrlf and disable
    # LFS smudge BEFORE checkout writes any file content.
    subprocess.run(
        [
            git_exe,
            "clone",
            "--local",
            "--shared",
            "--no-checkout",
            str(bare_path),
            str(consumer_dir),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    # CRLF determinism (panel: byte-identical across users).
    subprocess.run(
        [git_exe, "-C", str(consumer_dir), "config", "core.autocrlf", "false"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=True,
    )
    # Disable LFS smudge cross-platform: empty-string smudge is the
    # portable equivalent of `git lfs smudge --skip`. The `cat`
    # alternative is not on Windows PATH.
    for key, val in (
        ("filter.lfs.smudge", ""),
        ("filter.lfs.clean", ""),
        ("filter.lfs.process", ""),
        ("filter.lfs.required", "false"),
    ):
        subprocess.run(
            [git_exe, "-C", str(consumer_dir), "config", key, val],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=False,
        )
    checkout_target = known_sha or "HEAD"
    subprocess.run(
        [git_exe, "-C", str(consumer_dir), "checkout", checkout_target],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    return resolved_sha


def clone_with_fallback(
    execute_transport_plan: Callable[..., None],
    repo_url_base: str,
    target_path: Path,
    *,
    progress_reporter: Any = None,
    dep_ref: DependencyReference | None = None,
    verbose_callback: Callable[[str], None] | None = None,
    repo_cls: Any = None,
    **clone_kwargs: Any,
) -> Repo:
    """Clone a working-tree repository following the TransportSelector plan.

    Thin adapter over the caller-supplied ``execute_transport_plan``
    callable (typically ``self._execute_transport_plan``) that supplies
    a working-tree clone action (``Repo.clone_from``). Behavior is
    unchanged from the pre-#1126 implementation, except every clone
    attempt now begins with a robust ``_rmtree`` of the target
    for symmetry with the bare-clone path. This is strictly safer
    (clean slate per attempt) and matches the existing behavior on
    the second-and-subsequent attempts where target may contain a
    partial clone from the failed first attempt.

    Returns:
        The successfully cloned :class:`Repo`.

    Raises:
        RuntimeError: If the planned attempt(s) all fail.
    """
    repo_holder: list[Repo] = []
    _repo = repo_cls if repo_cls is not None else Repo

    def _wt_action(url: str, env: dict[str, str], target: Path) -> None:
        # Pre-attempt cleanup: GitPython's Repo.clone_from refuses a
        # non-empty target. Symmetric with _bare_action so retries
        # always start from a clean slate. Behavior change from the
        # pre-#1126 implementation - covered by 6.13.
        if target.exists():
            _rmtree(target)
        repo_holder.append(
            _repo.clone_from(
                url,
                target,
                env=env,
                progress=progress_reporter,
                **clone_kwargs,
            )
        )

    execute_transport_plan(
        repo_url_base,
        target_path,
        dep_ref=dep_ref,
        clone_action=_wt_action,
        verbose_callback=verbose_callback,
    )
    return repo_holder[0]


def build_clone_failure_message(
    *,
    repo_url_base: str,
    plan: Any,
    dep_ref: DependencyReference | None,
    dep_host: str | None,
    is_ado: bool,
    is_generic: bool,
    has_ado_token: bool,
    has_token: bool,
    auth_resolver: Any,
    configured_github_host: str,
    default_host_fn: Callable[[], str],
    last_error: Exception | None,
    sanitize_git_error: Callable[[str], str],
) -> str:
    """Build the aggregate ``RuntimeError`` message for a failed transport plan.

    Extracted from :meth:`GitHubPackageDownloader._execute_transport_plan`
    to keep that module under the file-length guardrail. Pure formatting:
    no I/O, no clone attempts.
    """
    if plan.strict and len(plan.attempts) >= 1:
        tried = plan.attempts[0].label
        error_msg = f"Failed to clone repository {repo_url_base} via {tried}. "
        if plan.fallback_hint:
            error_msg += plan.fallback_hint + " "
    else:
        error_msg = f"Failed to clone repository {repo_url_base} using all available methods. "
    if is_ado and not has_ado_token:
        host = dep_host or "dev.azure.com"
        error_msg += auth_resolver.build_error_context(
            host,
            "clone",
            org=dep_ref.ado_organization if dep_ref else None,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    elif is_generic:
        if dep_host:
            host_info = auth_resolver.classify_host(
                dep_host,
                port=dep_ref.port if dep_ref else None,
            )
            host_name = host_info.display_name
        else:
            host_name = "the target host"
        error_msg += (
            f"For private repositories on {host_name}, configure SSH keys or a git credential helper. "
            f"APM delegates authentication to git for non-GitHub/ADO hosts."
        )
    elif (
        configured_github_host
        and dep_host
        and dep_host == configured_github_host
        and configured_github_host != "github.com"
    ):
        suggested = f"github.com/{repo_url_base}"
        if dep_ref and dep_ref.virtual_path:
            suggested += f"/{dep_ref.virtual_path}"
        error_msg += (
            f"GITHUB_HOST is set to '{configured_github_host}', so shorthand dependencies "
            f"(without a hostname) resolve against that host. "
            f"If this package lives on a different server (e.g., github.com), "
            f"use the full hostname in apm.yml: {suggested}"
        )
    elif not has_token:
        host = dep_host or default_host_fn()
        org = dep_ref.repo_url.split("/")[0] if dep_ref and dep_ref.repo_url else None
        error_msg += auth_resolver.build_error_context(
            host,
            "clone",
            org=org,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    else:
        error_msg += "Please check repository access permissions and authentication setup."

    if last_error:
        sanitized_error = sanitize_git_error(str(last_error))
        error_msg += f" Last error: {sanitized_error}"

    return error_msg
