"""Canonical ownership helpers for merged native hook configuration."""

from __future__ import annotations

import json
from collections import Counter, deque
from pathlib import Path
from typing import Any, NoReturn

from apm_cli.core.scope import manifest_file
from apm_cli.deps.lockfile_write_authority import lockfile_exists
from apm_cli.integration import write_seam
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
    validate_path_segments,
)


def read_hook_object(path: Path) -> dict[str, Any]:
    """Read an optional native hook document without changing its contents."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("native hook document must contain an object")
    return data


def reinject_apm_source_from_sidecar(hooks: dict, sidecar_data: dict) -> None:
    """Restore sidecar ownership markers onto matching in-memory entries."""
    for event_name, sidecar_entries in sidecar_data.items():
        if event_name not in hooks or not isinstance(sidecar_entries, list):
            continue
        pool: dict[str, deque[str]] = {}
        for sidecar_entry in sidecar_entries:
            if isinstance(sidecar_entry, dict) and "_apm_source" in sidecar_entry:
                comparable = {
                    key: value
                    for key, value in sorted(sidecar_entry.items())
                    if key != "_apm_source"
                }
                content_key = json.dumps(comparable, sort_keys=True)
                pool.setdefault(content_key, deque()).append(sidecar_entry["_apm_source"])

        for disk_entry in hooks[event_name]:
            if not isinstance(disk_entry, dict) or "_apm_source" in disk_entry:
                continue
            comparable = {
                key: value for key, value in sorted(disk_entry.items()) if key != "_apm_source"
            }
            content_key = json.dumps(comparable, sort_keys=True)
            sources = pool.get(content_key)
            if sources:
                disk_entry["_apm_source"] = sources.popleft()
                if not sources:
                    del pool[content_key]


def read_merged_hook_config(json_path: Path, project_root: Path, *, user_scope: bool) -> dict:
    """Return the existing merged hook config, or ``{}`` when absent or unreadable.

    A project config whose path runs through a symlink is refused, so hook
    entries are never merged into (or read from) a file outside the project.
    """
    if not user_scope and has_symlink_component(project_root, json_path):
        raise PathTraversalError(
            f"Refusing to merge hooks through a symlinked config file: {json_path.name}. "
            "Replace the symlink with a regular file or remove it, then retry."
        )
    if not json_path.exists():
        return {}
    try:
        with open(json_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}


def drop_source_entries(
    hooks: dict,
    markers: frozenset[str] | set[str],
    keep_events: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Remove entries owned by *markers* from every event outside *keep_events*.

    An event this call empties is deleted; user and sibling entries are kept.
    Merged targets keep the events a source re-declared this run, so a hook
    file removed from a package cannot leave its event entries behind (#3665).
    """
    for event_name in list(hooks):
        entries = hooks[event_name]
        if event_name in keep_events or not isinstance(entries, list):
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and e.get("_apm_source") in markers)]
        if len(kept) == len(entries):
            continue
        if kept:
            hooks[event_name] = kept
        else:
            del hooks[event_name]


def drop_dependency_owned_entries(hooks: dict) -> bool:
    """Remove APM-owned entries except local-bundle ones and drop emptied events.

    Return whether any entry was removed.
    """
    modified = False
    for event_name in list(hooks):
        entries = hooks[event_name]
        if not isinstance(entries, list):
            continue
        kept = [
            entry
            for entry in entries
            if not (
                isinstance(entry, dict)
                and "_apm_source" in entry
                and not is_local_bundle_hook_source(entry["_apm_source"])
            )
        ]
        modified = modified or len(kept) != len(entries)
        if kept:
            hooks[event_name] = kept
        else:
            del hooks[event_name]
    return modified


def extract_apm_source_sidecar(hooks: dict) -> dict[str, list[dict[str, Any]]]:
    """Extract ownership metadata and leave native hook entries schema-clean."""
    sidecar: dict[str, list[dict[str, Any]]] = {}
    for event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        owned = [
            dict(entry) for entry in entries if isinstance(entry, dict) and "_apm_source" in entry
        ]
        if owned:
            sidecar[event_name] = owned
        for entry in entries:
            if isinstance(entry, dict):
                entry.pop("_apm_source", None)
    return sidecar


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_json_constant(constant: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {constant}")


def canonicalize_hook_sidecar(sidecar_bytes: bytes) -> bytes:
    """Compare all ownership content without depending on JSON object-key order.

    Keep list order and every field, including ownership markers. Reject
    duplicate keys and non-JSON constants rather than normalizing invalid input.
    """
    # The integrator reads sidecars as UTF-8; json.loads(bytes) would also
    # accept UTF-16/32 files that the next install cannot read.
    sidecar = json.loads(
        sidecar_bytes.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )
    if not isinstance(sidecar, dict):
        raise ValueError("ownership sidecar must be a JSON object")
    sidecar = _without_local_bundle_entries(sidecar)
    return json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _without_local_bundle_entries(hooks: dict[str, Any]) -> dict[str, Any]:
    """Drop local-bundle-owned entries: replay has no source to rebuild them from."""
    kept: dict[str, Any] = {}
    for event_name, entries in hooks.items():
        if not isinstance(entries, list):
            kept[event_name] = entries
            continue
        remaining = [
            entry
            for entry in entries
            if not (
                isinstance(entry, dict) and is_local_bundle_hook_source(entry.get("_apm_source"))
            )
        ]
        if remaining or not entries:
            kept[event_name] = remaining
    return kept


def project_apm_owned_hook_entries(
    config_bytes: bytes,
    sidecar_bytes: bytes,
    event_container_key: str,
) -> bytes:
    """Return a canonical structural projection of the APM-owned hook entries.

    Merged hook configs are shared with users, so comparing their whole JSON
    documents makes unrelated user hooks look like deployment drift. The
    sidecar is APM-owned and maps matching native entries back to their
    ``_apm_source`` markers; retain only that marked slice for comparison.
    Local-bundle entries are excluded, as replay cannot rebuild them.
    """

    config = json.loads(config_bytes, object_pairs_hook=_reject_duplicate_keys)
    sidecar = json.loads(sidecar_bytes, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(config, dict) or not isinstance(sidecar, dict):
        raise ValueError("merged hook config and ownership sidecar must be JSON objects")

    raw_container = config.get(event_container_key)
    if not isinstance(raw_container, dict):
        raise ValueError(f"merged hook config requires a {event_container_key} object")
    if any(not isinstance(entries, list) for entries in raw_container.values()):
        raise ValueError("merged hook events must be lists")
    container = {event_name: list(entries) for event_name, entries in raw_container.items()}
    for event_name, sidecar_entries in sidecar.items():
        if not isinstance(sidecar_entries, list):
            if sidecar_entries:
                raise ValueError("ownership sidecar events must be lists")
            continue
        owned_counts = Counter(
            json.dumps(
                {key: value for key, value in entry.items() if key != "_apm_source"},
                sort_keys=True,
            )
            for entry in sidecar_entries
            if isinstance(entry, dict) and isinstance(entry.get("_apm_source"), str)
        )
        config_counts = Counter(
            json.dumps(entry, sort_keys=True)
            for entry in container.get(event_name, [])
            if isinstance(entry, dict)
        )
        if any(config_counts[key] > count for key, count in owned_counts.items()):
            raise ValueError("ambiguous duplicate APM-owned hook entry")
    reinject_apm_source_from_sidecar(container, sidecar)
    container = _without_local_bundle_entries(container)

    owned: dict[str, list[dict[str, Any]]] = {}
    for event_name, entries in container.items():
        owned_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("_apm_source"), str)
        ]
        if owned_entries:
            owned[event_name] = owned_entries
    return json.dumps(owned, sort_keys=True, separators=(",", ":")).encode("utf-8")


def apm_owned_hook_entries_remain(
    config_path: Path, sidecar_path: Path, event_container_key: str
) -> bool:
    """Return whether a shared hook config still holds APM-owned entries.

    Audit asks this when replay deploys no hooks to the config (#3924).
    Unreadable or non-hook JSON is treated as user-owned.
    """
    from apm_cli.utils.normalization import _normalize

    try:
        owned = project_apm_owned_hook_entries(
            _normalize(config_path.read_bytes()),
            _normalize(sidecar_path.read_bytes()) if sidecar_path.is_file() else b"{}",
            event_container_key,
        )
    except (OSError, ValueError):
        return False
    return owned != b"{}"


def apm_owned_hook_entry_pointers(config_path: Path, event_container_key: str) -> frozenset[str]:
    """Return the JSON pointers of APM-owned entries in a shared hook config.

    Audit uses this so that an entry APM merged is never reported as untracked
    (RA-043). Ownership is the ``_apm_source`` marker, inline or restored from
    the ``apm-hooks.json`` sidecar beside the config. A pointer has the form
    ``/<container>/<event>/<index>``. Unreadable JSON is user-owned.
    """
    try:
        config = read_hook_object(config_path)
        sidecar = read_hook_object(config_path.parent / "apm-hooks.json")
    except (OSError, ValueError, TypeError):
        return frozenset()
    container = config.get(event_container_key)
    if not isinstance(container, dict):
        return frozenset()
    events = {
        event: [dict(entry) if isinstance(entry, dict) else entry for entry in entries]
        for event, entries in container.items()
        if isinstance(entries, list)
    }
    reinject_apm_source_from_sidecar(events, sidecar)

    def escape(value: str) -> str:
        return value.replace("~", "~0").replace("/", "~1")

    return frozenset(
        f"/{escape(event_container_key)}/{escape(event)}/{index}"
        for event, entries in events.items()
        for index, entry in enumerate(entries)
        if isinstance(entry, dict) and isinstance(entry.get("_apm_source"), str)
    )


# "::" never appears in a canonical dependency string, so no package can claim it.
LOCAL_BUNDLE_HOOK_SOURCE_PREFIX = "local-bundle::"


# Separates a bundle's deployed slug from the approval key that admitted it.
_LOCAL_BUNDLE_APPROVAL_SEPARATOR = "::"


def local_bundle_hook_source_marker(bundle_id: str, approval_key: str | None = None) -> str:
    """Return the sidecar marker for hooks a local bundle install registered.

    The only writer of the bundle's approval record: the marker carries the
    exact content-bound key that admitted the hooks, so every later install
    and audit can re-check it (#4027). The slug is the deployed (``--as``)
    name; the key is the bundle's real package key.
    """
    marker = f"{LOCAL_BUNDLE_HOOK_SOURCE_PREFIX}{bundle_id}"
    if approval_key:
        marker = f"{marker}{_LOCAL_BUNDLE_APPROVAL_SEPARATOR}{approval_key}"
    return marker


def parse_local_bundle_hook_source(marker: object) -> tuple[str, str | None] | None:
    """Return ``(slug, approval_key)`` for a local-bundle marker, else ``None``.

    The only reader of the bundle's approval record. A marker written before
    the key was recorded yields ``approval_key=None``.
    """
    if not is_local_bundle_hook_source(marker):
        return None
    slug, _, key = str(marker)[len(LOCAL_BUNDLE_HOOK_SOURCE_PREFIX) :].partition(
        _LOCAL_BUNDLE_APPROVAL_SEPARATOR
    )
    return slug, (key or None)


class LocalBundleMarkers(frozenset):
    """Every marker one local bundle slug wrote, whichever key it recorded."""

    def __new__(cls, bundle_id: str):
        instance = super().__new__(cls, {local_bundle_hook_source_marker(bundle_id)})
        instance.bundle_id = bundle_id
        return instance

    def __contains__(self, marker: object) -> bool:
        parsed = parse_local_bundle_hook_source(marker)
        return parsed is not None and parsed[0] == self.bundle_id


def is_local_bundle_hook_source(marker: object) -> bool:
    """Return whether *marker* names a local bundle rather than a dependency."""
    return isinstance(marker, str) and marker.startswith(LOCAL_BUNDLE_HOOK_SOURCE_PREFIX)


def dependency_hook_source_marker(dependency_ref) -> str:
    """Project dependency identity into a collision-resistant sidecar marker."""
    if dependency_ref.is_local:
        modules_root = Path("apm_modules")
        return dependency_ref.get_install_path(modules_root).relative_to(modules_root).as_posix()
    return dependency_ref.get_canonical_dependency_string()


def lockfile_dependency_identities(
    project_root: Path,
) -> tuple[list[tuple[str, str]], bool]:
    """Return install path and hook marker from a readable lockfile."""
    try:
        from apm_cli.deps.lockfile import LEGACY_LOCKFILE_NAME, LockFile, get_lockfile_path

        lockfile_path = get_lockfile_path(project_root)
        if not lockfile_exists(lockfile_path):
            legacy_path = project_root / LEGACY_LOCKFILE_NAME
            if legacy_path.exists():
                lockfile_path = legacy_path
        if not lockfile_exists(lockfile_path):
            return [], False
        lockfile = LockFile.read(lockfile_path)
        if lockfile is None:
            return [], False
        identities: list[tuple[str, str]] = []
        apm_modules = project_root / "apm_modules"
        for locked_dependency in lockfile.get_all_dependencies():
            dependency = locked_dependency.to_dependency_ref()
            identities.append(
                (
                    dependency.get_install_path(apm_modules).relative_to(apm_modules).as_posix(),
                    dependency_hook_source_marker(dependency),
                )
            )
        return identities, True
    except (AttributeError, OSError, TypeError, ValueError, KeyError):
        return [], False


def safe_dependency_path(apm_modules: Path, relative_path: str) -> Path | None:
    """Return a lockfile dependency path without escaping apm_modules."""
    try:
        validate_path_segments(
            relative_path,
            context="lockfile dependency path",
            reject_empty=True,
        )
        package_path = apm_modules / Path(relative_path)
        ensure_path_within(package_path, apm_modules)
        if has_symlink_component(apm_modules, package_path):
            return None
        return package_path
    except (OSError, PathTraversalError, RuntimeError, TypeError):
        return None


def is_dependency_package_dir(path: Path) -> bool:
    """Return True when a path looks like an installed package root."""
    try:
        hooks = path / "hooks"
        apm_hooks = path / ".apm" / "hooks"
        apm_yml = manifest_file(path)
        skill_md = path / "SKILL.md"
        return (
            (hooks.is_dir() and not hooks.is_symlink())
            or (apm_hooks.is_dir() and not apm_hooks.is_symlink())
            or (apm_yml.is_file() and not apm_yml.is_symlink())
            or (skill_md.is_file() and not skill_md.is_symlink())
        )
    except OSError:
        return False


def add_dependency_source(
    sources: set[str],
    package_path: Path,
    apm_modules: Path,
) -> bool:
    """Add legacy leaf and canonical install-path markers for one package."""
    try:
        if (
            not package_path.is_dir()
            or package_path.is_symlink()
            or not is_dependency_package_dir(package_path)
        ):
            return False
        sources.add(package_path.name)
        sources.add(package_path.relative_to(apm_modules).as_posix())
        return True
    except (OSError, ValueError):
        return False


def child_dependency_dirs(path: Path) -> list[Path]:
    """Return direct non-hidden child dirs without following symlink roots."""
    try:
        if path.is_symlink() or not path.is_dir():
            return []
        return sorted(
            [
                child
                for child in path.iterdir()
                if not child.is_symlink() and child.is_dir() and not child.name.startswith(".")
            ],
            key=lambda child: child.name,
        )
    except OSError:
        return []


def collect_known_subdirectory_sources(
    sources: set[str],
    repo_root: Path,
    apm_modules: Path,
) -> None:
    """Collect sources from known virtual subdirectory layouts."""
    for namespace in ("collections", "skills"):
        for package_path in child_dependency_dirs(repo_root / namespace):
            add_dependency_source(sources, package_path, apm_modules)

    apm_dir = repo_root / ".apm"
    try:
        if apm_dir.is_symlink() or not apm_dir.is_dir():
            return
    except OSError:
        return
    for primitive in ("agents", "commands", "hooks", "instructions", "prompts", "skills"):
        for package_path in child_dependency_dirs(apm_dir / primitive):
            add_dependency_source(sources, package_path, apm_modules)


def collect_remote_dependency_sources(
    sources: set[str],
    namespace: Path,
    apm_modules: Path,
) -> None:
    """Collect fallback sources from explicit remote install layouts."""
    if add_dependency_source(sources, namespace, apm_modules):
        return

    for repo_or_project in child_dependency_dirs(namespace):
        if add_dependency_source(sources, repo_or_project, apm_modules):
            continue
        collect_known_subdirectory_sources(sources, repo_or_project, apm_modules)
        for ado_repo in child_dependency_dirs(repo_or_project):
            if add_dependency_source(sources, ado_repo, apm_modules):
                continue
            collect_known_subdirectory_sources(sources, ado_repo, apm_modules)


def collect_local_dependency_sources(
    sources: set[str],
    local_namespace: Path,
    apm_modules: Path,
) -> None:
    """Collect direct and parent-anchored local package roots."""
    for local_package in child_dependency_dirs(local_namespace):
        if add_dependency_source(sources, local_package, apm_modules):
            continue
        for anchored_package in child_dependency_dirs(local_package):
            add_dependency_source(sources, anchored_package, apm_modules)


def bounded_dependency_hook_sources(apm_modules: Path) -> set[str]:
    """Fallback source scan limited to known apm_modules package layouts."""
    sources: set[str] = set()
    for package_root in child_dependency_dirs(apm_modules):
        if package_root.name == "_local":
            collect_local_dependency_sources(sources, package_root, apm_modules)
            continue
        collect_remote_dependency_sources(sources, package_root, apm_modules)
    return sources


def dependency_hook_sources(project_root: Path) -> set[str]:
    """Return markers corresponding to installed dependency roots."""
    apm_modules = project_root / "apm_modules"
    if not apm_modules.is_dir():
        return set()

    identities, lockfile_readable = lockfile_dependency_identities(project_root)
    if lockfile_readable:
        sources: set[str] = set()
        for relative_path, canonical_identity in identities:
            package_path = safe_dependency_path(apm_modules, relative_path)
            if package_path is None or not add_dependency_source(
                sources,
                package_path,
                apm_modules,
            ):
                continue
            sources.add(canonical_identity)
        return sources
    return bounded_dependency_hook_sources(apm_modules)


def legacy_source_marker_is_unambiguous(
    package_info,
    project_root: Path,
    legacy_marker: str,
) -> bool:
    """Return True when an old leaf marker names only this dependency."""
    try:
        dependency_ref = vars(package_info).get("dependency_ref")
    except TypeError:
        dependency_ref = None
    if dependency_ref is None:
        return False
    identities, lockfile_readable = lockfile_dependency_identities(project_root)
    if not lockfile_readable:
        return False
    current = dependency_hook_source_marker(dependency_ref)
    matching = {
        canonical_identity
        for relative_path, canonical_identity in identities
        if Path(relative_path).name == legacy_marker
    }
    return matching == {current}


# Per-file hook targets (Copilot) keep no merged config, so a local bundle's
# approval record lives in the same sidecar name at the target root, outside
# the hooks directory the harness loads. Canvas extensions a local bundle
# deploys are recorded beside its hooks, with the same marker.
PER_FILE_BUNDLE_RECORDS = "local-bundle-hooks"
_RECORD_HOOK_FILES = "hook_files"
BUNDLE_RECORD_HOOKS = "hooks"
BUNDLE_RECORD_CANVAS = "canvas"
_RECORD_KINDS = {
    BUNDLE_RECORD_HOOKS: (PER_FILE_BUNDLE_RECORDS, _RECORD_HOOK_FILES),
    BUNDLE_RECORD_CANVAS: ("local-bundle-canvas", "canvas_files"),
}


def per_file_bundle_records(
    sidecar_path: Path, kind: str = BUNDLE_RECORD_HOOKS
) -> list[dict[str, Any]]:
    """Return the local-bundle approval records of *kind* a target sidecar holds."""
    data = read_hook_object(sidecar_path)
    records = data.get(_RECORD_KINDS[kind][0], [])
    if not isinstance(records, list):
        raise TypeError(f"local-bundle {kind} records must be a list")
    return [record for record in records if isinstance(record, dict)]


def per_file_bundle_hook_keys(
    sidecar_path: Path, kind: str = BUNDLE_RECORD_HOOKS
) -> dict[str, tuple[str | None, list[str]]]:
    """Return ``slug -> (approval_key, files)`` for the *kind* records of a sidecar."""
    files_key = _RECORD_KINDS[kind][1]
    found: dict[str, tuple[str | None, list[str]]] = {}
    for record in per_file_bundle_records(sidecar_path, kind):
        parsed = parse_local_bundle_hook_source(record.get("_apm_source"))
        files = record.get(files_key)
        if parsed is not None:
            found[parsed[0]] = (
                parsed[1],
                [str(item) for item in files] if isinstance(files, list) else [],
            )
    return found


def write_per_file_bundle_record(
    sidecar_path: Path,
    bundle_id: str,
    approval_key: str | None,
    hook_files: list[str],
    kind: str = BUNDLE_RECORD_HOOKS,
) -> None:
    """Record (or, with no *hook_files*, drop) one bundle's *kind* approval record."""
    records_key, files_key = _RECORD_KINDS[kind]
    kept = [
        record
        for record in (per_file_bundle_records(sidecar_path, kind) if sidecar_path.exists() else [])
        if record.get("_apm_source") not in LocalBundleMarkers(bundle_id)
    ]
    if hook_files:
        kept.append(
            {
                "_apm_source": local_bundle_hook_source_marker(bundle_id, approval_key),
                files_key: sorted(hook_files),
            }
        )
    data = read_hook_object(sidecar_path) if sidecar_path.exists() else {}
    if kept:
        data[records_key] = kept
    else:
        data.pop(records_key, None)
    if data:
        write_seam.mkdir(sidecar_path.parent, parents=True, exist_ok=True)
        write_seam.write_text(sidecar_path, json.dumps(data, indent=2) + "\n")
    elif sidecar_path.exists():
        write_seam.unlink(sidecar_path)
