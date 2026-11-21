"""Gate a local bundle's canvas extensions like its hooks and MCP servers.

Canvas extensions are executable Node modules (``extension.mjs``). A local
bundle copies its files verbatim, not through ``CanvasIntegrator``, so the
experimental feature flag and the executables gate are checked here.

An install that deploys a bundle's extensions records them with the bundle's
slug and the exact-content key that admitted them, beside its hooks record
(:func:`~apm_cli.integration.hook_ownership.write_per_file_bundle_record`).
Every ``apm install``, ``apm update`` and ``apm prune`` re-checks those
records against current executables trust and retires what it no longer
admits, and ``apm audit --ci`` reports it first, as for bundle hooks.
Retirement touches only the rows of the bundle that recorded them; a file
whose bytes no longer match its recorded hash is kept, and a row with no
recorded hash is kept with its file.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BundleCanvas:
    """The canvas files one local bundle deployed, with the keys that admitted them.

    ``recorded`` is ``False`` for local-bundle canvas rows no bundle record
    claims (deployed before the record existed); ``slug`` then names the
    extension directory.
    """

    slug: str
    recorded: bool
    keys: set[str | None] = field(default_factory=set)
    files: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class UnadmittedBundleCanvas:
    """One local bundle's deployed canvas extensions current trust does not admit."""

    slug: str
    reason: str
    files: tuple[str, ...] = ()
    recorded: bool = True


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
    retired = not dry_run and retire_recorded_bundle_canvas(
        project_root, targets, slug, user_scope=user_scope
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


def canvas_names(paths: Collection[str]) -> frozenset[str]:
    """Return the extension directory names of bundle canvas *paths*."""
    names: set[str] = set()
    for rel in paths:
        parts = [seg for seg in rel.replace("\\", "/").split("/") if seg]
        for index, seg in enumerate(parts[:-1]):
            if seg.lower() == "extensions":
                names.add(parts[index + 1])
                break
    return frozenset(names)


def _canvas_targets(targets: Any) -> list[Any]:
    return [
        target
        for target in targets or ()
        if getattr(target, "primitives", {}).get("canvas") is not None
    ]


def _sidecar(project_root: Path, target: Any) -> Path:
    from ..integration.hook_integrator import _APM_HOOKS_SIDECAR

    return Path(project_root) / target.root_dir / _APM_HOOKS_SIDECAR


def _target_prefixes(target: Any) -> tuple[str, ...]:
    from .audit_target_roots import canvas_deploy_prefixes

    return tuple(sorted(canvas_deploy_prefixes([target])))


def record_bundle_canvas(
    project_root: Path,
    targets: Any,
    slug: str,
    approval_key: str | None,
    deployed: Collection[str],
) -> None:
    """Record the canvas files this install of bundle *slug* deployed, with its key."""
    from ..integration.hook_ownership import BUNDLE_RECORD_CANVAS, write_per_file_bundle_record
    from .local_bundle_hook_trust import _relative_value

    root = Path(project_root)
    values = [_relative_value(value, root) for value in deployed]
    for target in _canvas_targets(targets):
        prefixes = _target_prefixes(target)
        files = sorted(value for value in values if prefixes and value.startswith(prefixes))
        sidecar = _sidecar(root, target)
        if files or sidecar.is_file():
            write_per_file_bundle_record(
                sidecar, slug, approval_key, files, kind=BUNDLE_RECORD_CANVAS
            )


def deployed_bundle_canvas(
    project_root: Path,
    targets: Any,
    bundle_paths: Collection[str] | Callable[[], Collection[str]],
) -> list[BundleCanvas]:
    """Return the canvas files each local bundle deployed, as the lockfile still claims.

    A record names a bundle's files; lockfile local-bundle canvas rows no
    record claims are grouped by extension directory, unrecorded.
    """
    from ..integration.hook_ownership import BUNDLE_RECORD_CANVAS, per_file_bundle_hook_keys
    from .local_bundle_hook_trust import _relative_value

    canvas_targets = _canvas_targets(targets)
    if not canvas_targets:
        return []
    root = Path(project_root)
    paths = bundle_paths() if callable(bundle_paths) else bundle_paths
    claimed = {_relative_value(value, root) for value in paths}
    prefixes = tuple(prefix for target in canvas_targets for prefix in _target_prefixes(target))
    found: dict[tuple[str, bool], BundleCanvas] = {}
    recorded: set[str] = set()
    for target in canvas_targets:
        try:
            records = per_file_bundle_hook_keys(_sidecar(root, target), BUNDLE_RECORD_CANVAS)
        except (OSError, ValueError, TypeError):
            records = {}
        for slug, (key, files) in records.items():
            held = claimed.intersection(files)
            recorded.update(files)
            if held:
                holding = found.setdefault((slug, True), BundleCanvas(slug, True))
                holding.keys.add(key)
                holding.files.update(held)
    for value in sorted(claimed - recorded):
        if prefixes and value.startswith(prefixes):
            name = next(iter(canvas_names([value])), value)
            holding = found.setdefault((name, False), BundleCanvas(name, False))
            holding.keys.add(None)
            holding.files.add(value)
    return [found[key] for key in sorted(found)]


def unadmitted_bundle_canvas(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    bundle_paths: Collection[str] | Callable[[], Collection[str]] = (),
) -> list[UnadmittedBundleCanvas]:
    """Return deployed local-bundle canvas extensions the current trust does not admit.

    *allow_executables* returns the effective deny-wins map and is asked only
    when a bundle has deployed canvas files. A gate-off map admits them all,
    as installs do; a missing map admits none.
    """
    deployed = deployed_bundle_canvas(project_root, targets, bundle_paths)
    if not deployed:
        return []
    allow_map = allow_executables()
    from ..security.executables import (
        EXEC_TYPE_CANVAS,
        exec_map_authorizes_all,
        package_type_approved,
    )
    from .local_bundle_hook_trust import REASON_NOT_GRANTED, REASON_UNRECORDED

    if exec_map_authorizes_all(allow_map):
        return []
    result: list[UnadmittedBundleCanvas] = []
    for holding in deployed:
        files = tuple(sorted(holding.files))
        if None in holding.keys:
            reason = REASON_UNRECORDED
        elif not all(
            package_type_approved(allow_map, (key,), EXEC_TYPE_CANVAS) for key in holding.keys
        ):
            reason = REASON_NOT_GRANTED
        else:
            continue
        result.append(UnadmittedBundleCanvas(holding.slug, reason, files, holding.recorded))
    return result


def retire_unadmitted_bundle_canvas(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    *,
    lock_dir: Path | None,
    logger: Any = None,
    dry_run: bool = False,
) -> list[UnadmittedBundleCanvas]:
    """Retire every local-bundle canvas extension trust no longer admits."""
    from ..core.remediation import BUNDLE_HOOKS_APPROVED
    from .local_bundle_hook_trust import _lock_bundle_paths

    unadmitted = unadmitted_bundle_canvas(
        project_root, targets, allow_executables, lambda: _lock_bundle_paths(lock_dir)
    )
    for item in unadmitted:
        if not dry_run:
            _retire(project_root, targets, item, lock_dir=lock_dir)
        if logger is not None:
            lead = "Would retire" if dry_run else "Retired"
            logger.warning(
                f"{lead} canvas extension(s) from local bundle '{item.slug}' ({item.reason}). "
                f"To keep them, {BUNDLE_HOOKS_APPROVED.clause()}."
            )
    return unadmitted


def retire_recorded_bundle_canvas(
    project_root: Path, targets: Any, slug: str, *, user_scope: bool
) -> bool:
    """Retire the canvas files bundle *slug* recorded; another bundle's stay.

    Returns whether any lockfile row was retired.
    """
    from ..core.scope import InstallScope, get_apm_dir
    from .local_bundle_hook_trust import _lock_bundle_paths

    lock_dir = get_apm_dir(InstallScope.USER) if user_scope else Path(project_root)
    holdings = deployed_bundle_canvas(project_root, targets, _lock_bundle_paths(lock_dir))
    item = next((h for h in holdings if h.recorded and h.slug == slug), None)
    if item is None:
        return False
    return _retire(
        project_root,
        targets,
        UnadmittedBundleCanvas(slug, "", tuple(sorted(item.files))),
        lock_dir=lock_dir,
    )


def _retire(
    project_root: Path, targets: Any, item: UnadmittedBundleCanvas, *, lock_dir: Path | None
) -> bool:
    """Remove one holding's unedited files, drop their rows and the bundle's record."""
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..integration.hook_ownership import BUNDLE_RECORD_CANVAS, write_per_file_bundle_record
    from ..utils.content_hash import compute_file_hash
    from .local_bundle_hook_trust import _drop_lock_rows, _lock_bundle_paths, _relative_value

    root = Path(project_root)
    files = set(item.files)
    rows = {
        value for value in _lock_bundle_paths(lock_dir) if _relative_value(value, root) in files
    }
    lockfile = LockFile.read(get_lockfile_path(lock_dir)) if lock_dir is not None else None
    recorded = dict(lockfile.local_deployed_file_hashes) if lockfile is not None else {}
    proven = {value for value in rows if recorded.get(value)}
    for value in proven:
        path = Path(value) if Path(value).is_absolute() else root / value
        if path.is_file() and not path.is_symlink() and compute_file_hash(path) == recorded[value]:
            path.unlink()
    if proven and lock_dir is not None:
        _drop_lock_rows(lock_dir, proven)
    # A row with no recorded hash is unproven: its file, row and record stay.
    if item.recorded and proven == rows:
        for target in _canvas_targets(targets):
            sidecar = _sidecar(root, target)
            if sidecar.is_file():
                write_per_file_bundle_record(
                    sidecar, item.slug, None, [], kind=BUNDLE_RECORD_CANVAS
                )
    return bool(proven)
