"""Re-check local-bundle hooks against current executable trust (#4027).

A local bundle's hooks were admitted by one exact content-bound approval key,
recorded with their ownership marker (see
:func:`~apm_cli.integration.hook_ownership.local_bundle_hook_source_marker`):
in the merged-config sidecar for merged targets, and in the same sidecar
name at the target root for per-file targets such as Copilot. Every install,
update, prune and ``apm audit --ci`` asks this module whether that key is
still granted under the current project, user and org layers. Hooks whose
key is no longer granted, or that record no key, are not admitted: install
retires them (merged entries through the hook integrator's local-bundle
retirement owner; per-file hook files are removed with their lock rows), and
audit reports them first.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apm_cli.core.remediation import BUNDLE_HOOKS_RETIRED

REASON_UNRECORDED = "no approval record"
REASON_NOT_GRANTED = "approval no longer granted"


@dataclass(frozen=True)
class UnadmittedBundleHooks:
    """One deployed local bundle whose hooks the current trust does not admit."""

    slug: str
    reason: str
    hook_files: tuple[str, ...] = ()


@dataclass
class _BundleHookState:
    keys: set[str | None] = field(default_factory=set)
    hook_files: set[str] = field(default_factory=set)


def _merged_hook_documents(project_root: Path, targets: Any):
    """Yield ``(config, sidecar, container)`` for every merged hook target."""
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS
    from apm_cli.integration.hook_ownership import read_hook_object

    for target in targets or ():
        config = _MERGE_HOOK_TARGETS.get(getattr(target, "name", None))
        if config is None:
            continue
        json_path = Path(project_root) / target.root_dir / config.config_filename
        sidecar_path = json_path.parent / _APM_HOOKS_SIDECAR
        try:
            yield (
                read_hook_object(json_path),
                read_hook_object(sidecar_path),
                config.event_container_key,
            )
        except (OSError, ValueError, TypeError):
            # Unreadable ownership state cannot prove an approval.
            yield None, None, config.event_container_key


def _per_file_targets(targets: Any) -> list[Any]:
    from apm_cli.integration.hook_integrator import _MERGE_HOOK_TARGETS

    return [
        target
        for target in targets or ()
        if getattr(target, "name", None) not in _MERGE_HOOK_TARGETS
        and callable(getattr(target, "supports", None))
        and target.supports("hooks")
    ]


def _record_value(path: Path, project_root: Path) -> str:
    """Return the lockfile value for *path*: project-relative, else absolute."""
    if path.is_relative_to(project_root):
        return path.relative_to(project_root).as_posix()
    return path.as_posix()


def _collect_per_file(
    project_root: Path,
    targets: Any,
    bundle_paths: Collection[str] | Callable[[], Collection[str]],
    found: dict[str, _BundleHookState],
) -> None:
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR
    from apm_cli.integration.hook_ownership import per_file_bundle_hook_keys

    for target in _per_file_targets(targets):
        target_root = Path(project_root) / target.root_dir
        recorded: set[str] = set()
        try:
            records = per_file_bundle_hook_keys(target_root / _APM_HOOKS_SIDECAR)
        except (OSError, ValueError, TypeError):
            records = {}
        for slug, (key, files) in records.items():
            state = found.setdefault(slug, _BundleHookState())
            state.keys.add(key)
            state.hook_files.update(files)
            recorded.update(files)
        # A bundle hook file the lock records without an approval record is
        # unapproved (written before the record existed, or the record is gone).
        hooks_dir = target_root / "hooks"
        if not hooks_dir.is_dir() or not any(hooks_dir.glob("*.json")):
            continue
        if callable(bundle_paths):
            bundle_paths = bundle_paths()
        for value in bundle_paths:
            path = Path(value) if Path(value).is_absolute() else Path(project_root) / value
            if path.suffix != ".json" or path.parent != hooks_dir or value in recorded:
                continue
            if _record_value(path, Path(project_root)) in recorded:
                continue
            slug = path.stem.removesuffix("-hooks")
            state = found.setdefault(slug, _BundleHookState())
            state.keys.add(None)
            state.hook_files.add(value)


def deployed_bundle_hook_keys(
    project_root: Path,
    targets: Any,
    bundle_paths: Collection[str] | Callable[[], Collection[str]] = (),
) -> dict[str, _BundleHookState]:
    """Return each deployed local bundle slug with its recorded keys and hook files.

    *bundle_paths* are the lockfile's local-bundle deployed paths; they name
    per-file hook files whose approval record is missing.
    """
    from apm_cli.integration.hook_ownership import (
        parse_local_bundle_hook_source,
        reinject_apm_source_from_sidecar,
    )

    found: dict[str, _BundleHookState] = {}
    for data, sidecar, container in _merged_hook_documents(project_root, targets):
        if data is None:
            continue
        hooks = data.get(container)
        if not isinstance(hooks, dict):
            continue
        if sidecar:
            reinject_apm_source_from_sidecar(hooks, sidecar)
        for entries in hooks.values():
            for entry in entries if isinstance(entries, list) else ():
                parsed = (
                    parse_local_bundle_hook_source(entry.get("_apm_source"))
                    if isinstance(entry, dict)
                    else None
                )
                if parsed is not None:
                    found.setdefault(parsed[0], _BundleHookState()).keys.add(parsed[1])
    _collect_per_file(project_root, targets, bundle_paths, found)
    return found


def unadmitted_bundle_hooks(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    bundle_paths: Collection[str] | Callable[[], Collection[str]] = (),
) -> list[UnadmittedBundleHooks]:
    """Return deployed local-bundle hooks the current trust does not admit.

    *allow_executables* returns the effective deny-wins map, and is asked
    only when a local bundle has deployed hooks. A gate-off map from the trust
    owner admits every hook, as installs do; a missing map admits none.
    """
    deployed = deployed_bundle_hook_keys(project_root, targets, bundle_paths)
    if not deployed:
        return []
    allow_map = allow_executables()
    from apm_cli.security.executables import (
        EXEC_TYPE_HOOKS,
        exec_map_authorizes_all,
        package_type_approved,
    )

    if exec_map_authorizes_all(allow_map):
        return []

    result: list[UnadmittedBundleHooks] = []
    for slug, state in sorted(deployed.items()):
        keys = state.keys
        files = tuple(sorted(state.hook_files))
        if None in keys:
            result.append(UnadmittedBundleHooks(slug, REASON_UNRECORDED, files))
        elif not all(package_type_approved(allow_map, (key,), EXEC_TYPE_HOOKS) for key in keys):
            result.append(UnadmittedBundleHooks(slug, REASON_NOT_GRANTED, files))
    return result


def _retire_per_file(project_root: Path, targets: Any, item: UnadmittedBundleHooks) -> None:
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR
    from apm_cli.integration.hook_ownership import write_per_file_bundle_record

    for value in item.hook_files:
        path = Path(value) if Path(value).is_absolute() else Path(project_root) / value
        if path.is_file() and not path.is_symlink():
            path.unlink()
    for target in _per_file_targets(targets):
        sidecar = Path(project_root) / target.root_dir / _APM_HOOKS_SIDECAR
        if sidecar.is_file():
            write_per_file_bundle_record(sidecar, item.slug, None, [])


def _drop_lock_rows(lock_dir: Path, paths: Collection[str]) -> None:
    """Drop retired local-bundle hook files from the scope's lockfile."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lock_path = get_lockfile_path(lock_dir)
    lockfile = LockFile.read(lock_path)
    if lockfile is None or not set(paths) & set(lockfile.local_deployed_files):
        return
    remaining = [path for path in lockfile.local_deployed_files if path not in paths]
    hashes = {
        path: digest
        for path, digest in lockfile.local_deployed_file_hashes.items()
        if path not in paths
    }
    DeploymentLedgerCodec.replace_legacy_owner(lockfile, ".", remaining, hashes)
    lockfile.write(lock_path)


def retire_unadmitted_bundle_hooks(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    *,
    lock_dir: Path | None = None,
    logger: Any = None,
    dry_run: bool = False,
) -> list[UnadmittedBundleHooks]:
    """Retire every local-bundle hook trust no longer admits."""
    # The lockfile is read only when a per-file hooks directory holds files.
    unadmitted = unadmitted_bundle_hooks(
        project_root, targets, allow_executables, lambda: _lock_bundle_paths(lock_dir)
    )
    if not unadmitted:
        return []
    for item in unadmitted:
        if not dry_run:
            retire_bundle_hooks(
                project_root,
                targets,
                item.slug,
                lock_dir=lock_dir,
                reason=item.reason,
                logger=logger,
            )
        elif logger is not None:
            logger.warning(
                f"Would retire hooks from local bundle '{item.slug}' ({item.reason}). "
                f"To keep them, {BUNDLE_HOOKS_RETIRED.clause()}."
            )
    return unadmitted


def retire_merged_bundle_entries(project_root: Path, targets: Any, slug: str) -> None:
    """Drop one bundle's merged hook entries before an approved reinstall rewrites them."""
    from apm_cli.integration.hook_integrator import HookIntegrator

    hook_targets = [target for target in targets if target.supports("hooks")]
    HookIntegrator().retire_local_bundle_hooks(slug, project_root, hook_targets)


def _bundle_script_rows(
    lock_paths: Collection[str], targets: Any, slug: str, project_root: Path
) -> set[str]:
    """Return the lock rows of hook scripts the hook integrator copied for *slug*."""
    prefixes = tuple(
        f"{target.root_dir}/hooks/{middle}{slug}/"
        for target in targets or ()
        if callable(getattr(target, "supports", None)) and target.supports("hooks")
        for middle in ("", "scripts/")
    )
    return {
        value
        for value in lock_paths
        if _relative_value(value, project_root).startswith(prefixes) and prefixes
    }


def _relative_value(value: str, project_root: Path) -> str:
    path = Path(value)
    return _record_value(path, project_root) if path.is_absolute() else value


def _remove_unedited(project_root: Path, rows: Collection[str], recorded: dict[str, str]) -> None:
    """Remove deployed files whose bytes still match the lock; edits are kept."""
    from apm_cli.utils.content_hash import compute_file_hash

    for value in rows:
        path = Path(value) if Path(value).is_absolute() else project_root / value
        if not path.is_file() or path.is_symlink():
            continue
        digest = recorded.get(value)
        if digest is None or digest == compute_file_hash(path):
            path.unlink()


def retire_bundle_hooks(
    project_root: Path,
    targets: Any,
    slug: str,
    *,
    lock_dir: Path | None,
    reason: str | None,
    logger: Any = None,
    diagnostics: Any = None,
) -> bool:
    """Retire every hook one local bundle deployed earlier; its reinstall registers none.

    Removes merged entries, per-file hook files with their approval record,
    the hook scripts copied for *slug*, and their lockfile rows. A *reason*
    announces the retirement; ``None`` retires silently (the new bundle ships
    no hooks). Returns whether anything was deployed.
    """
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    project_root = Path(project_root)
    lockfile = LockFile.read(get_lockfile_path(lock_dir)) if lock_dir is not None else None
    lock_paths = _lock_bundle_paths(lock_dir)
    state = deployed_bundle_hook_keys(project_root, targets, lock_paths).get(slug)
    hook_files = {
        _relative_value(value, project_root) for value in (state.hook_files if state else ())
    }
    scripts = _bundle_script_rows(lock_paths, targets, slug, project_root)
    rows = scripts | {
        value for value in lock_paths if _relative_value(value, project_root) in hook_files
    }
    retire_merged_bundle_entries(project_root, targets, slug)
    if state is None and not rows:
        return False
    # Hook configs register executables and always go; an edited script is kept.
    configs = tuple(sorted(state.hook_files)) if state else ()
    _retire_per_file(project_root, targets, UnadmittedBundleHooks(slug, reason or "", configs))
    recorded = dict(lockfile.local_deployed_file_hashes) if lockfile is not None else {}
    _remove_unedited(project_root, scripts, recorded)
    if rows and lock_dir is not None:
        _drop_lock_rows(lock_dir, rows)
    if reason is not None:
        message = (
            f"Retired hooks from local bundle '{slug}' ({reason}). "
            f"To keep them, {BUNDLE_HOOKS_RETIRED.clause()}."
        )
        if diagnostics is not None:
            diagnostics.warn(message=message, package=slug)
        elif logger is not None:
            logger.warning(message)
    return True


def _lock_bundle_paths(lock_dir: Path | None) -> frozenset[str]:
    if lock_dir is None:
        return frozenset()
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lockfile = LockFile.read(get_lockfile_path(lock_dir))
    if lockfile is None:
        return frozenset()
    return DeploymentLedgerCodec.local_bundle_values(DeploymentLedgerCodec.from_lockfile(lockfile))


class BundleHookTargetsUnresolved(RuntimeError):
    """The scope's targets could not be resolved, so no hook was re-checked."""


def scope_bundle_hook_inputs(scope: Any, apm_package: Any, *, no_policy: bool, logger: Any):
    """Return ``(deploy_root, targets, allow_provider)`` for one command scope."""
    from apm_cli.core.scope import get_deploy_root, is_user_scope
    from apm_cli.integration.targets import resolve_targets
    from apm_cli.security.executables import resolve_project_exec_allow_map

    root = get_deploy_root(scope)
    user_scope = is_user_scope(scope)
    explicit = list(getattr(apm_package, "canonical_targets", None) or ()) or None
    try:
        targets = resolve_targets(root, user_scope=user_scope, explicit_target=explicit)
    except Exception as exc:
        raise BundleHookTargetsUnresolved(str(exc)) from exc

    def allow_provider() -> dict[str, dict[str, bool]]:
        return resolve_project_exec_allow_map(
            root, apm_package, user_scope=user_scope, no_policy=no_policy, logger=logger
        )

    return root, targets, allow_provider


def reconcile_scope_bundle_hooks(
    scope: Any,
    apm_package: Any,
    *,
    no_policy: bool = False,
    logger: Any = None,
    dry_run: bool = False,
) -> list[UnadmittedBundleHooks]:
    """Re-check one scope's local-bundle hooks; install, update and prune call this."""
    from apm_cli.core.scope import get_apm_dir

    root, targets, allow_provider = scope_bundle_hook_inputs(
        scope, apm_package, no_policy=no_policy, logger=logger
    )
    return retire_unadmitted_bundle_hooks(
        root, targets, allow_provider, lock_dir=get_apm_dir(scope), logger=logger, dry_run=dry_run
    )


def installed_local_bundles(scope: Any, apm_package: Any) -> tuple[list[str], int]:
    """Return ``(names, file_count)`` of the local bundles a scope's lockfile records.

    Names come from the hook ownership markers and compile staging slugs a
    bundle leaves; *file_count* counts every bundle-owned path, so a bundle
    without either is still reported (#4318).
    """
    from apm_cli.core.scope import get_apm_dir
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lock_dir = get_apm_dir(scope)
    paths = _lock_bundle_paths(lock_dir)
    lockfile = LockFile.read(get_lockfile_path(lock_dir))
    staged = set(lockfile.local_staging_slugs()) if lockfile is not None else set()
    if not paths and not staged:
        return [], 0
    try:
        root, targets, _allow = scope_bundle_hook_inputs(
            scope, apm_package, no_policy=True, logger=None
        )
        hooked = set(deployed_bundle_hook_keys(root, targets, paths))
    except BundleHookTargetsUnresolved:
        hooked = set()
    return sorted(hooked | staged), len(paths)
