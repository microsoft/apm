"""Canonical ownership helpers for merged native hook configuration."""

from __future__ import annotations

import json
from collections import Counter, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from apm_cli.core.scope import manifest_file
from apm_cli.deps.lockfile_write_authority import lockfile_exists
from apm_cli.integration import write_seam
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
    open_regular_file,
    validate_path_segments,
)


def read_hook_object(path: Path, *, regular_only: bool = False) -> dict[str, Any]:
    """Read an optional native hook document without changing its contents.

    With *regular_only* (an APM-owned sidecar) the file is opened without
    following a link and read only if it is a regular file (#5328): a link,
    FIFO, socket or device raises ``OSError`` instead of being read or blocking.
    """
    if regular_only:
        # Always through the safe opener: no exists-then-read window (#5328).
        try:
            with open_regular_file(path) as handle:
                text = handle.read().decode("utf-8")
        except FileNotFoundError:
            return {}
    elif not path.exists():
        return {}
    else:
        text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError("native hook document must contain an object")
    return data


def require_hook_event_lists(
    hooks: dict, events, path: Path, project_root: Path, user_scope: bool = False
) -> None:
    """Ensure the merge target event, and every alias it clears, is a list (#5137).

    The first of *events* is created when missing; a present event (or alias
    spelling) that is not a list raises a ``ValueError`` naming the file and
    that event, with the scope's remedy.
    """
    hooks.setdefault(events[0], [])
    for event in events:
        if event in hooks and not isinstance(hooks[event], list):
            raise ValueError(malformed_hook_event(path, project_root, event, user_scope))


def malformed_hook_event(
    path: Path, project_root: Path, event: str, user_scope: bool = False
) -> str:
    """Name a hook event in a deployed config that is not a list of entries (#5137)."""
    from apm_cli.core.remediation import HOOK_EVENT_MALFORMED
    from apm_cli.utils.diagnostics import printable_ascii_text
    from apm_cli.utils.paths import portable_relpath

    try:
        shown = portable_relpath(path, project_root)
    except ValueError:
        shown = path.as_posix()
    remedy = (
        HOOK_EVENT_MALFORMED.with_command("apm install -g") if user_scope else HOOK_EVENT_MALFORMED
    )
    # The event name comes from package and user files: never print control bytes.
    return (
        f"{printable_ascii_text(shown)}: hook event '{printable_ascii_text(event)}' is not a "
        f"list of hook entries. {remedy.sentence()}"
    )


def reinject_apm_source_from_sidecar(hooks: dict, sidecar_data: dict) -> None:
    """Restore sidecar ownership markers onto matching in-memory entries."""
    for event_name, sidecar_entries in sidecar_data.items():
        if not isinstance(sidecar_entries, list) or not isinstance(hooks.get(event_name), list):
            # A missing or malformed event on disk holds no entry to mark (#5137).
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


def _owned_entry(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("_apm_source"), str)


def _slot_sorted(items: list, owned: Callable[[Any], bool], key: Callable[[Any], str]) -> list:
    """*items* with the owned ones stably sorted by *key* inside the slots they hold."""
    ranked = iter(sorted((item for item in items if owned(item)), key=key))
    return [next(ranked) if owned(item) else item for item in items]


def canonical_hook_order(hooks: dict) -> None:
    """Give APM's merged hook entries one order, whatever order packages were added (#5285).

    In every event, APM-owned entries are stably sorted by ``_apm_source``
    inside the slots they already hold, and events holding only APM entries
    are sorted by name inside their key slots. User entries and events never
    move, so a single, incremental, full or update install writes the same
    bytes for the same set of packages.
    """
    for event_name, entries in hooks.items():
        if isinstance(entries, list):
            hooks[event_name] = _slot_sorted(entries, _owned_entry, lambda e: e["_apm_source"])

    def apm_only(event_name: str) -> bool:
        entries = hooks[event_name]
        return isinstance(entries, list) and bool(entries) and all(map(_owned_entry, entries))

    order = _slot_sorted(list(hooks), apm_only, str)
    if order != list(hooks):
        reordered = {name: hooks[name] for name in order}
        hooks.clear()
        hooks.update(reordered)


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
        sidecar = read_hook_object(config_path.parent / "apm-hooks.json", regular_only=True)
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


def verified_apm_hook_files(project_root: Path, lock) -> frozenset[str]:
    """Return the merged hook configs and sidecars APM provably owns (#5277).

    ``unmanaged_files`` counts these as managed. Its guarantee is
    attestation by committed records: a committer who can forge the lock or
    the apm.yml approval can already add a ``deployed_files`` row, so content
    is not re-bound here. A config and its ``apm-hooks.json`` sidecar count
    only while ownership verifies:

    - both are regular files reached through no link (a FIFO never blocks);
    - both parse as strict JSON objects;
    - every sidecar entry's ``_apm_source`` names a dependency *lock*
      records, or a local bundle whose exact approval key apm.yml grants
      ``hooks: true`` (and does not deny);
    - each event of the config's hooks container holds exactly the sidecar's
      entries, and no event the sidecar lacks (a user-authored hook leaves the
      shared file unmanaged).

    Keys outside the hooks container (a merged ``settings.json`` holds other
    settings) are covered at file granularity. Anything else stays unmanaged
    (fail closed).
    """
    from apm_cli.install.manifest_reconcile import merge_hook_config_projection_specs
    from apm_cli.integration.targets import KNOWN_TARGETS

    try:
        sources = {
            dependency_hook_source_marker(locked.to_dependency_ref())
            for locked in lock.get_all_dependencies()
        }
        approved = _approved_bundle_hook_keys(project_root)
    except (AttributeError, OSError, TypeError, ValueError, KeyError):
        return frozenset()
    managed: set[str] = set()
    specs = merge_hook_config_projection_specs(list(KNOWN_TARGETS.values()))
    for config_rel, (sidecar_rel, container_key) in specs.items():
        if _hook_ownership_verifies(
            project_root, config_rel, sidecar_rel, container_key, (sources, approved)
        ):
            managed.update((config_rel, sidecar_rel))
    return frozenset(managed)


def _approved_bundle_hook_keys(project_root: Path) -> frozenset[str]:
    """Approval keys the committed apm.yml grants ``hooks: true`` and does not deny."""
    from yaml import YAMLError

    from apm_cli.security.executables import package_type_approved, parse_project_executables
    from apm_cli.utils.yaml_io import load_yaml

    path = manifest_file(project_root)
    try:
        data = load_yaml(path) if path.is_file() else None
    except YAMLError:
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    allow, deny, _alias = parse_project_executables(data)
    return frozenset(
        key
        for key in allow
        if package_type_approved(allow, (key,), "hooks")
        and not package_type_approved(deny, (key,), "hooks")
    )


def _strict_object(project_root: Path, path: Path) -> dict[str, Any]:
    """Parse *path* only as a regular file reached through no link; a FIFO never blocks."""
    if has_symlink_component(project_root, path):
        raise ValueError(f"linked hook file: {path.name}")
    with open_regular_file(path) as handle:
        raw = handle.read()
    data = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )
    if not isinstance(data, dict):
        raise ValueError(f"not a JSON object: {path.name}")
    return data


def _content_key(entry: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in entry.items() if k != "_apm_source"}, sort_keys=True)


def _owned_by(entry: object, attesting: tuple[set[str], frozenset[str]]) -> bool:
    """Whether *entry*'s marker names a locked dependency or an approved local bundle."""
    sources, approved = attesting
    marker = entry.get("_apm_source") if isinstance(entry, dict) else None
    parsed = parse_local_bundle_hook_source(marker)
    if parsed is not None:
        return bool(parsed[0]) and parsed[1] is not None and parsed[1] in approved
    return isinstance(marker, str) and marker in sources


def _hook_ownership_verifies(
    project_root: Path,
    config_rel: str,
    sidecar_rel: str,
    container_key: str,
    attesting: tuple[set[str], frozenset[str]],
) -> bool:
    try:
        sidecar = _strict_object(project_root, project_root / sidecar_rel)
        container = _strict_object(project_root, project_root / config_rel).get(container_key)
    except (OSError, ValueError):
        return False
    if not sidecar or not isinstance(container, dict) or set(container) - set(sidecar):
        return False
    for event_name, owned in sidecar.items():
        on_disk = container.get(event_name)
        if not isinstance(owned, list) or not owned or not isinstance(on_disk, list):
            return False
        if not all(_owned_by(entry, attesting) for entry in owned):
            return False
        if not all(isinstance(entry, dict) for entry in on_disk):
            return False
        if Counter(map(_content_key, on_disk)) != Counter(map(_content_key, owned)):
            return False
    return True


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
_RECORD_FILE_HASHES = "file_hashes"
BUNDLE_RECORD_HOOKS = "hooks"
BUNDLE_RECORD_CANVAS = "canvas"
# Bundle Markdown with frontmatter hooks is recorded in its own committed file
# at the target root: a merged hook sidecar is rebuilt from config entries.
BUNDLE_RECORD_MARKDOWN = "markdown"
BUNDLE_MARKDOWN_RECORD_FILE = "apm-bundle-markdown-hooks.json"
_RECORD_KINDS = {
    BUNDLE_RECORD_HOOKS: (PER_FILE_BUNDLE_RECORDS, _RECORD_HOOK_FILES),
    BUNDLE_RECORD_CANVAS: ("local-bundle-canvas", "canvas_files"),
    BUNDLE_RECORD_MARKDOWN: ("local-bundle-markdown-hooks", "markdown_files"),
}


def per_file_bundle_records(
    sidecar_path: Path, kind: str = BUNDLE_RECORD_HOOKS, read=read_hook_object
) -> list[dict[str, Any]]:
    """Return the local-bundle approval records of *kind* a target sidecar holds.

    *read* reads the sidecar; an audit passes one that reads each file once (#5211).
    """
    data = read(sidecar_path, regular_only=True)  # APM's own record (#5328)
    records = data.get(_RECORD_KINDS[kind][0], [])
    if not isinstance(records, list):
        raise TypeError(f"local-bundle {kind} records must be a list")
    return [record for record in records if isinstance(record, dict)]


def per_file_bundle_file_hashes(
    sidecar_path: Path, kind: str = BUNDLE_RECORD_HOOKS
) -> dict[str, dict[str, str]]:
    """Return ``slug -> {file: content hash}`` the *kind* records of a sidecar bind."""
    found: dict[str, dict[str, str]] = {}
    for record in per_file_bundle_records(sidecar_path, kind):
        parsed = parse_local_bundle_hook_source(record.get("_apm_source"))
        hashes = record.get(_RECORD_FILE_HASHES)
        if parsed is not None and isinstance(hashes, dict):
            found[parsed[0]] = {str(k): str(v) for k, v in hashes.items()}
    return found


def per_file_bundle_hook_keys(
    sidecar_path: Path, kind: str = BUNDLE_RECORD_HOOKS, read=read_hook_object
) -> dict[str, tuple[str | None, list[str]]]:
    """Return ``slug -> (approval_key, files)`` for the *kind* records of a sidecar.

    Raises:
        TypeError: A bundle's record lists its files as anything but a list of
            paths: a corrupted record is never read as "no files" (#5144).
    """
    files_key = _RECORD_KINDS[kind][1]
    found: dict[str, tuple[str | None, list[str]]] = {}
    for record in per_file_bundle_records(sidecar_path, kind, read):
        parsed = parse_local_bundle_hook_source(record.get("_apm_source"))
        files = record.get(files_key)
        if parsed is not None:
            if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
                raise TypeError(f"local-bundle {kind} record {files_key} must be a list of paths")
            found[parsed[0]] = (parsed[1], list(files))
    return found


def write_per_file_bundle_record(
    sidecar_path: Path,
    bundle_id: str,
    approval_key: str | None,
    hook_files: list[str],
    kind: str = BUNDLE_RECORD_HOOKS,
    project_root: Path | None = None,
    user_scope: bool = False,
    file_hashes: dict[str, str] | None = None,
) -> None:
    """Record (or, with no *hook_files*, drop) one bundle's *kind* approval record.

    The sidecar is written through :func:`write_hook_sidecar` (#5039);
    *project_root* marks an install write, as there. *file_hashes* binds each
    file to the content the key admitted (#5254).
    """
    records_key, files_key = _RECORD_KINDS[kind]
    # A linked sidecar is no APM record (#5328): it is replaced, never read through.
    existing = sidecar_path.exists() and not sidecar_path.is_symlink()
    kept = [
        record
        for record in (per_file_bundle_records(sidecar_path, kind) if existing else [])
        if record.get("_apm_source") not in LocalBundleMarkers(bundle_id)
    ]
    if hook_files:
        record: dict[str, Any] = {
            "_apm_source": local_bundle_hook_source_marker(bundle_id, approval_key),
            files_key: sorted(hook_files),
        }
        if file_hashes:
            record[_RECORD_FILE_HASHES] = {
                path: file_hashes[path] for path in sorted(hook_files) if path in file_hashes
            }
        kept.append(record)
    data = read_hook_object(sidecar_path, regular_only=True) if existing else {}
    if kept:
        data[records_key] = kept
    else:
        data.pop(records_key, None)
    if data:
        write_seam.mkdir(sidecar_path.parent, parents=True, exist_ok=True)
        write_hook_sidecar(sidecar_path, data, project_root, user_scope)
    elif sidecar_path.exists():
        remove_hook_sidecar(sidecar_path)


def write_hook_sidecar(
    path: Path,
    data: dict,
    project_root: Path | None = None,
    user_scope: bool = False,
) -> None:
    """Write APM's hook ownership sidecar and record the write (#5009).

    The record is the baseline crash recovery verifies the sidecar against.
    *project_root* (an install write) also classifies a sidecar that existed
    without a record; a cleanup rewrite passes none.
    """
    from apm_cli.integration import config_file_provenance as provenance

    record = (
        provenance.recorded_install_write(path, project_root, user_scope)
        if project_root is not None
        else provenance.recorded_config_write(path)
    )
    with record:
        write_seam.atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def remove_hook_sidecar(path: Path) -> None:
    """Remove APM's hook ownership sidecar and forget the record of its writes (#5009)."""
    from apm_cli.integration.config_file_provenance import forget_config_file

    write_seam.unlink(path)
    forget_config_file(path)


def remove_emptied_hook_file(
    json_path: Path, data: dict, defaults: dict[str, Any], root: Path | None, stats: Any = None
) -> bool:
    """Offer a cleaned hook file to the provenance owner; return whether it was deleted (#4867).

    Only a record that APM created the file deletes it; an emptied file kept for
    lack of one is named in ``stats["kept_paths"]`` (#4962).
    """
    from apm_cli.integration.config_file_provenance import delete_emptied_config

    emptied = hook_file_emptied(data, defaults)
    if delete_emptied_config(json_path, root=root, empty=emptied, require_created=True):
        return True
    if emptied and stats is not None:
        stats.setdefault("kept_paths", []).append(json_path.as_posix())
    return False


def hook_file_emptied(data: dict, defaults: dict[str, Any]) -> bool:
    """Whether a cleaned hook file holds only its target's injected defaults (#4852).

    The content fact only: whether the file then goes is decided by
    :func:`~apm_cli.integration.config_file_provenance.delete_emptied_config`
    (#4867). A default matches only with the same type (``true`` is not ``1``).
    """
    return all(
        key in defaults and type(defaults[key]) is type(value) and defaults[key] == value
        for key, value in data.items()
    )
