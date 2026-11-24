"""Re-check local-bundle hooks (and canvas) against current executable trust (#4027).

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

import contextlib
import os
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.core.remediation import BUNDLE_HOOKS_RETIRED
from apm_cli.install.hook_script_refs import (
    hook_commands,
    runs_script,
    script_refs,
)
from apm_cli.install.local_bundle_hook_cwd import commands_without_cwd, runs_through_cwd
from apm_cli.install.local_bundle_hook_states import (
    BundleHookStates,
    _BundleHookState,
    _merged_hook_documents,
    _MergedHookState,
    _modified_slugs,
    _per_file_targets,
    _recorded_slugs,
    _sidecar_identities,
    read_bundle_hook_states,
)

REASON_UNRECORDED = "no approval record"
REASON_NOT_GRANTED = "approval no longer granted"


@dataclass(frozen=True)
class UnadmittedBundleHooks:
    """One deployed local bundle whose hooks the current trust does not admit."""

    slug: str
    reason: str
    hook_files: tuple[str, ...] = ()


def _record_value(path: Path, project_root: Path) -> str:
    """Return the lockfile value for *path*: project-relative, else absolute."""
    if path.is_relative_to(project_root):
        return path.relative_to(project_root).as_posix()
    return path.as_posix()


def _collect_per_file(
    project_root: Path,
    states: BundleHookStates,
    bundle_paths: Collection[str] | Callable[[], Collection[str]],
    found: dict[str, _BundleHookState],
) -> None:
    for per_file in states.per_file:
        target_root = Path(project_root) / per_file.target.root_dir
        recorded: set[str] = set()
        # Read once by read_bundle_hook_states (#5211); None names the sidecar (#5144).
        unreadable = per_file.label if per_file.records is None else None
        for slug, (key, files) in (per_file.records or {}).items():
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
            if unreadable is not None:
                state.unreadable.add(unreadable)


def deployed_bundle_hook_keys(
    project_root: Path,
    targets: Any,
    bundle_paths: Collection[str] | Callable[[], Collection[str]] = (),
    states: BundleHookStates | None = None,
) -> dict[str, _BundleHookState]:
    """Return each deployed local bundle slug with its recorded keys and hook files.

    *bundle_paths* are the lockfile's local-bundle deployed paths; they name
    per-file hook files whose approval record is missing. *states*, when the
    caller already read them, are used instead of reading every config again.
    """
    from apm_cli.integration.hook_ownership import (
        parse_local_bundle_hook_source,
        reinject_apm_source_from_sidecar,
    )

    states = states or read_bundle_hook_states(project_root, targets)
    found: dict[str, _BundleHookState] = {}
    for data, sidecar, container in _merged_hook_documents(project_root, targets, states.merged):
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
    _collect_per_file(project_root, states, bundle_paths, found)
    return found


def unadmitted_bundle_hooks(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    bundle_paths: Collection[str] | Callable[[], Collection[str]] = (),
    states: BundleHookStates | None = None,
) -> list[UnadmittedBundleHooks]:
    """Return deployed local-bundle hooks the current trust does not admit.

    *allow_executables* returns the effective deny-wins map, and is asked
    only when a local bundle has deployed hooks. A gate-off map from the trust
    owner admits every hook, as installs do; a missing map admits none.
    """
    deployed = deployed_bundle_hook_keys(project_root, targets, bundle_paths, states)
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
        if state.unreadable:
            result.append(UnadmittedBundleHooks(slug, state.unreadable_reason(), files))
        elif None in keys:
            result.append(UnadmittedBundleHooks(slug, REASON_UNRECORDED, files))
        elif not all(package_type_approved(allow_map, (key,), EXEC_TYPE_HOOKS) for key in keys):
            result.append(UnadmittedBundleHooks(slug, REASON_NOT_GRANTED, files))
    return result


def _scripted_slugs(state: _MergedHookState, lock_paths: Collection[str], project_root: Path):
    """Bundles the lock records hook scripts for under *state*'s target."""
    from apm_cli.integration.hook_command_paths import hook_scripts_parent

    base = Path(hook_scripts_parent(state.target.name, state.target.root_dir)).as_posix() + "/"
    slugs: set[str] = set()
    for value in lock_paths:
        rest = _relative_value(value, project_root)
        if rest.startswith(base) and "/" in rest[len(base) :]:
            slugs.add(rest[len(base) :].split("/", 1)[0])
    return slugs


def bundle_hook_ownership(
    project_root: Path,
    targets: Any,
    bundle_paths: Collection[str] = (),
    states: BundleHookStates | None = None,
) -> list[str]:
    """Every bundle hook ownership problem, reading each config and sidecar once (#5078).

    A problem is a bundle entry edited or removed outside APM, an unreadable
    ownership record (or config, while the lock records bundle hooks there),
    or bundle hook scripts the lock records for a target whose ownership
    record names no entry for that bundle (a missing or stripped sidecar).
    Each would otherwise leave the bundle's entries silently unattributed.
    """
    states = states or read_bundle_hook_states(project_root, targets)
    # A per-file record that cannot be read (#5144) is named, never read as "no record".
    problems = [
        f"{state.label}: the bundle hook ownership record cannot be read, "
        "so bundle hooks there cannot be attributed"
        for state in states.per_file
        if state.records is None
    ]
    for state in states.merged:
        scripted = _scripted_slugs(state, bundle_paths, project_root)
        if state.sidecar_unreadable or (
            state.config_unreadable and (scripted or _recorded_slugs(state))
        ):
            named = f" (unreadable: {state.sidecar_label})" if state.sidecar_unreadable else ""
            problems.append(
                f"{state.label}: the hook config or its ownership record cannot be read"
                f"{named}, so bundle hooks there cannot be attributed"
            )
            continue
        problems += [
            f"{slug}: a deployed hook entry was changed outside APM"
            for slug in sorted(_modified_slugs(state))
        ]
        problems += [
            f"{slug}: {state.label} has no ownership record for the bundle's deployed hooks"
            for slug in sorted(scripted - _recorded_slugs(state))
        ]
    return problems


def contained_bundle_path(project_root: Path, value: str, prefixes: Collection[str]) -> Path | None:
    """Return the in-project path a bundle record or lock row names, or ``None``.

    Records and lock rows are committed files, so a value is acted on only
    when it names a path under one of *prefixes* (project-relative roots such
    as ``.github/hooks/``) with no ``.``/``..`` segment and no linked
    component, and resolves inside *project_root*.
    """
    from pathlib import PureWindowsPath

    from apm_cli.utils.path_security import (
        PathTraversalError,
        ensure_path_within,
        has_symlink_component,
    )

    root = Path(project_root)
    rel = _relative_value(value, root).replace("\\", "/")
    parts = rel.split("/")
    if (
        not prefixes
        or Path(rel).is_absolute()
        or PureWindowsPath(rel).is_absolute()
        or any(part in ("", ".", "..") for part in parts)
        or not rel.startswith(tuple(prefixes))
    ):
        return None
    path = root / rel
    if has_symlink_component(root, path):
        return None
    try:
        ensure_path_within(path, root)
    except PathTraversalError:
        return None
    return path


def _target_base(project_root: Path, target: Any) -> Path:
    root_dir = Path(target.root_dir)
    return root_dir.parent if root_dir.is_absolute() else Path(project_root)


def bundle_hook_cleanup_blockers(project_root: Path, targets: Any) -> list[str]:
    """Return the hook configs a bundle hook removal could not safely read or rewrite.

    A linked path (a symlinked target root or config) or an unreadable config
    cannot prove what a bundle registered, so its removal is refused before
    any change instead of reported as done (#3700).
    """
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS
    from apm_cli.integration.hook_ownership import per_file_bundle_hook_keys, read_hook_object
    from apm_cli.utils.path_security import has_symlink_component

    blockers: list[str] = []
    for target in targets or ():
        if not (callable(getattr(target, "supports", None)) and target.supports("hooks")):
            continue
        root = Path(project_root) / target.root_dir
        config = _MERGE_HOOK_TARGETS.get(target.name)
        paths = [root / _APM_HOOKS_SIDECAR]
        if config is not None:
            paths.insert(0, root / config.config_filename)
        base = _target_base(Path(project_root), target)
        for path in paths:
            if not (path.is_symlink() or path.exists()):
                continue
            label = _record_value(path, Path(project_root))
            if has_symlink_component(base, path):
                blockers.append(f"{label} (linked path)")
                continue
            try:
                read_hook_object(path, regular_only=path.name == _APM_HOOKS_SIDECAR)
                if config is None:
                    # A per-file sidecar's bundle records are read again on removal.
                    per_file_bundle_hook_keys(path)
            except (OSError, ValueError, TypeError):
                blockers.append(f"{label} (unreadable)")
    return blockers


def _hooks_dir_prefix(root_dir: str, *parts: str) -> str:
    """Return ``<root_dir>/hooks/<parts>/`` normalized as lock rows are (a ``.`` root drops out)."""
    return Path(root_dir, "hooks", *parts).as_posix() + "/"


def _hooks_prefixes(targets: Any) -> tuple[str, ...]:
    return tuple(
        _hooks_dir_prefix(target.root_dir)
        for target in targets or ()
        if callable(getattr(target, "supports", None)) and target.supports("hooks")
    )


def _retire_per_file(
    project_root: Path, targets: Any, item: UnadmittedBundleHooks
) -> tuple[set[str], set[str]]:
    """Remove one bundle's per-file hook files; return ``(kept, unwritten_sidecars)``.

    A file that cannot be removed (a symlink, an unsafe path) keeps its
    approval record, so it stays owned and visible instead of orphaned. A
    sidecar that cannot be rewritten keeps its record and is reported, so the
    deleted files' lock rows are still reconciled.
    """
    from apm_cli.integration import write_seam
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR
    from apm_cli.integration.hook_ownership import (
        per_file_bundle_hook_keys,
        write_per_file_bundle_record,
    )

    prefixes = tuple(_hooks_dir_prefix(target.root_dir) for target in _per_file_targets(targets))
    kept: set[str] = set()
    for value in item.hook_files:
        hook_file = contained_bundle_path(project_root, value, prefixes)
        if hook_file is not None and hook_file.is_file():
            # A failed removal leaves the file on disk; it is retained below.
            with contextlib.suppress(OSError):
                write_seam.unlink(hook_file)  # journaled like the deploy (#5130)
        literal = Path(value) if Path(value).is_absolute() else Path(project_root) / value
        if literal.is_symlink() or literal.exists():
            kept.add(value)
    unwritten: set[str] = set()
    for target in _per_file_targets(targets):
        sidecar = Path(project_root) / target.root_dir / _APM_HOOKS_SIDECAR
        if not sidecar.is_file():
            continue
        try:
            records = per_file_bundle_hook_keys(sidecar)
            if item.slug not in records:
                continue
            key, files = records[item.slug]
            write_per_file_bundle_record(
                sidecar, item.slug, key, [value for value in files if value in kept]
            )
        except (OSError, ValueError, TypeError):
            unwritten.add(_record_value(sidecar, Path(project_root)))
    return kept, unwritten


def _unreadable_per_file_sidecars(project_root: Path, targets: Any) -> list[str]:
    """Per-file sidecars whose bundle records cannot be read, named for the user."""
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR
    from apm_cli.integration.hook_ownership import per_file_bundle_hook_keys
    from apm_cli.utils.diagnostics import printable_ascii_text

    unreadable = []
    for target in _per_file_targets(targets):
        sidecar = Path(project_root) / target.root_dir / _APM_HOOKS_SIDECAR
        if not sidecar.is_file():
            continue
        try:
            per_file_bundle_hook_keys(sidecar)
        except (OSError, ValueError, TypeError):
            label = printable_ascii_text(_record_value(sidecar, Path(project_root)))
            unreadable.append(f"{label} (unreadable)")
    return unreadable


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
        project_root, targets, allow_executables, lambda: local_bundle_paths(lock_dir)
    )
    if not unadmitted:
        return []
    failed: dict[str, tuple[str, ...]] = {}
    for item in unadmitted:
        if not dry_run:
            retired = retire_bundle_hooks(
                project_root,
                targets,
                item.slug,
                lock_dir=lock_dir,
                reason=item.reason,
                logger=logger,
            )
            if retired.failed_paths:
                failed[item.slug] = retired.failed_paths
        elif logger is not None:
            logger.warning(
                f"Would retire hooks from local bundle '{item.slug}' ({item.reason}). "
                f"To keep them, {BUNDLE_HOOKS_RETIRED.clause()}."
            )
    if failed:
        raise BundleHookRetirementIncomplete(failed)
    return unadmitted


def retire_merged_bundle_entries(project_root: Path, targets: Any, slug: str) -> list[str]:
    """Drop one bundle's merged hook entries; return the configs that could not be rewritten."""
    from apm_cli.integration.hook_integrator import HookIntegrator

    hook_targets = [target for target in targets if target.supports("hooks")]
    stats = HookIntegrator().retire_local_bundle_hooks(slug, project_root, hook_targets)
    return list(stats["failed_paths"])


class BundleHookRetirementIncomplete(RuntimeError):
    """Hooks trust no longer admits could not all be retired; the rest stays for a retry."""

    def __init__(self, failed: dict[str, Collection[str]]):
        self.failed = {slug: tuple(paths) for slug, paths in failed.items()}
        detail = "; ".join(f"{slug}: {', '.join(paths)}" for slug, paths in self.failed.items())
        super().__init__(
            f"Could not retire local bundle hooks ({detail}). They were kept; "
            "fix the file permissions, then re-run the command."
        )


@dataclass(frozen=True)
class BundleHookRetirement:
    """What retiring one bundle's hooks did; truthy when the bundle had deployed hooks."""

    deployed: bool
    failed_paths: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.deployed


def _bundle_script_rows(
    lock_paths: Collection[str], targets: Any, slug: str, project_root: Path
) -> set[str]:
    """Return the lock rows of hook scripts the hook integrator copied for *slug*."""
    prefixes = tuple(f"{base}{slug}/" for base in _script_bases(targets))
    return {
        value
        for value in lock_paths
        if _relative_value(value, project_root).startswith(prefixes) and prefixes
    }


def _script_bases(targets: Any) -> tuple[str, ...]:
    """Each hook target's script directory parent, as the hook integrator lays it out.

    Only the layout a target actually uses is matched, so a slug named
    ``scripts`` never covers another bundle's Copilot scripts.
    """
    from apm_cli.integration.hook_command_paths import hook_scripts_parent

    return tuple(
        Path(hook_scripts_parent(target.name, target.root_dir)).as_posix() + "/"
        for target in targets or ()
        if callable(getattr(target, "supports", None)) and target.supports("hooks")
    )


def bundle_hook_leftovers(
    project_root: Path, targets: Any, lock_paths: Collection[str]
) -> dict[str, set[str]]:
    """Return slugs whose hook ownership outlived their registration, with the evidence.

    A partially retired bundle can keep sidecar markers or script claims after
    its native entries are gone; uninstall must still reach them. A bundle whose
    hooks were only parked has neither, so it is not reported.
    """
    found: dict[str, set[str]] = {}
    for slug, _key in _sidecar_identities(project_root, targets):
        found.setdefault(slug, set())
    bases = _script_bases(targets)
    for value in lock_paths:
        rel = _relative_value(value, Path(project_root))
        for base in bases:
            slug, sep, _rest = (
                rel[len(base) :].partition("/") if rel.startswith(base) else ("", "", "")
            )
            if slug and sep:
                found.setdefault(slug, set()).add(value)
    return found


def bundle_hook_identities(
    project_root: Path, targets: Any, lock_paths: Collection[str]
) -> dict[str, set[str | None]]:
    """Return each slug with hook ownership left and the approval keys it records.

    Covers deployed hooks and partial-retirement leftovers: a sidecar marker
    keeps its key after the native entry is gone. ``None`` (or no key at all)
    means no content identity was recorded.
    """
    found = {
        slug: set(state.keys)
        for slug, state in deployed_bundle_hook_keys(project_root, targets, lock_paths).items()
    }
    for slug, key in _sidecar_identities(project_root, targets):
        found.setdefault(slug, set()).add(key)
    for slug in bundle_hook_leftovers(project_root, targets, lock_paths):
        found.setdefault(slug, set())
    return found


def _relative_value(value: str, project_root: Path) -> str:
    path = Path(value)
    return _record_value(path, project_root) if path.is_absolute() else value


def _per_file_configs_running(
    project_root: Path, targets: Any, owned: Collection[str], scripts: Collection[str]
) -> set[str]:
    """Return per-file hook files outside *owned* whose commands run *scripts*.

    Without its approval record a per-file hook file cannot be attributed to
    a bundle (Kiro folds the package slug into a longer file stem), so a hook
    file that still runs the bundle's scripts, or cannot be read, keeps them
    and refuses retirement.
    """
    from apm_cli.integration.hook_ownership import read_hook_object

    found: set[str] = set()
    refs = script_refs(project_root, scripts)
    for target in _per_file_targets(targets):
        target_root = project_root / target.root_dir
        hooks_dir = target_root / "hooks"
        # A hook in any target may run a script by any spelling (#5106).
        if not refs[0] or not hooks_dir.is_dir():
            continue
        for path in sorted(hooks_dir.glob("*.json")):
            value = _record_value(path, project_root)
            if value in owned:
                continue
            try:
                document = read_hook_object(path)
            except (OSError, ValueError, TypeError):
                found.add(value)  # an unreadable hook file may still run the scripts
                continue
            texts = list(hook_commands(document))
            if any(
                runs_script(text, refs, (project_root, target_root), project_root)
                for text in commands_without_cwd(document, texts)
            ) or runs_through_cwd(document, project_root, scripts):
                found.add(value)
    return found


def _configs_still_running(
    project_root: Path, targets: Any, slug: str, scripts: Collection[str]
) -> set[str]:
    """Return merged hook configs with an entry *slug* no longer owns that runs its scripts.

    Read before retirement removes anything: an entry the user edited (another
    matcher) lost the bundle's ownership marker, so retirement would leave it
    registered while discarding the ownership that keeps the bundle visible to
    trust checks. Only the target's hook event container counts; permissions
    or environment values naming a script do not run it.
    """
    from apm_cli.hook_contract import HOOK_COMMAND_KEYS
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS
    from apm_cli.integration.hook_ownership import (
        LocalBundleMarkers,
        read_hook_object,
        reinject_apm_source_from_sidecar,
    )

    def commands(entry: Any) -> list[str]:
        # Only a hook's command fields run anything, at the entry level
        # (flat format) or in its nested handlers (matcher groups).
        if not isinstance(entry, dict):
            return []
        handlers = [entry, *(h for h in entry.get("hooks") or () if isinstance(h, dict))]
        return [
            value
            for handler in handlers
            for key in HOOK_COMMAND_KEYS
            if isinstance(value := handler.get(key), str)
        ]

    markers = LocalBundleMarkers(slug)
    found: set[str] = set()
    refs = script_refs(project_root, scripts)
    for target in targets or ():
        config = _MERGE_HOOK_TARGETS.get(getattr(target, "name", None))
        if config is None:
            continue
        target_root = project_root / target.root_dir
        json_path = target_root / config.config_filename
        # A hook in any target may run a script by any spelling (#5106).
        if not refs[0] or not json_path.is_file():
            continue
        try:
            hooks = read_hook_object(json_path).get(config.event_container_key)
            sidecar = read_hook_object(json_path.parent / _APM_HOOKS_SIDECAR, regular_only=True)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(hooks, dict):
            continue
        if sidecar:
            reinject_apm_source_from_sidecar(hooks, sidecar)
        foreign = [
            entry
            for entries in hooks.values()
            if isinstance(entries, list)
            for entry in entries
            if not (isinstance(entry, dict) and entry.get("_apm_source") in markers)
        ]
        texts = [text for entry in foreign for text in commands(entry)]
        if any(
            runs_script(text, refs, (project_root, target_root), project_root) for text in texts
        ):
            found.add(_record_value(json_path, project_root))
    return found


def _script_path(project_root: Path, value: str, targets: Any) -> tuple[Path, Path] | None:
    """Return ``(path, root)`` for a bundle script row, or ``None`` when it is refused.

    A Claude root relocated outside the deploy root (``CLAUDE_CONFIG_DIR``)
    records absolute rows; they resolve under that target's hook cleanup root,
    as install contained them, and nowhere else.
    """
    from apm_cli.integration.hook_bundle import hook_cleanup_root

    for target in targets or ():
        if not (callable(getattr(target, "supports", None)) and target.supports("hooks")):
            continue
        prefix = _hooks_dir_prefix(target.root_dir)
        if not Path(prefix).is_absolute() or not value.startswith(prefix):
            continue
        root = hook_cleanup_root(target, project_root)
        if root is None or root == project_root:
            return None
        path = contained_bundle_path(root, value, (_hooks_dir_prefix("."),))
        return (path, root) if path is not None else None
    prefixes = _hooks_prefixes(targets)
    path = contained_bundle_path(project_root, value, prefixes)
    return (path, project_root) if path is not None else None


def _remove_unedited(
    project_root: Path, rows: Collection[str], recorded: dict[str, str], targets: Any
) -> set[str]:
    """Remove deployed hook scripts whose bytes still match the lock; edits are kept.

    Returns the rows whose file could not be removed, including a present file
    whose path is refused; their claims must stay.
    """
    from apm_cli.integration import write_seam
    from apm_cli.integration.base_integrator import BaseIntegrator
    from apm_cli.utils.content_hash import compute_file_hash

    removed: dict[Path, list[Path]] = {}
    failed: set[str] = set()
    for value in rows:
        found = _script_path(project_root, value, targets)
        if found is None:
            spelled = Path(value) if Path(value).is_absolute() else project_root / value
            if os.path.lexists(spelled):
                failed.add(value)
            continue
        path, root = found
        if not path.is_file():
            continue
        try:
            digest = recorded.get(value)
            if digest is None or digest == compute_file_hash(path):
                write_seam.unlink(path)
                removed.setdefault(root, []).append(path)
        except OSError:
            failed.add(value)
    for root, paths in removed.items():
        BaseIntegrator.cleanup_empty_parents(paths, root)
    return failed


def _bundle_hook_inputs(project_root: Path, targets: Any, slug: str, lock_paths):
    """``(state, hook files, script rows)`` one bundle's hook retirement acts on."""
    state = deployed_bundle_hook_keys(project_root, targets, lock_paths).get(slug)
    hook_files = {
        _relative_value(value, project_root) for value in (state.hook_files if state else ())
    }
    return state, hook_files, _bundle_script_rows(lock_paths, targets, slug, project_root)


def _still_running(project_root: Path, targets: Any, slug: str, hook_files, scripts) -> set[str]:
    """Hook configs the bundle does not own that still run its scripts."""
    return _configs_still_running(project_root, targets, slug, scripts) | (
        _per_file_configs_running(project_root, targets, hook_files, scripts)
    )


def bundle_hooks_in_use(
    project_root: Path, targets: Any, slug: str, *, lock_dir: Path | None
) -> tuple[str, ...]:
    """Hook configs that still run *slug*'s scripts, read before anything is written (#5291).

    The same check :func:`retire_bundle_hooks` refuses on; a caller runs it
    first so a refused uninstall writes nothing, not even its resume note.
    """
    project_root = Path(project_root)
    _state, hook_files, scripts = _bundle_hook_inputs(
        project_root, targets, slug, local_bundle_paths(lock_dir)
    )
    return tuple(sorted(_still_running(project_root, targets, slug, hook_files, scripts)))


def retire_bundle_hooks(
    project_root: Path,
    targets: Any,
    slug: str,
    *,
    lock_dir: Path | None,
    reason: str | None,
    logger: Any = None,
    diagnostics: Any = None,
) -> BundleHookRetirement:
    """Retire every hook one local bundle deployed earlier; its reinstall registers none.

    Removes merged entries, per-file hook files with their approval record,
    the hook scripts copied for *slug*, and their lockfile rows. A *reason*
    announces the retirement; ``None`` retires silently (the new bundle ships
    no hooks). The result is truthy when anything was deployed and names the
    merged configs that could not be rewritten; then nothing else is removed,
    so the bundle's scripts, records and lock rows stay for a retry. A hook
    config or script that cannot be deleted, or a sidecar that cannot be
    rewritten, is named too; then the scripts and retained claims stay and no
    retirement is announced.
    """
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    project_root = Path(project_root)
    lockfile = LockFile.read(get_lockfile_path(lock_dir)) if lock_dir is not None else None
    lock_paths = local_bundle_paths(lock_dir)
    state, hook_files, scripts = _bundle_hook_inputs(project_root, targets, slug, lock_paths)
    rows = scripts | {
        value for value in lock_paths if _relative_value(value, project_root) in hook_files
    }
    still_running = _still_running(project_root, targets, slug, hook_files, scripts)
    if still_running:
        # Nothing is retired: the bundle keeps the ownership trust checks read.
        return BundleHookRetirement(True, tuple(sorted(still_running)))
    if unreadable := _unreadable_per_file_sidecars(project_root, targets):
        # Read before any retirement write: a corrupt record retires nothing (#5144).
        return BundleHookRetirement(True, tuple(unreadable))
    failed = retire_merged_bundle_entries(project_root, targets, slug)
    if failed:
        return BundleHookRetirement(True, tuple(failed))
    if state is None and not rows:
        return BundleHookRetirement(False)
    # Hook configs register executables and always go; an edited script is kept.
    configs = tuple(sorted(state.hook_files)) if state else ()
    item = UnadmittedBundleHooks(slug, reason or "", configs)
    kept, unwritten = _retire_per_file(project_root, targets, item)
    kept = {_relative_value(value, project_root) for value in kept}
    undeleted: set[str] = set()
    if kept or unwritten:
        # A retained hook config still points at the scripts: they and every
        # retained claim stay until the config itself can be removed.
        rows -= scripts
    else:
        recorded = dict(lockfile.local_deployed_file_hashes) if lockfile is not None else {}
        undeleted = _remove_unedited(project_root, scripts, recorded, targets)
        rows -= undeleted
    rows -= {value for value in rows if _relative_value(value, project_root) in kept}
    if rows and lock_dir is not None:
        _drop_lock_rows(lock_dir, rows)
    failed_paths = tuple(sorted(kept | unwritten | undeleted))
    if reason is not None and not failed_paths:
        message = (
            f"Retired hooks from local bundle '{slug}' ({reason}). "
            f"To keep them, {BUNDLE_HOOKS_RETIRED.clause()}."
        )
        if diagnostics is not None:
            diagnostics.warn(message=message, package=slug)
        elif logger is not None:
            logger.warning(message)
    return BundleHookRetirement(True, failed_paths)


def local_bundle_paths(lock_dir: Path | None) -> frozenset[str]:
    """Return the paths the scope's lockfile records for local bundles."""
    if lock_dir is None:
        return frozenset()
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lockfile = LockFile.read(get_lockfile_path(lock_dir))
    if lockfile is None:
        return frozenset()
    return DeploymentLedgerCodec.local_bundle_values(DeploymentLedgerCodec.from_lockfile(lockfile))


def bundle_hook_cleanup_targets(targets: Any, *, user_scope: bool) -> list[Any]:
    """Return *targets* plus every hook-capable target, for removing a bundle's hooks.

    A bundle installed with ``--target`` may have registered hooks outside the
    manifest's targets; retirement only touches what the bundle owns, so the
    cleanup set covers every target that can hold hooks in this scope.
    """
    from apm_cli.integration.copilot_cowork_paths import CoworkResolutionError
    from apm_cli.integration.targets import KNOWN_TARGETS, scoped_profile_or_none
    from apm_cli.utils.path_security import PathTraversalError

    found = {target.name: target for target in targets or ()}
    for profile in KNOWN_TARGETS.values():
        if profile.name in found or not profile.supports("hooks"):
            continue
        # An unresolvable user root (relative CODEX_HOME, two OneDrive mounts)
        # never received a deploy, so it only drops that target.
        try:
            scoped = scoped_profile_or_none(profile, user_scope=user_scope)
        except (CoworkResolutionError, PathTraversalError, OSError, ValueError):
            continue
        if scoped is not None and scoped.supports("hooks"):
            found[scoped.name] = scoped
    return list(found.values())


def bundle_hook_recheck_targets(targets: Any, *, root: Path, user_scope: bool) -> list[Any]:
    """Return every hook target of the scope the bundle hook re-check covers.

    The manifest's *targets* plus every hook-capable target (see
    :func:`bundle_hook_cleanup_targets`). At project scope an added target
    whose root under *root* is reached through a link is left out: the
    manifest did not choose it, and it may name another project's config.
    """
    from apm_cli.utils.path_security import has_symlink_component

    found = bundle_hook_cleanup_targets(targets, user_scope=user_scope)
    if user_scope:
        return found
    named = {target.name for target in targets or ()}
    base = Path(root)
    return [
        target
        for target in found
        if target.name in named or not has_symlink_component(base, base / target.root_dir)
    ]


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
    """Re-check one scope's local-bundle hooks and canvas extensions.

    Install, update and prune call this. Returns the unadmitted hooks.
    """
    from apm_cli.core.scope import get_apm_dir, is_user_scope

    from .local_bundle_canvas import retire_unadmitted_bundle_canvas

    root, targets, allow_provider = scope_bundle_hook_inputs(
        scope, apm_package, no_policy=no_policy, logger=logger
    )
    # A bundle installed with --target may hold hooks in a target the manifest
    # does not name: every hook target of the scope is re-checked.
    targets = bundle_hook_recheck_targets(targets, root=root, user_scope=is_user_scope(scope))
    lock_dir = get_apm_dir(scope)
    from .local_bundle_markdown_hooks import preflight_bundle_markdown_records

    # Every Markdown hook record is read before the first retirement (#5254).
    preflight_bundle_markdown_records(root, targets, scope=scope, logger=logger)
    hooks = retire_unadmitted_bundle_hooks(
        root, targets, allow_provider, lock_dir=lock_dir, logger=logger, dry_run=dry_run
    )
    retire_unadmitted_bundle_canvas(
        root, targets, allow_provider, lock_dir=lock_dir, logger=logger, dry_run=dry_run
    )
    from .local_bundle_markdown_hooks import retire_unadmitted_bundle_markdown

    retire_unadmitted_bundle_markdown(
        root,
        targets,
        allow_provider,
        lock_dir=lock_dir,
        scope=scope,
        logger=logger,
        dry_run=dry_run,
    )
    return hooks


def installed_local_bundles(scope: Any, apm_package: Any) -> tuple[list[str], int]:
    """Return ``(names, file_count)`` of the local bundles a scope's lockfile records.

    Names come from the hook ownership markers and compile staging slugs a
    bundle leaves; *file_count* counts every bundle-owned path, so a bundle
    without either is still reported (#4318).
    """
    from apm_cli.core.scope import get_apm_dir
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lock_dir = get_apm_dir(scope)
    paths = local_bundle_paths(lock_dir)
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
