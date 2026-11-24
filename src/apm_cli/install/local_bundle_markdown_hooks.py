"""Bundle Markdown whose frontmatter declares hooks, retired with its approval (#5254).

An approved local bundle may deploy Markdown files (a command ``.md``, a
``SKILL.md``) whose frontmatter declares ``hooks`` (#5248). As for canvas
extensions, the lockfile's local-bundle rows are the source of truth: every
row whose deployed file declares frontmatter hooks must be admitted by a
committed approval record (``apm-bundle-markdown-hooks.json`` at a target
root) whose key current trust still grants and whose recorded content hash
matches the row. Every install, update and prune retires the rest, across
every known target root of the scope; a row no record admits fails closed.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .local_bundle_hook_trust import REASON_NOT_GRANTED, REASON_UNRECORDED

_REASON_CHANGED = "content differs from what its approval admitted"


@dataclass
class _Holding:
    """Hooked Markdown rows retired under one bundle and reason."""

    slug: str | None
    reason: str
    files: set[str] = field(default_factory=set)


@dataclass
class _Record:
    """One record entry: a bundle's key, its files and their admitted content hashes."""

    slug: str
    key: str
    files: set[str] = field(default_factory=set)
    hashes: dict[str, str] = field(default_factory=dict)


class BundleMarkdownRecordUnreadable(RuntimeError):
    """A committed bundle Markdown hook record cannot be read; nothing was retired."""

    def __init__(self, record: str, reason: object):
        super().__init__(
            f"Cannot read the local bundle Markdown hook record {record} ({reason}). "
            "Nothing was retired; repair or remove that file, then re-run the command."
        )


def _record_path(project_root: Path, target: Any) -> Path:
    from ..integration.hook_ownership import BUNDLE_MARKDOWN_RECORD_FILE

    return Path(project_root) / target.root_dir / BUNDLE_MARKDOWN_RECORD_FILE


def _prefixes(target: Any) -> tuple[str, ...]:
    """Return the target root and every primitive ``deploy_root`` it deploys under."""
    prefixes = [Path(target.root_dir).as_posix().rstrip("/") + "/"]
    for mapping in (getattr(target, "primitives", None) or {}).values():
        deploy_root = getattr(mapping, "deploy_root", None)
        if deploy_root:
            prefix = Path(deploy_root, mapping.subdir or "").as_posix().rstrip("/") + "/"
            if prefix not in prefixes:
                prefixes.append(prefix)
    return tuple(prefixes)


def _scan_targets(targets: Any, *, user_scope: bool, logger: Any = None) -> list[Any]:
    """Return *targets* plus every known target of the scope.

    A bundle file deployed for a target the manifest no longer names is still
    re-checked against its record.
    """
    from ..integration.copilot_cowork_paths import CoworkResolutionError
    from ..integration.targets import KNOWN_TARGETS, scoped_profile_or_none
    from ..utils.path_security import PathTraversalError

    found = {target.name: target for target in targets or ()}
    for profile in KNOWN_TARGETS.values():
        if profile.name in found:
            continue
        try:
            scoped = scoped_profile_or_none(profile, user_scope=user_scope)
        except (CoworkResolutionError, PathTraversalError, OSError, ValueError) as exc:
            # A root this scope cannot resolve never received a deploy.
            if logger is not None:
                logger.verbose_detail(
                    f"Skipped target {profile.name} for the bundle Markdown hook re-check: "
                    f"its root cannot be resolved ({exc})"
                )
            continue
        if scoped is not None:
            found[scoped.name] = scoped
    return list(found.values())


def _package_id(approval_key: str) -> str:
    """Return the package id an exact-content bundle key names (``<id>#<ver>@<digest>``)."""
    return approval_key.split("#", 1)[0]


def _key_names_bundle(slug: str, approval_key: str | None) -> bool:
    return bool(approval_key) and _package_id(str(approval_key)) == slug


def _hash_or_none(path: Path) -> str | None:
    """Return the file's content hash, or ``None`` when it cannot be read."""
    from ..utils.content_hash import compute_file_hash

    try:
        return compute_file_hash(path) if path.is_file() else None
    except OSError:
        return None


def _read_records(project_root: Path, targets: Any) -> list[tuple[dict, dict]]:
    """Read every target's committed record before anything changes.

    Returns ``(slug -> (key, files), slug -> {file: hash})`` per record file.
    Raises :class:`BundleMarkdownRecordUnreadable` naming the first record
    that cannot be read, so a retirement never stops half done.
    """
    from ..integration.hook_ownership import (
        BUNDLE_RECORD_MARKDOWN,
        per_file_bundle_file_hashes,
        per_file_bundle_hook_keys,
    )
    from ..utils.path_security import open_regular_file
    from .local_bundle_hook_trust import _relative_value

    found: list[tuple[dict, dict]] = []
    seen: set[Path] = set()
    for target in targets or ():
        path = _record_path(project_root, target)
        if path in seen or (not path.exists() and not path.is_symlink()):
            continue
        seen.add(path)
        try:
            # A FIFO, device or link is never read: it would block or redirect the preflight.
            with open_regular_file(path):
                pass
            found.append(
                (
                    per_file_bundle_hook_keys(path, BUNDLE_RECORD_MARKDOWN),
                    per_file_bundle_file_hashes(path, BUNDLE_RECORD_MARKDOWN),
                )
            )
        except (OSError, ValueError, TypeError) as exc:
            raise BundleMarkdownRecordUnreadable(
                _relative_value(str(path), Path(project_root)), exc
            ) from exc
    return found


def _records(project_root: Path, targets: Any) -> list[_Record]:
    """Return every record entry across every target root, each with its own key.

    Entries are never merged across targets, so one version's withdrawn key
    never judges another version's files. An entry whose key names another
    bundle than its slug grants nothing, so its rows fail closed.
    """
    from .local_bundle_hook_trust import _relative_value

    root = Path(project_root)
    found: list[_Record] = []
    for keyed, hashed in _read_records(root, targets):
        for slug, (key, files) in keyed.items():
            if not _key_names_bundle(slug, key):
                continue  # a record borrowing another bundle's key grants nothing
            found.append(
                _Record(
                    slug,
                    str(key),
                    {_relative_value(value, root) for value in files},
                    {_relative_value(k, root): v for k, v in hashed.get(slug, {}).items()},
                )
            )
    return found


def record_bundle_markdown_hooks(
    scope: Any,
    project_root: Path,
    targets: Any,
    slug: str,
    approval_key: str | None,
    deployed: Collection[str],
    dry_run: bool,
) -> None:
    """Record the hooked Markdown this install deployed, with its key and content hash.

    Hooked Markdown deploys only under an approved key (#5248), so any it
    deployed was admitted by *approval_key*. A target where this install
    deployed none keeps its earlier record; a dry run records nothing.
    """
    if dry_run or not approval_key:
        return
    # Recorded under the package the key names, so the record binds to its key.
    slug = _package_id(approval_key)
    from ..core.scope import InstallScope
    from ..integration.hook_ownership import BUNDLE_RECORD_MARKDOWN, write_per_file_bundle_record
    from .local_bundle_hook_trust import _relative_value
    from .local_bundle_paths import markdown_declares_hooks

    root = Path(project_root)
    hooked: dict[str, str] = {}
    for value in deployed:
        path = Path(value) if Path(value).is_absolute() else root / value
        digest = _hash_or_none(path)
        if digest is not None and markdown_declares_hooks(path):
            hooked[_relative_value(value, root)] = digest
    for target in targets or ():
        files = sorted(value for value in hooked if value.startswith(_prefixes(target)))
        if files:
            write_per_file_bundle_record(
                _record_path(root, target),
                slug,
                approval_key,
                files,
                kind=BUNDLE_RECORD_MARKDOWN,
                project_root=root,
                user_scope=scope == InstallScope.USER,
                file_hashes={value: hooked[value] for value in files},
            )


def _frames(project_root: Path, targets: Any) -> dict[Path, tuple[str, ...]]:
    """Return each containment base with the prefixes judged below it (#5289).

    A target rooted outside the project (user scope with a relocated
    ``CLAUDE_CONFIG_DIR``) records absolute rows: they are judged below the
    root's parent, under the root's own name, as the hook retirement does.
    """
    from .local_bundle_hook_trust import _target_base

    frames: dict[Path, list[str]] = {}
    for target in targets or ():
        prefixes = list(_prefixes(target))
        if Path(target.root_dir).is_absolute():
            frames.setdefault(_target_base(project_root, target), []).append(
                Path(target.root_dir).name + "/"
            )
            prefixes = prefixes[1:]
        frames.setdefault(Path(project_root), []).extend(prefixes)
    return {base: tuple(dict.fromkeys(found)) for base, found in frames.items() if found}


def _contained(frames: dict[Path, tuple[str, ...]], value: str) -> Path | None:
    """The in-root path *value* names in its target's frame, or ``None`` (no link crossed)."""
    from .local_bundle_hook_trust import contained_bundle_path

    for base, prefixes in frames.items():
        path = contained_bundle_path(base, value, prefixes)
        if path is not None:
            return path
    return None


def _hooked_rows(project_root: Path, targets: Any, lock_paths: Collection[str]) -> set[str]:
    """Return the local-bundle lock rows, inside a target root, that declare hooks."""
    from .local_bundle_hook_trust import _relative_value
    from .local_bundle_paths import markdown_declares_hooks

    frames = _frames(Path(project_root), targets)
    rows: set[str] = set()
    for value in lock_paths:
        path = _contained(frames, value)
        if path is not None and path.is_file() and markdown_declares_hooks(path):
            rows.add(_relative_value(value, Path(project_root)))
    return rows


def _denied(entries: list[_Record], allow_map: Any) -> set[int]:
    """Return the indexes of entries whose key the current trust explicitly denies."""
    from ..security.executables import EXEC_TYPE_HOOKS
    from .local_bundle_handler import bundle_type_denied

    return {
        index
        for index, entry in enumerate(entries)
        if bundle_type_denied(allow_map, entry.key, EXEC_TYPE_HOOKS)
    }


def _unadmitted(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    lock_paths: Collection[str],
    lock_hashes: dict[str, str] | None = None,
) -> list[_Holding]:
    rows = _hooked_rows(project_root, targets, lock_paths)
    if not rows:
        return []
    # A provider, or the map an explicit bundle install already resolved.
    allow_map = allow_executables() if callable(allow_executables) else allow_executables
    from ..security.executables import (
        EXEC_TYPE_HOOKS,
        exec_map_authorizes_all,
        package_type_approved,
    )

    if exec_map_authorizes_all(allow_map):
        return []
    entries = _records(project_root, targets)
    granted = {
        index
        for index, entry in enumerate(entries)
        if package_type_approved(allow_map, (entry.key,), EXEC_TYPE_HOOKS)
    }
    denied = _denied(entries, allow_map)
    expected = lock_hashes or {}
    holdings: dict[tuple[str | None, str], _Holding] = {}
    for row in sorted(rows):
        owners = [index for index, entry in enumerate(entries) if row in entry.files]
        current = _hash_or_none(Path(project_root) / row)
        refusing = [index for index in owners if index in denied]
        admitting = [
            index
            for index in owners
            if index in granted
            and current is not None
            and entries[index].hashes.get(row) == current == expected.get(row)
        ]
        if admitting and not refusing:
            continue  # a granted entry admits these exact bytes; a deny wins
        # Name the bundle whose recorded bytes are on disk, the deployer.
        deployer = [index for index in owners if entries[index].hashes.get(row) == current]
        chosen = (refusing or deployer or owners or [None])[0]
        slug = entries[chosen].slug if chosen is not None else None
        if not owners:
            reason = REASON_UNRECORDED
        elif not refusing and not deployer and any(index in granted for index in owners):
            reason = _REASON_CHANGED  # still approved, but not these bytes
        else:
            reason = REASON_NOT_GRANTED
        holdings.setdefault((slug, reason), _Holding(slug, reason)).files.add(row)
    return list(holdings.values())


def _remove_exact(
    project_root: Path, files: Collection[str], recorded: dict[str, str], targets: Any = ()
):
    """Remove each file whose bytes match its lock hash exactly; return ``(removed, kept)``.

    A file with no lock hash, edited outside APM, or unreadable is kept. Each
    path is re-checked against its target's frame right before it is hashed
    and removed, so an ancestor swapped for a link after the scan keeps the
    file (and its lock row); with no *targets*, nothing is removed.
    """
    from ..integration import write_seam

    frames = _frames(Path(project_root), targets)
    removed: set[str] = set()
    kept: set[str] = set()
    for value in files:
        path = _contained(frames, value)
        digest = recorded.get(value)
        if path is None or digest is None or digest != _hash_or_none(path):
            kept.add(value)
            continue
        try:
            write_seam.unlink(path)
        except OSError:
            kept.add(value)
            continue
        removed.add(value)
    return removed, kept


def _forget(project_root: Path, targets: Any, slug: str, removed: set[str], scope: Any) -> None:
    """Drop *removed* from *slug*'s records; an emptied record is removed."""
    from ..core.scope import InstallScope
    from ..integration.hook_ownership import (
        BUNDLE_RECORD_MARKDOWN,
        per_file_bundle_file_hashes,
        per_file_bundle_hook_keys,
        write_per_file_bundle_record,
    )

    for target in targets or ():
        path = _record_path(project_root, target)
        if not path.is_file():
            continue
        records = per_file_bundle_hook_keys(path, BUNDLE_RECORD_MARKDOWN)
        if slug not in records:
            continue
        key, files = records[slug]
        hashes = per_file_bundle_file_hashes(path, BUNDLE_RECORD_MARKDOWN).get(slug, {})
        write_per_file_bundle_record(
            path,
            slug,
            key,
            [value for value in files if value not in removed],
            kind=BUNDLE_RECORD_MARKDOWN,
            project_root=Path(project_root),
            user_scope=scope == InstallScope.USER,
            file_hashes=hashes,
        )


def preflight_bundle_markdown_records(
    project_root: Path, targets: Any, *, scope: Any, logger: Any = None
) -> None:
    """Read every record the re-check will use before any retirement starts.

    Raises :class:`BundleMarkdownRecordUnreadable` so install, update and
    prune stop before the first hook, canvas or Markdown file is removed.
    """
    from ..core.scope import is_user_scope

    user_scope = scope is not None and is_user_scope(scope)
    _read_records(Path(project_root), _scan_targets(targets, user_scope=user_scope, logger=logger))


def retire_unadmitted_bundle_markdown(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    *,
    lock_dir: Path,
    scope: Any = None,
    logger: Any = None,
    dry_run: bool = False,
) -> list[str]:
    """Retire hooked bundle Markdown current trust no longer admits; return its paths.

    Every known target root of the scope is scanned. A file is removed only
    when its bytes match its lock hash exactly; a file with no hash, edited
    outside APM, or unreadable is kept and named.
    """
    from ..core.remediation import BUNDLE_HOOKS_RETIRED, kept_note
    from ..core.scope import is_user_scope
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..utils.diagnostics import printable_ascii_text
    from .local_bundle_hook_trust import _drop_lock_rows, _relative_value, local_bundle_paths

    root = Path(project_root)
    lock_path = get_lockfile_path(lock_dir)
    if lock_path.is_symlink() or not lock_path.is_file():
        return []  # the lockfile owner reports a missing or linked lockfile
    scanned = _scan_targets(targets, user_scope=scope is not None and is_user_scope(scope))
    lockfile = LockFile.read(lock_path)
    if lockfile is None or not lockfile.local_deployed_files:
        return []  # bundle Markdown is recorded there; no row, nothing to retire or read
    recorded = {
        _relative_value(value, root): digest
        for value, digest in lockfile.local_deployed_file_hashes.items()
    }
    lock_paths = local_bundle_paths(lock_dir)
    holdings = _unadmitted(root, scanned, allow_executables, lock_paths, recorded)
    if not holdings:
        return []
    retired: list[str] = []
    for holding in holdings:
        # The slug and file names come from committed records: never print control bytes.
        label = (
            f"local bundle '{printable_ascii_text(holding.slug)}'"
            if holding.slug
            else "a local bundle"
        )
        if dry_run:
            if logger is not None:
                logger.warning(
                    f"Would retire {len(holding.files)} Markdown file(s) with frontmatter "
                    f"hooks from {label} ({holding.reason})."
                )
            continue
        removed, kept = _remove_exact(root, holding.files, recorded, scanned)
        if removed:
            _drop_lock_rows(
                lock_dir, {v for v in lock_paths if _relative_value(v, root) in removed}
            )
            if holding.slug:
                _forget(root, scanned, holding.slug, removed, scope)
        retired.extend(sorted(removed))
        if logger is None:
            continue
        if removed:
            logger.warning(
                f"Retired {len(removed)} Markdown file(s) with frontmatter hooks from {label} "
                f"({holding.reason}). To keep them, {BUNDLE_HOOKS_RETIRED.clause()}."
            )
        if kept:
            logger.warning(
                kept_note(
                    f"{len(kept)} Markdown file(s) with frontmatter hooks from {label} "
                    f"({holding.reason})",
                    "they are edited, unreadable or have no recorded hash",
                    "Review and remove them by hand: "
                    f"{', '.join(printable_ascii_text(path) for path in sorted(kept))}.",
                )
            )
    return retired


def retire_before_bundle_install(
    scope: Any,
    project_root: Path,
    targets: Any,
    allow_executables: Any,
    *,
    logger: Any = None,
    dry_run: bool = False,
) -> None:
    """Run the Markdown preflight and retirement before ``apm install <bundle>`` deploys.

    An explicit bundle install skips the scope reconcile a plain install
    runs, so hooked Markdown a withdrawn key admitted is retired here, by the
    same owner, against the trust map this install resolved (#5308 review).
    """
    from ..core.scope import get_apm_dir

    lock_dir = get_apm_dir(scope) if scope is not None else Path(project_root)
    preflight_bundle_markdown_records(project_root, targets, scope=scope, logger=logger)
    retire_unadmitted_bundle_markdown(
        project_root,
        targets,
        allow_executables,
        lock_dir=lock_dir,
        scope=scope,
        logger=logger,
        dry_run=dry_run,
    )
