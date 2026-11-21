"""Gate a local bundle's canvas extensions like its hooks and MCP servers.

Canvas extensions are executable Node modules (``extension.mjs``). A local
bundle copies its files verbatim, not through ``CanvasIntegrator``, so the
experimental feature flag and the executables gate are checked here. When the
gate does not admit canvas for the exact bundle content (no approval, or the
bundle is denied), the extensions this bundle deployed earlier are retired:
unedited files are removed and their lockfile rows dropped, as hook scripts
are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def gate_bundle_canvas(
    pack_files: dict[str, str],
    project_root: Path,
    *,
    targets: Any,
    slug: str,
    approval_key: str | None,
    allow_executables: Any,
    create_config: bool,
    scope: Any,
    dry_run: bool,
    report: Any,
) -> tuple[bool, int]:
    """Return whether canvas deploys for this bundle, and how many files were blocked.

    Blocked canvas paths are removed from *pack_files*. *report* is a
    one-argument warning sink.
    """
    from ..core.experimental import is_enabled
    from ..core.scope import InstallScope
    from ..integration.canvas_integrator import is_canvas_bundle_path
    from ..security.executables import EXEC_TYPE_CANVAS, package_type_approved

    enabled = is_enabled("canvas", create_config=create_config)
    keys = () if approval_key is None else (approval_key,)
    if enabled and package_type_approved(allow_executables, keys, EXEC_TYPE_CANVAS):
        return True, 0
    blocked = sorted(rel for rel in pack_files if is_canvas_bundle_path(rel))
    for rel in blocked:
        pack_files.pop(rel, None)
    if not (enabled and blocked):
        return False, 0
    user_scope = scope is InstallScope.USER
    retired = not dry_run and retire_bundle_canvas(
        project_root, targets, canvas_names(blocked), user_scope=user_scope
    )
    lead = (
        f"Retired canvas extension(s) from local bundle '{slug}'"
        if retired
        else f"Blocked {len(blocked)} canvas extension file(s) from bundle '{slug}'"
    )
    report(f"{lead}: {_reason(allow_executables, approval_key, user_scope=user_scope)}")
    return False, len(blocked)


def _reason(allow_executables: Any, approval_key: str | None, *, user_scope: bool) -> str:
    from ..security.executables import local_bundle_approval_hint
    from .local_bundle_handler import bundle_denied_hint, bundle_type_denied

    if approval_key is not None and bundle_type_denied(allow_executables, approval_key, "canvas"):
        return bundle_denied_hint(approval_key, "canvas", user_scope=user_scope)
    return (
        "canvas extensions are executable extension.mjs code and are not approved for "
        "this exact bundle content. "
        + local_bundle_approval_hint(str(approval_key), "canvas", user_scope=user_scope)
    )


def canvas_names(paths: list[str]) -> frozenset[str]:
    """Return the extension directory names of bundle canvas *paths*."""
    names: set[str] = set()
    for rel in paths:
        parts = [seg for seg in rel.replace("\\", "/").split("/") if seg]
        for index, seg in enumerate(parts[:-1]):
            if seg.lower() == "extensions":
                names.add(parts[index + 1])
                break
    return frozenset(names)


def retire_bundle_canvas(
    project_root: Path, targets: Any, names: frozenset[str], *, user_scope: bool
) -> bool:
    """Remove the canvas extensions named *names* that local bundles deployed.

    Only local-bundle lock rows under a target's canvas root are touched; an
    edited file is kept, its row dropped. Returns whether any row was retired.
    """
    from ..core.scope import InstallScope, get_apm_dir
    from ..deps.lockfile import LockFile, get_lockfile_path
    from .audit_target_roots import canvas_deploy_prefixes
    from .local_bundle_hook_trust import (
        _drop_lock_rows,
        _lock_bundle_paths,
        _relative_value,
        _remove_unedited,
    )

    prefixes = tuple(
        f"{root}{name}/" for root in canvas_deploy_prefixes(list(targets)) for name in names
    )
    lock_dir = get_apm_dir(InstallScope.USER) if user_scope else project_root
    rows = {
        value
        for value in _lock_bundle_paths(lock_dir)
        if prefixes and _relative_value(value, project_root).startswith(prefixes)
    }
    if not rows:
        return False
    lockfile = LockFile.read(get_lockfile_path(lock_dir))
    recorded = dict(lockfile.local_deployed_file_hashes) if lockfile is not None else {}
    _remove_unedited(project_root, rows, recorded)
    _drop_lock_rows(lock_dir, rows)
    return True
