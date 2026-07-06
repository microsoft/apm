"""Stage-and-restore mechanism for ``apm update``'s plan-confirmation gate.

``download_callback`` (see ``resolve.py``) materialises a re-resolved
semver dep's new content to disk as part of *resolving* the dependency
graph -- this is unavoidable, since discovering a package's transitive
deps requires reading its manifest. But ``apm update`` shows the computed
plan and asks for confirmation only *after* resolve completes. Left alone,
that means a declined confirmation, a non-interactive abort (no TTY, no
``--yes``), or ``--dry-run`` all leave ``apm_modules/`` already advanced to
the new version while ``apm.lock.yaml`` stays on the old one.

This module closes that gap: ``purge_cached_semver_paths_for_update``
moves a semver dep's existing install path aside (instead of deleting it)
so the resolver is still forced through ``download_callback`` to
re-resolve, and ``restore_update_backups`` reconciles the outcome once the
plan-confirmation gate resolves -- discarding the backups on commit, or
restoring them (and removing any freshly-added content) otherwise.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.utils.file_ops import robust_rmtree as _rrm

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.models.dependency.reference import DependencyReference

# Short enough to keep backup directory names readable, long enough that an
# accidental collision between two sanitized-but-distinct dep keys is not a
# realistic concern.
_DISAMBIGUATOR_LEN = 8


def _sanitize_backup_name(dep_key: str) -> str:
    """Turn a dep key into a filesystem-safe, collision-resistant directory name.

    Sanitizing alone is not injective: ``"owner/repo"``, ``"owner_repo"``, and
    ``"owner:repo"`` would all collapse to the same ``"owner_repo"`` name,
    which risks one dep's backup silently overwriting another's (or restoring
    the wrong content). A short hash of the original, un-sanitized key is
    appended to keep distinct keys apart even after sanitization.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", dep_key)
    digest = hashlib.sha256(dep_key.encode("utf-8")).hexdigest()[:_DISAMBIGUATOR_LEN]
    return f"{safe}-{digest}"


def purge_cached_semver_paths_for_update(
    *,
    all_apm_deps: list[DependencyReference],
    apm_modules_dir: Path,
    logger: Any,
    backup_root: Path | None = None,
) -> dict[str, Path]:
    """Pre-purge on-disk install paths for direct git-source and registry semver deps
    when ``--update`` / ``--refresh`` is set.

    Bug 1 fix (#1496): the BFS resolver short-circuits at
    ``install_path.exists()`` and never invokes ``download_callback``,
    which is where ``_maybe_resolve_git_semver`` lives. For git-source
    semver direct deps we therefore pre-purge the install path so the
    resolver is forced through the callback, re-runs ``git ls-remote``,
    and rewrites the lockfile with the latest matching tag. Matches
    npm / cargo / bundler: ``--update`` is the explicit re-resolve
    trigger and must not be swallowed by the on-disk cache. Scoped to
    direct deps to avoid disturbing transitive cached content; the
    resolver re-walks transitives naturally once a direct dep's
    callback rewrites its ref. Local and proxy deps are excluded (their
    semver semantics belong to a different resolver path). Registry semver
    deps are included: their callback also gates on install_path.exists().

    When *backup_root* is given, the existing content is moved there
    instead of being deleted outright, and the returned dict maps
    ``dep_key -> backup_path`` so a caller with a plan-confirmation gate
    (``apm update``) can restore it if the plan is ultimately declined --
    see ``restore_update_backups``. When *backup_root* is ``None`` (e.g.
    ``apm install --update``, which has no decline path), the old
    delete-outright behaviour is unchanged.
    """
    backups: dict[str, Path] = {}
    for _dep in all_apm_deps:
        if getattr(_dep, "ref_kind", None) != "semver":
            continue
        if _dep.is_local:
            continue
        if getattr(_dep, "artifactory_prefix", None):
            continue
        try:
            _ip = _dep.get_install_path(apm_modules_dir)
        except Exception:  # noqa: S112
            # Path computation failure (e.g. malformed dep) is non-fatal
            # here -- the resolver will surface a real error downstream.
            continue
        if not _ip.exists():
            continue
        _cleared = False
        if backup_root is not None:
            _dep_key = _dep.get_unique_key()
            _backup_path = backup_root / _sanitize_backup_name(_dep_key)
            with suppress(Exception):
                if _backup_path.exists():
                    _rrm(_backup_path, ignore_errors=True)
                _backup_path.parent.mkdir(parents=True, exist_ok=True)
                _ip.rename(_backup_path)
                backups[_dep_key] = _backup_path
                _cleared = True
        else:
            with suppress(Exception):
                _rrm(_ip, ignore_errors=True)
                _cleared = True
        # Only claim the path was cleared when the rename/rmtree actually
        # succeeded -- a swallowed exception above must not be reported as
        # a successful purge, which would mislead a verbose user into
        # thinking semver re-resolution will occur when it may not.
        if logger and _cleared:
            logger.verbose_detail(
                f"[*] --update: cleared cached install path for "
                f"{_dep.get_unique_key()} to force semver re-resolution"
            )
    return backups


def restore_update_backups(ctx: InstallContext, *, keep_new: bool) -> None:
    """Reconcile ``ctx.update_backups`` after the plan-confirmation gate resolves.

    When *keep_new* is True (the update was confirmed and applied) AND the
    dep was actually re-downloaded this run, the fresh content stays in
    place and its backup is discarded. Every other backed-up dep -- either
    because *keep_new* is False (declined, non-interactive abort, or
    ``--dry-run``), or because it was purged but never actually
    re-resolved (e.g. a failure elsewhere aborted the run first) -- has
    its original content moved back into place. When not committing, a
    dep with no prior backup that was nonetheless downloaded this run (a
    fresh add swept up by this resolve pass) has its new content removed
    entirely. This is what keeps a declined/aborted/dry-run ``apm update``
    from silently leaving ``apm_modules/`` ahead of ``apm.lock.yaml``.

    Deps are looked up via ``ctx.all_apm_deps`` merged with
    ``ctx.deps_to_install`` rather than ``ctx.deps_to_install`` alone: the
    latter (the full transitive closure) is only populated after the BFS
    resolver returns successfully, so if resolution itself raises (a
    network error, a bad transitive manifest, etc.) after some direct deps
    were already purged, ``deps_to_install`` -- and ``callback_downloaded``
    -- can still be empty. ``all_apm_deps`` (direct deps only, but
    populated before resolve even starts) guarantees every dep this module
    could have purged is still resolvable even in that early-failure case;
    ``deps_to_install``, when available, extends coverage to transitive
    deps swept into the same update pass.
    """
    # Coerced with isinstance rather than a plain ``or {}`` fallback: a
    # loosely-mocked ctx (e.g. a bare MagicMock() in an unrelated pipeline
    # test) has these as auto-generated, always-truthy Mock attributes when
    # unset, which would otherwise slip past ``... or {}`` and break the
    # dict/list operations below.
    _raw_backups = getattr(ctx, "update_backups", None)
    backups: dict[str, Path] = _raw_backups if isinstance(_raw_backups, dict) else {}
    if not backups and keep_new:
        return
    _raw_downloaded = getattr(ctx, "callback_downloaded", None)
    downloaded = _raw_downloaded if isinstance(_raw_downloaded, dict) else {}
    _raw_all_deps = getattr(ctx, "all_apm_deps", None)
    _raw_deps_to_install = getattr(ctx, "deps_to_install", None)
    dep_by_key = {
        d.get_unique_key(): d for d in (_raw_all_deps if isinstance(_raw_all_deps, list) else [])
    }
    dep_by_key.update(
        {
            d.get_unique_key(): d
            for d in (_raw_deps_to_install if isinstance(_raw_deps_to_install, list) else [])
        }
    )
    apm_modules_dir = ctx.apm_modules_dir

    for _dep_key, _backup_path in backups.items():
        if keep_new and _dep_key in downloaded:
            # New content committed -- the backup is no longer needed.
            with suppress(Exception):
                if _backup_path.exists():
                    _rrm(_backup_path, ignore_errors=True)
            continue
        # Not committed, or this dep was purged but never actually
        # re-resolved (e.g. an earlier failure aborted the run) -- restore
        # the original content.
        _dep = dep_by_key.get(_dep_key)
        if _dep is None:
            continue
        with suppress(Exception):
            _ip = _dep.get_install_path(apm_modules_dir)
            if _ip.exists():
                _rrm(_ip, ignore_errors=True)
            if _backup_path.exists():
                _ip.parent.mkdir(parents=True, exist_ok=True)
                _backup_path.rename(_ip)

    if not keep_new:
        # Freshly-downloaded deps with no prior backup (new adds swept into
        # this update pass) never existed before -- remove them outright.
        for _dep_key in downloaded:
            if _dep_key in backups:
                continue
            _dep = dep_by_key.get(_dep_key)
            if _dep is None:
                continue
            with suppress(Exception):
                _ip = _dep.get_install_path(apm_modules_dir)
                if _ip.exists():
                    _rrm(_ip, ignore_errors=True)

    if backups:
        _backup_root = next(iter(backups.values())).parent
        with suppress(Exception):
            if _backup_root.is_dir() and not any(_backup_root.iterdir()):
                _backup_root.rmdir()
