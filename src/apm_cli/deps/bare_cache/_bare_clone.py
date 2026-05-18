"""bare_clone_with_fallback: 3-tier bare-repo clone via transport-plan executor."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._scrub import _rmtree, _scrub_bare_remote_url

if TYPE_CHECKING:
    from ...models.apm_package import DependencyReference


@dataclass(frozen=True, slots=True)
class BareCloneOpts:
    """Keyword-only arguments for :func:`bare_clone_with_fallback`."""

    dep_ref: DependencyReference
    ref: str | None
    is_commit_sha: bool


def bare_clone_with_fallback(
    execute_transport_plan: Callable[..., None],
    repo_url_base: str,
    bare_target: Path,
    opts: BareCloneOpts,
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
    from ...utils.git_env import get_git_executable

    git_exe = get_git_executable()
    dep_ref = opts.dep_ref
    ref = opts.ref
    is_commit_sha = opts.is_commit_sha

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
                        [git_exe, "--git-dir", str(target), "remote", "add", "origin", url],
                        env=env,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        [git_exe, "--git-dir", str(target), "fetch", "--depth=1", "origin", ref],
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
                        [git_exe, "--git-dir", str(target), "update-ref", "HEAD", ref],
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
                    "--git-dir",
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
                [git_exe, "--git-dir", str(target), "update-ref", "HEAD", full_sha],
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
