"""Private helpers for lockfile-cache and download-strategy resolution.

Extracted from ``integrate.py`` to keep that module under 500 lines.
All names are re-exported via ``integrate.py`` so existing import paths
and ``unittest.mock.patch`` targets remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.install.phases.heal import _HealChainOpts, run_heal_chain

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


@dataclass(frozen=True, slots=True)
class _LockfileCheckParams:
    """Parameter bundle for :func:`_check_lockfile_match`."""

    resolved_ref: Any
    update_refs: bool
    ref_changed: bool


def _check_git_or_content_hash(install_path: Path, locked_dep: Any) -> tuple[bool, bool]:
    """Check local HEAD SHA against *locked_dep*; fall back to content-hash on git failure.

    Returns ``(lockfile_match, via_content_hash_only)``.

    SHA *mismatch* returns ``(False, False)`` immediately -- the content-hash
    fallback is intentionally **not** triggered on mismatch, only on git
    failure (e.g. ``.git`` removed, virtual package subdirectory).
    """
    try:
        from git import Repo as GitRepo

        local_repo = GitRepo(install_path)
        if local_repo.head.commit.hexsha == locked_dep.resolved_commit:
            return True, False
        return False, False  # SHA mismatch -- do NOT fall to content-hash
    except Exception:
        pass
    # Git failed -- fall back to content-hash verification (#763)
    if locked_dep.content_hash and install_path.is_dir():
        from apm_cli.utils.content_hash import verify_package_hash

        if verify_package_hash(install_path, locked_dep.content_hash):
            return True, True
    return False, False


def _check_lockfile_match(
    install_path: Path,
    existing_lockfile: Any,
    dep_ref: Any,
    params: _LockfileCheckParams,
) -> tuple[bool, bool]:
    """Return ``(lockfile_match, lockfile_match_via_content_hash_only)``.

    Encapsulates Phase-5 cache logic (#171): skip download when the
    package at *install_path* already matches the lockfile SHA.
    """
    if not (install_path.exists() and existing_lockfile):
        return False, False
    locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
    if not (locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached"):
        return False, False
    if params.update_refs:
        # Update mode: remote ref must still resolve to the same commit,
        # then verify the local checkout matches.
        if not (
            params.resolved_ref
            and params.resolved_ref.resolved_commit == locked_dep.resolved_commit
        ):
            return False, False
        return _check_git_or_content_hash(install_path, locked_dep)
    elif not params.ref_changed:
        # Normal mode: compare local HEAD with lockfile SHA.
        return _check_git_or_content_hash(install_path, locked_dep)
    return False, False


def _resolve_download_strategy(
    ctx: InstallContext,
    dep_ref: Any,
    install_path: Path,
) -> tuple[Any, bool, Any, bool]:
    """Determine whether *dep_ref* can be served from cache.

    Returns ``(resolved_ref, skip_download, dep_locked_chk, ref_changed)``
    where *skip_download* is ``True`` when the package at *install_path*
    is already up-to-date.
    """
    from apm_cli.drift import detect_ref_change
    from apm_cli.models.apm_package import GitReferenceType
    from apm_cli.utils.path_security import safe_rmtree

    existing_lockfile = ctx.existing_lockfile
    update_refs = ctx.update_refs
    diagnostics = ctx.diagnostics
    logger = ctx.logger

    # npm-like behavior: Branches always fetch latest, only tags/commits use cache
    # Resolve git reference to determine type
    resolved_ref = None
    if dep_ref.get_unique_key() not in ctx.pre_downloaded_keys:
        # Resolve when there is an explicit ref, OR when update_refs
        # is True AND we have a non-cached lockfile entry to compare
        # against (otherwise resolution is wasted work -- the package
        # will be downloaded regardless).
        _has_lockfile_sha = False
        if update_refs and existing_lockfile:
            _lck = existing_lockfile.get_dependency(dep_ref.get_unique_key())
            _has_lockfile_sha = bool(
                _lck and _lck.resolved_commit and _lck.resolved_commit != "cached"
            )
        if dep_ref.reference or (update_refs and _has_lockfile_sha):
            try:  # noqa: SIM105
                resolved_ref = ctx.downloader.resolve_git_reference(dep_ref)
            except Exception:
                pass  # If resolution fails, skip cache (fetch latest)

    # Use cache only for tags and commits (not branches)
    is_cacheable = resolved_ref and resolved_ref.ref_type in [
        GitReferenceType.TAG,
        GitReferenceType.COMMIT,
    ]
    # Skip download if: already fetched by resolver callback, or cached tag/commit
    already_resolved = dep_ref.get_unique_key() in ctx.callback_downloaded
    # Detect if manifest ref changed vs what the lockfile recorded.
    # detect_ref_change() handles all transitions including None->ref.
    _dep_locked_chk = (
        existing_lockfile.get_dependency(dep_ref.get_unique_key()) if existing_lockfile else None
    )
    ref_changed = detect_ref_change(dep_ref, _dep_locked_chk, update_refs=update_refs)
    # Phase 5 (#171): Also skip when lockfile SHA matches local HEAD
    # -- but not when the manifest ref has changed (user wants different version).
    # Track whether lockfile_match was satisfied via content-hash fallback only
    # (no git HEAD verification possible -- typical for virtual packages, where
    # install_path is a carved-out subdirectory rather than a git repo).
    # The self-heal logic below uses this to recover from the v<=0.12.2
    # branch-ref drift bug for upgrading users.
    lockfile_match, lockfile_match_via_content_hash_only = _check_lockfile_match(
        install_path,
        existing_lockfile,
        dep_ref,
        _LockfileCheckParams(
            resolved_ref=resolved_ref, update_refs=update_refs, ref_changed=ref_changed
        ),
    )

    # Self-heal pipeline (PR #1158).
    #
    # All install-time heals (branch-ref drift detection, v<=0.12.2
    # buggy-lockfile recovery, future heals) live in
    # ``apm_cli.install.heals`` and are dispatched by ``run_heal_chain``.
    # Each heal is an isolated, individually-testable Chain-of-
    # Responsibility handler that may turn ``lockfile_match`` False,
    # set ``ref_changed`` True, and add a bypass key telling
    # ``FreshDependencySource`` that an upcoming content_hash change is
    # legitimate recovery, not a supply-chain attack.
    #
    # The dispatcher (not individual heals) renders user-facing
    # diagnostics + log messages, so heals stay pure and testable.
    lockfile_match, ref_changed = run_heal_chain(
        ctx,
        dep_ref,
        _HealChainOpts(
            resolved_ref=resolved_ref,
            existing_lockfile=existing_lockfile,
            lockfile_match=lockfile_match,
            lockfile_match_via_content_hash_only=lockfile_match_via_content_hash_only,
            update_refs=update_refs,
            ref_changed=ref_changed,
        ),
    )

    skip_download = install_path.exists() and (
        (is_cacheable and not update_refs)
        or (already_resolved and not update_refs)
        or lockfile_match
    )

    # Verify content integrity when lockfile has a hash
    if skip_download and _dep_locked_chk and _dep_locked_chk.content_hash:
        from apm_cli.utils.content_hash import verify_package_hash

        if not verify_package_hash(install_path, _dep_locked_chk.content_hash):
            _hash_msg = f"Content hash mismatch for {dep_ref.get_unique_key()} -- re-downloading"
            diagnostics.warn(_hash_msg, package=dep_ref.get_unique_key())
            if logger:
                logger.progress(_hash_msg)
            safe_rmtree(install_path, ctx.apm_modules_dir)
            skip_download = False

    # When registry-only mode is active, bypass cache if the
    # cached artifact was NOT previously downloaded via the
    # registry (no registry_prefix in lockfile). This handles
    # the transition from direct-VCS installs to proxy installs
    # for packages not yet in the lockfile.
    if (
        skip_download
        and ctx.registry_config
        and ctx.registry_config.enforce_only
        and not dep_ref.is_local
    ):
        if not _dep_locked_chk or _dep_locked_chk.registry_prefix is None:
            skip_download = False

    return resolved_ref, skip_download, _dep_locked_chk, ref_changed
