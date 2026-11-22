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
A reinstall retires the extensions the bundle's new content no longer ships.

Records and lock rows are committed files, so retirement acts only on paths
inside a target's canvas root (see
:func:`~apm_cli.install.local_bundle_hook_trust.contained_bundle_path`), only
on rows of the bundle being retired, and never on a file another bundle's
record lists. A file whose bytes no longer match its recorded hash is kept;
a row with no recorded hash is kept with its file.
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

    @property
    def key(self) -> str | None:
        """The approval key the record carries, if exactly one."""
        keys = {key for key in self.keys if key}
        return next(iter(keys)) if len(keys) == 1 and None not in self.keys else None


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
    one-argument warning sink. Extensions this bundle recorded earlier that
    its new content no longer ships are retired first, silently.
    """
    from ..core.experimental import is_enabled
    from ..core.scope import InstallScope
    from ..integration.canvas_integrator import is_canvas_bundle_path
    from ..security.executables import EXEC_TYPE_CANVAS, package_type_approved

    user_scope = scope is InstallScope.USER
    shipped = sorted(rel for rel in pack_files if is_canvas_bundle_path(rel))
    if not dry_run:
        retire_recorded_bundle_canvas(
            project_root,
            targets,
            slug,
            user_scope=user_scope,
            keep=frozenset(_extension_suffix(rel) for rel in shipped),
        )
    enabled = is_enabled("canvas", create_config=create_config)
    keys = () if approval_key is None else (approval_key,)
    if enabled and package_type_approved(allow_executables, keys, EXEC_TYPE_CANVAS):
        return True, 0
    for rel in shipped:
        pack_files.pop(rel, None)
    if not (enabled and shipped):
        return False, 0
    retired = not dry_run and retire_recorded_bundle_canvas(
        project_root, targets, slug, user_scope=user_scope
    )
    lead = (
        f"Retired canvas extension(s) from local bundle '{slug}'"
        if retired
        else f"Blocked {len(shipped)} canvas extension file(s) from bundle '{slug}'"
    )
    report(f"{lead}: {_reason(allow_executables, approval_key, user_scope=user_scope)}")
    return False, len(shipped)


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


def _extension_suffix(path: str) -> str:
    """Return *path* below its ``extensions/`` segment (``board/extension.mjs``)."""
    parts = [seg for seg in path.replace("\\", "/").split("/") if seg]
    for index, seg in enumerate(parts[:-1]):
        if seg.lower() == "extensions":
            return "/".join(parts[index + 1 :])
    return "/".join(parts)


def canvas_names(paths: Collection[str]) -> frozenset[str]:
    """Return the extension directory names of bundle canvas *paths*."""
    return frozenset(_extension_suffix(rel).split("/", 1)[0] for rel in paths if "/" in rel)


def _canvas_targets(targets: Any) -> list[Any]:
    return [
        target
        for target in targets or ()
        if getattr(target, "primitives", {}).get("canvas") is not None
    ]


def _sidecar(project_root: Path, target: Any) -> Path:
    from ..integration.hook_integrator import _APM_HOOKS_SIDECAR

    return Path(project_root) / target.root_dir / _APM_HOOKS_SIDECAR


def _canvas_prefixes(targets: Any) -> tuple[str, ...]:
    from .audit_target_roots import canvas_deploy_prefixes

    return tuple(sorted(canvas_deploy_prefixes(_canvas_targets(targets))))


def _contained(project_root: Path, values: Collection[str], prefixes: tuple[str, ...]) -> set[str]:
    """Return the project-relative spelling of every *value* inside a canvas root."""
    from .local_bundle_hook_trust import _relative_value, contained_bundle_path

    root = Path(project_root)
    return {
        _relative_value(value, root)
        for value in values
        if contained_bundle_path(root, value, prefixes) is not None
    }


def record_bundle_canvas(
    project_root: Path,
    targets: Any,
    slug: str,
    approval_key: str | None,
    deployed: Collection[str],
) -> None:
    """Record the canvas files this install of bundle *slug* deployed, with its key."""
    from ..integration.hook_ownership import BUNDLE_RECORD_CANVAS, write_per_file_bundle_record

    root = Path(project_root)
    for target in _canvas_targets(targets):
        files = sorted(_contained(root, deployed, _canvas_prefixes([target])))
        sidecar = _sidecar(root, target)
        if files or sidecar.is_file():
            write_per_file_bundle_record(
                sidecar, slug, approval_key, files, kind=BUNDLE_RECORD_CANVAS
            )


def _canvas_records(project_root: Path, targets: Any) -> dict[str, tuple[set, set[str]]]:
    """Return ``slug -> (keys, contained files)`` from every canvas target's record."""
    from ..integration.hook_ownership import BUNDLE_RECORD_CANVAS, per_file_bundle_hook_keys

    root = Path(project_root)
    found: dict[str, tuple[set, set[str]]] = {}
    for target in _canvas_targets(targets):
        try:
            records = per_file_bundle_hook_keys(_sidecar(root, target), BUNDLE_RECORD_CANVAS)
        except (OSError, ValueError, TypeError):
            records = {}
        prefixes = _canvas_prefixes([target])
        for slug, (key, files) in records.items():
            keys, held = found.setdefault(slug, (set(), set()))
            keys.add(key)
            held.update(_contained(root, files, prefixes))
    return found


def deployed_bundle_canvas(
    project_root: Path,
    targets: Any,
    bundle_paths: Collection[str] | Callable[[], Collection[str]],
) -> list[BundleCanvas]:
    """Return the canvas files each local bundle deployed, as the lockfile still claims.

    A record names a bundle's files; local-bundle lock rows inside a canvas
    root that no record claims are grouped by extension directory, unrecorded.
    Values outside every canvas root are never returned.
    """
    prefixes = _canvas_prefixes(targets)
    if not prefixes:
        return []
    root = Path(project_root)
    records = _canvas_records(root, targets)
    # The lockfile is read only when a record or a canvas root exists.
    if not records and not any((root / prefix).is_dir() for prefix in prefixes):
        return []
    paths = bundle_paths() if callable(bundle_paths) else bundle_paths
    claimed = _contained(root, paths, prefixes)
    found: dict[tuple[str, bool], BundleCanvas] = {}
    recorded: set[str] = set()
    for slug, (keys, files) in records.items():
        recorded.update(files)
        held = claimed & files
        if held:
            holding = found.setdefault((slug, True), BundleCanvas(slug, True))
            holding.keys.update(keys)
            holding.files.update(held)
    for value in sorted(claimed - recorded):
        name = next(iter(canvas_names([value])), value)
        holding = found.setdefault((name, False), BundleCanvas(name, False))
        holding.keys.add(None)
        holding.files.add(value)
    return [found[key] for key in sorted(found)]


def _unadmitted(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    bundle_paths: Collection[str] | Callable[[], Collection[str]],
) -> list[tuple[BundleCanvas, str]]:
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
    result: list[tuple[BundleCanvas, str]] = []
    for holding in deployed:
        if None in holding.keys:
            result.append((holding, REASON_UNRECORDED))
        elif not all(
            package_type_approved(allow_map, (key,), EXEC_TYPE_CANVAS) for key in holding.keys
        ):
            result.append((holding, REASON_NOT_GRANTED))
    return result


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
    return [
        UnadmittedBundleCanvas(holding.slug, reason, tuple(sorted(holding.files)), holding.recorded)
        for holding, reason in _unadmitted(project_root, targets, allow_executables, bundle_paths)
    ]


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

    unadmitted = _unadmitted(
        project_root, targets, allow_executables, lambda: _lock_bundle_paths(lock_dir)
    )
    for holding, reason in unadmitted:
        if not dry_run:
            _retire(project_root, targets, holding, holding.files, lock_dir=lock_dir)
        if logger is not None:
            lead = "Would retire" if dry_run else "Retired"
            logger.warning(
                f"{lead} canvas extension(s) from local bundle '{holding.slug}' ({reason}). "
                f"To keep them, {BUNDLE_HOOKS_APPROVED.clause()}."
            )
    return [
        UnadmittedBundleCanvas(holding.slug, reason, tuple(sorted(holding.files)), holding.recorded)
        for holding, reason in unadmitted
    ]


def retire_recorded_bundle_canvas(
    project_root: Path,
    targets: Any,
    slug: str,
    *,
    user_scope: bool,
    keep: frozenset[str] | None = None,
) -> bool:
    """Retire the canvas files bundle *slug* recorded; another bundle's stay.

    *keep* names extension-relative paths (``board/extension.mjs``) the
    bundle still ships; those stay. Returns whether any file was removed or
    any lockfile row retired.
    """
    from ..core.scope import InstallScope, get_apm_dir
    from .local_bundle_hook_trust import _lock_bundle_paths

    lock_dir = get_apm_dir(InstallScope.USER) if user_scope else Path(project_root)
    holdings = deployed_bundle_canvas(project_root, targets, _lock_bundle_paths(lock_dir))
    holding = next((h for h in holdings if h.recorded and h.slug == slug), None)
    if holding is None:
        return False
    files = {value for value in holding.files if _extension_suffix(value) not in (keep or ())}
    return bool(files) and _retire(project_root, targets, holding, files, lock_dir=lock_dir)


def _retire(
    project_root: Path,
    targets: Any,
    holding: BundleCanvas,
    files: Collection[str],
    *,
    lock_dir: Path | None,
) -> bool:
    """Retire *files* of one holding; edited files and unhashed rows stay.

    A file another bundle's record lists is left with its row. Only files
    inside a canvas root are touched; the holding's record keeps the rest.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..integration.base_integrator import BaseIntegrator
    from ..integration.hook_ownership import BUNDLE_RECORD_CANVAS, write_per_file_bundle_record
    from ..utils.content_hash import compute_file_hash
    from .local_bundle_hook_trust import (
        _drop_lock_rows,
        _lock_bundle_paths,
        _relative_value,
        contained_bundle_path,
    )

    root = Path(project_root)
    prefixes = _canvas_prefixes(targets)
    others = {
        value
        for other, (_keys, held) in _canvas_records(root, targets).items()
        if other != holding.slug or not holding.recorded
        for value in held
    }
    wanted = set(files) - others
    lockfile = LockFile.read(get_lockfile_path(lock_dir)) if lock_dir is not None else None
    hashes = dict(lockfile.local_deployed_file_hashes) if lockfile is not None else {}
    dropped: set[str] = set()
    removed: list[Path] = []
    for value in _lock_bundle_paths(lock_dir):
        rel = _relative_value(value, root)
        path = contained_bundle_path(root, value, prefixes)
        if rel not in wanted or path is None or not hashes.get(value):
            continue
        if path.is_file() and compute_file_hash(path) == hashes[value]:
            path.unlink()
            removed.append(path)
        dropped.add(value)
    if dropped and lock_dir is not None:
        _drop_lock_rows(lock_dir, dropped)
    for path in removed:
        stop = next((root / p for p in prefixes if path.is_relative_to(root / p)), root)
        BaseIntegrator.cleanup_empty_parents([path], stop)
    if holding.recorded and dropped:
        remaining = sorted(holding.files - {_relative_value(v, root) for v in dropped})
        for target in _canvas_targets(targets):
            sidecar = _sidecar(root, target)
            if sidecar.is_file():
                write_per_file_bundle_record(
                    sidecar, holding.slug, holding.key, remaining, kind=BUNDLE_RECORD_CANVAS
                )
    return bool(dropped)
