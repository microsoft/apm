# pylint: disable=duplicate-code
"""fetch_sha_into_bare: hydrate a specific SHA into an existing bare repo."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...models.apm_package import DependencyReference

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers (extracted from fetch_sha_into_bare to keep its
# McCabe complexity ≤ 20; previously nested closures).
# ---------------------------------------------------------------------------


def _rev_parse_present(git_exe: str, bare_path: Path, sha: str) -> bool:
    """Return True if *sha* is already reachable in the bare repo at *bare_path*."""
    try:
        # no env= needed -- purely local git plumbing, no network access
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


def _pin_sha_as_head_ref(git_exe: str, bare_path: Path, sha: str) -> None:
    """Add ``refs/heads/apm-pin-<sha-prefix>`` so the SHA is reachable via git-clone.

    ``git clone --local --shared`` from a *shallow* bare ignores
    ``--shared`` and falls back to the upload-pack protocol, which
    only transfers objects reachable from advertised refs.  A SHA
    fetched by :func:`fetch_sha_into_bare` is inserted into the
    object store but is *not* referenced by any ref, so the clone
    silently omits it and subsequent ``git checkout <sha>`` fails.

    Creating a synthetic ``refs/heads/apm-pin-*`` ref makes the
    commit reachable via the default ``refs/heads/*`` refspec, so
    upload-pack includes it.  Best-effort: a failure here is logged
    at DEBUG level and does not abort the install (the fallback is
    a fresh bare clone for the pinned package).
    """
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        _log.debug(
            "fetch_sha_into_bare: sha %r is not a valid 40-char hex SHA, skipping pin ref",
            sha,
        )
        return
    ref_name = f"refs/heads/apm-pin-{sha[:12]}"
    try:
        # no env= needed -- purely local git plumbing, no network access
        result = subprocess.run(
            [git_exe, "--git-dir", str(bare_path), "update-ref", ref_name, sha],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            _log.debug(
                "fetch_sha_into_bare: pinned %s as %s in %s",
                sha[:12],
                ref_name,
                bare_path,
            )
        else:
            _log.debug(
                "fetch_sha_into_bare: update-ref exited %d for %s in %s",
                result.returncode,
                sha[:12],
                bare_path,
            )
    except Exception as exc:
        _log.debug(
            "fetch_sha_into_bare: could not create pin ref for %s in %s: %s",
            sha[:12],
            bare_path,
            exc,
        )


def _scrub_fetch_head(bare_path: Path) -> None:
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FetchShaCtx:
    """Bundled context for :func:`_try_fetch_sha_step2` and :func:`_try_broaden_shallow`."""

    execute_transport_plan: Callable[..., None]
    git_exe: str
    repo_url_base: str
    bare_path: Path
    sha: str
    dep_ref: DependencyReference


def _try_fetch_sha_step2(ctx: _FetchShaCtx) -> bool:
    """Attempt a shallow fetch for the specific SHA (step 2). Return True on success."""

    def _fetch_action_sha(url: str, env: dict[str, str], target: Path) -> None:
        subprocess.run(
            [ctx.git_exe, "--git-dir", str(target), "fetch", "--depth=1", url, ctx.sha],
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )

    try:
        ctx.execute_transport_plan(
            ctx.repo_url_base,
            ctx.bare_path,
            dep_ref=ctx.dep_ref,
            clone_action=_fetch_action_sha,
        )
        _scrub_fetch_head(ctx.bare_path)
        if _rev_parse_present(ctx.git_exe, ctx.bare_path, ctx.sha):
            _log.debug("fetch_sha_into_bare: shallow fetch of %s succeeded", ctx.sha[:12])
            _pin_sha_as_head_ref(ctx.git_exe, ctx.bare_path, ctx.sha)
            return True
    except subprocess.CalledProcessError as exc:
        stderr_text = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        _log.debug(
            "fetch_sha_into_bare: shallow fetch of %s failed: %s",
            ctx.sha[:12],
            stderr_text,
        )
    except Exception:
        _log.debug(
            "fetch_sha_into_bare: shallow fetch of %s raised unexpected error",
            ctx.sha[:12],
        )
    return False


def _try_broaden_shallow(ctx: _FetchShaCtx) -> bool:
    """Try broadening the shallow clone to expose a previously-unreachable SHA. Return True on success."""
    # Depth is capped to avoid unbounded history download on large repos.
    # Override via APM_BROAD_FETCH_DEPTH environment variable.
    broad_depth = os.environ.get("APM_BROAD_FETCH_DEPTH", "50")
    _log.info(
        "Hydrating missing commit %s into shared bare for %s", ctx.sha[:12], ctx.repo_url_base
    )
    _log.debug(
        "fetch_sha_into_bare: broadening shallow in %s to find %s", ctx.bare_path, ctx.sha[:12]
    )

    def _fetch_action_broad(url: str, env: dict[str, str], target: Path) -> None:
        subprocess.run(
            [ctx.git_exe, "--git-dir", str(target), "fetch", f"--depth={broad_depth}", url],
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )

    try:
        ctx.execute_transport_plan(
            ctx.repo_url_base,
            ctx.bare_path,
            dep_ref=ctx.dep_ref,
            clone_action=_fetch_action_broad,
        )
        _scrub_fetch_head(ctx.bare_path)
        if _rev_parse_present(ctx.git_exe, ctx.bare_path, ctx.sha):
            _log.debug("fetch_sha_into_bare: broad fetch succeeded, %s now present", ctx.sha[:12])
            _pin_sha_as_head_ref(ctx.git_exe, ctx.bare_path, ctx.sha)
            return True
    except subprocess.CalledProcessError as exc:
        stderr_text = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        _log.debug(
            "fetch_sha_into_bare: broad fetch failed for %s in %s: %s",
            ctx.sha[:12],
            ctx.bare_path,
            stderr_text,
        )
    except Exception:
        _log.debug(
            "fetch_sha_into_bare: broad fetch raised unexpected error for %s",
            ctx.sha[:12],
        )
    return False


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
    from ...utils.git_env import get_git_executable

    git_exe = get_git_executable()

    # Step 1: check first -- no network if SHA already present.
    _log.debug("fetch_sha_into_bare: checking if %s is present in %s", sha[:12], bare_path)
    if _rev_parse_present(git_exe, bare_path, sha):
        _log.debug("fetch_sha_into_bare: SHA %s already present, skipping fetch", sha[:12])
        _pin_sha_as_head_ref(git_exe, bare_path, sha)
        return True

    _ctx = _FetchShaCtx(
        execute_transport_plan=execute_transport_plan,
        git_exe=git_exe,
        repo_url_base=repo_url_base,
        bare_path=bare_path,
        sha=sha,
        dep_ref=dep_ref,
    )

    # Step 2: shallow fetch by full SHA (only for full 40-char SHAs).
    if len(sha) == 40:
        _log.debug(
            "fetch_sha_into_bare: attempting shallow fetch of %s into %s", sha[:12], bare_path
        )
        if _try_fetch_sha_step2(_ctx):
            return True

    # Step 3: broaden shallow -- fetch all refs without a SHA argument.
    if _try_broaden_shallow(_ctx):
        return True

    _log.debug(
        "fetch_sha_into_bare: all fetch attempts exhausted for %s in %s",
        sha[:12],
        bare_path,
    )
    return False
