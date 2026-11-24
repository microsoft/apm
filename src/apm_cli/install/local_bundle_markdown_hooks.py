"""Bundle Markdown whose frontmatter declares hooks, retired with its approval (#5254).

An approved local bundle may deploy Markdown files (a command ``.md``, a
``SKILL.md``) whose frontmatter declares ``hooks`` (#5248). Install records
those files with the bundle's exact-content approval key in the scope's
``apm_modules/``; every later install, update and prune re-checks the key
and retires the recorded files when current trust no longer grants it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

_RECORD = Path("apm_modules") / ".local-bundle-markdown-hooks.json"


def _record_path(scope_dir: Path) -> Path:
    return Path(scope_dir) / _RECORD


def _read(scope_dir: Path) -> dict[str, dict]:
    try:
        data = json.loads(_record_path(scope_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    bundles = data.get("bundles") if isinstance(data, dict) else None
    return bundles if isinstance(bundles, dict) else {}


def _write(scope_dir: Path, bundles: dict[str, dict]) -> None:
    from ..integration import write_seam
    from ..utils.path_security import PathTraversalError, has_symlink_component

    path = _record_path(scope_dir)
    if has_symlink_component(Path(scope_dir), path):
        raise PathTraversalError(f"Refusing to write {_RECORD.as_posix()} through a linked path")
    if not bundles:
        write_seam.unlink(path, missing_ok=True)
        return
    write_seam.mkdir(path.parent, parents=True, exist_ok=True)
    write_seam.atomic_write_text(
        path, json.dumps({"bundles": bundles}, indent=2, sort_keys=True) + "\n"
    )


def _spelled(project_root: Path, value: str) -> Path:
    return Path(value) if Path(value).is_absolute() else Path(project_root) / value


def _scope_dir(scope: Any, project_root: Path) -> Path:
    from ..core.scope import InstallScope, get_apm_dir

    return get_apm_dir(InstallScope.USER) if scope == InstallScope.USER else Path(project_root)


def record_bundle_markdown_hooks(
    scope: Any,
    project_root: Path,
    slug: str,
    approval_key: str | None,
    deployed: Collection[str],
    dry_run: bool,
) -> None:
    """Record the hooked Markdown this install of *slug* deployed, with its key.

    Hooked Markdown deploys only under an approved key (#5248), so any it
    deployed was admitted by *approval_key*. An install that deployed none
    keeps the earlier record, so a withdrawn key still retires what it
    admitted; a dry run records nothing.
    """
    if dry_run or not approval_key:
        return
    from .local_bundle_paths import markdown_declares_hooks

    files = sorted(
        value
        for value in deployed
        if value.lower().endswith(".md") and markdown_declares_hooks(_spelled(project_root, value))
    )
    if not files:
        return
    scope_dir = _scope_dir(scope, project_root)
    bundles = _read(scope_dir)
    bundles[slug] = {"approval_key": approval_key, "files": files}
    _write(scope_dir, bundles)


def _remove_unedited(project_root: Path, files: Collection[str], recorded: dict[str, str]):
    """Remove each recorded hooked file whose bytes still match the lock.

    Returns ``(removed, kept)``; a recorded file that no longer declares hooks
    is in neither, so it leaves the record but keeps its file and lock row.
    """
    from ..integration import write_seam
    from ..utils.content_hash import compute_file_hash
    from ..utils.path_security import has_symlink_component
    from .local_bundle_paths import markdown_declares_hooks

    removed: set[str] = set()
    kept: set[str] = set()
    for value in files:
        path = _spelled(project_root, value)
        if not path.exists() and not path.is_symlink():
            removed.add(value)
            continue
        if has_symlink_component(Path(project_root), path) or not path.is_file():
            kept.add(value)
            continue
        if not markdown_declares_hooks(path):
            continue  # no longer hooked (a newer bundle replaced it): file and row stay
        if recorded.get(value) not in (None, compute_file_hash(path)):
            kept.add(value)  # edited outside APM: the user's bytes stay
            continue
        try:
            write_seam.unlink(path)
        except OSError:
            kept.add(value)
            continue
        removed.add(value)
    return removed, kept


def retire_unadmitted_bundle_markdown(
    project_root: Path,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    *,
    lock_dir: Path,
    logger: Any = None,
    dry_run: bool = False,
) -> list[str]:
    """Retire recorded hooked Markdown whose bundle key trust no longer grants.

    Returns the slugs whose Markdown was retired (or, on a dry run, would be).
    A file edited outside APM is kept, stays recorded, and is named.
    """
    bundles = _read(lock_dir)
    if not bundles:
        return []
    allow_map = allow_executables()
    from ..core.remediation import BUNDLE_HOOKS_RETIRED
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..security.executables import (
        EXEC_TYPE_HOOKS,
        exec_map_authorizes_all,
        package_type_approved,
    )
    from .local_bundle_hook_trust import _drop_lock_rows

    if exec_map_authorizes_all(allow_map):
        return []
    lockfile = LockFile.read(get_lockfile_path(lock_dir))
    recorded = dict(lockfile.local_deployed_file_hashes) if lockfile is not None else {}
    retired: list[str] = []
    for slug, entry in sorted(bundles.items()):
        key = entry.get("approval_key") if isinstance(entry, dict) else None
        files = [str(f) for f in entry.get("files") or []] if isinstance(entry, dict) else []
        if isinstance(key, str) and package_type_approved(allow_map, (key,), EXEC_TYPE_HOOKS):
            continue
        retired.append(slug)
        if dry_run:
            if logger is not None:
                logger.warning(
                    f"Would retire {len(files)} Markdown file(s) with frontmatter hooks "
                    f"from local bundle '{slug}'."
                )
            continue
        removed, kept = _remove_unedited(project_root, files, recorded)
        if removed:
            _drop_lock_rows(lock_dir, removed)
        if kept:
            bundles[slug] = {"approval_key": key, "files": sorted(kept)}
        else:
            del bundles[slug]
        if logger is not None:
            message = (
                f"Retired {len(removed)} Markdown file(s) with frontmatter hooks from local "
                f"bundle '{slug}' (approval no longer granted). "
                f"To keep them, {BUNDLE_HOOKS_RETIRED.clause()}."
            )
            if kept:
                message += f" Kept edited file(s): {', '.join(sorted(kept))}."
            logger.warning(message)
    if retired and not dry_run:
        _write(lock_dir, bundles)
    return retired
