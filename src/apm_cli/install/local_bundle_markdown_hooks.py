"""Bundle Markdown whose frontmatter declares hooks, retired with its approval (#5254).

An approved local bundle may deploy Markdown files (a command ``.md``, a
``SKILL.md``) whose frontmatter declares ``hooks`` (#5248). As for canvas
extensions, the lockfile's local-bundle rows are the source of truth: every
row whose deployed file declares frontmatter hooks must be admitted by a
committed approval record (``apm-bundle-markdown-hooks.json`` at the target
root) whose key current trust still grants. Every install, update and prune
retires the rest; a row no record names fails closed.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .local_bundle_hook_trust import REASON_NOT_GRANTED, REASON_UNRECORDED


@dataclass
class _Holding:
    """Hooked Markdown rows retired under one bundle and reason."""

    slug: str | None
    reason: str
    files: set[str] = field(default_factory=set)


def _record_path(project_root: Path, target: Any) -> Path:
    from ..integration.hook_ownership import BUNDLE_MARKDOWN_RECORD_FILE

    return Path(project_root) / target.root_dir / BUNDLE_MARKDOWN_RECORD_FILE


def _prefix(target: Any) -> str:
    return Path(target.root_dir).as_posix().rstrip("/") + "/"


def _package_id(approval_key: str) -> str:
    """Return the package id an exact-content bundle key names (``<id>#<ver>@<digest>``)."""
    return approval_key.split("#", 1)[0]


def _key_names_bundle(slug: str, approval_key: str | None) -> bool:
    return bool(approval_key) and _package_id(str(approval_key)) == slug


class BundleMarkdownRecordUnreadable(RuntimeError):
    """A committed bundle Markdown hook record cannot be read; nothing was retired."""

    def __init__(self, record: str, reason: object):
        super().__init__(
            f"Cannot read the local bundle Markdown hook record {record} ({reason}). "
            "Nothing was retired; repair or remove that file, then re-run the command."
        )


def _read_records(project_root: Path, targets: Any) -> dict[Path, dict]:
    """Read every target's committed record before anything changes.

    Raises :class:`BundleMarkdownRecordUnreadable` naming the first record
    that cannot be read, so a retirement never stops half done.
    """
    from ..integration.hook_ownership import BUNDLE_RECORD_MARKDOWN, per_file_bundle_hook_keys
    from .local_bundle_hook_trust import _relative_value

    found: dict[Path, dict] = {}
    for target in targets or ():
        path = _record_path(project_root, target)
        if not path.exists() and not path.is_symlink():
            continue
        try:
            found[path] = per_file_bundle_hook_keys(path, BUNDLE_RECORD_MARKDOWN)
        except (OSError, ValueError, TypeError) as exc:
            raise BundleMarkdownRecordUnreadable(
                _relative_value(str(path), Path(project_root)), exc
            ) from exc
    return found


def _records(project_root: Path, targets: Any) -> dict[str, tuple[set, set[str]]]:
    """Return ``slug -> (keys, files)`` from every target's committed record.

    A record whose key names another bundle than its slug grants nothing, so
    its rows fail closed.
    """
    from .local_bundle_hook_trust import _relative_value

    found: dict[str, tuple[set, set[str]]] = {}
    for records in _read_records(project_root, targets).values():
        for slug, (key, files) in records.items():
            if not _key_names_bundle(slug, key):
                continue  # a record borrowing another bundle's key grants nothing
            keys, held = found.setdefault(slug, (set(), set()))
            keys.add(key)
            held.update(_relative_value(value, Path(project_root)) for value in files)
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
    """Record the hooked Markdown this install of *slug* deployed, with its key.

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
    hooked = {
        _relative_value(value, root)
        for value in deployed
        if markdown_declares_hooks(Path(value) if Path(value).is_absolute() else root / value)
    }
    for target in targets or ():
        files = sorted(value for value in hooked if value.startswith(_prefix(target)))
        if files:
            write_per_file_bundle_record(
                _record_path(root, target),
                slug,
                approval_key,
                files,
                kind=BUNDLE_RECORD_MARKDOWN,
                project_root=root,
                user_scope=scope == InstallScope.USER,
            )


def _hooked_rows(project_root: Path, targets: Any, lock_paths: Collection[str]) -> set[str]:
    """Return the local-bundle lock rows, inside a target root, that declare hooks."""
    from .local_bundle_hook_trust import _relative_value, contained_bundle_path
    from .local_bundle_paths import markdown_declares_hooks

    prefixes = tuple(_prefix(target) for target in targets or ())
    rows: set[str] = set()
    for value in lock_paths:
        path = contained_bundle_path(project_root, value, prefixes)
        if path is not None and path.is_file() and markdown_declares_hooks(path):
            rows.add(_relative_value(value, Path(project_root)))
    return rows


def _denied_slugs(records: dict[str, tuple[set, set[str]]], allow_map: Any) -> set[str]:
    """Return the slugs whose recorded key the current trust explicitly denies."""
    from ..security.executables import EXEC_TYPE_HOOKS
    from .local_bundle_handler import bundle_type_denied

    return {
        slug
        for slug, (keys, _files) in records.items()
        if any(bundle_type_denied(allow_map, key, EXEC_TYPE_HOOKS) for key in keys)
    }


def _unadmitted(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]]],
    lock_paths: Collection[str],
) -> list[_Holding]:
    rows = _hooked_rows(project_root, targets, lock_paths)
    if not rows:
        return []
    allow_map = allow_executables()
    from ..security.executables import (
        EXEC_TYPE_HOOKS,
        exec_map_authorizes_all,
        package_type_approved,
    )

    if exec_map_authorizes_all(allow_map):
        return []
    records = _records(project_root, targets)
    granted = {
        slug
        for slug, (keys, _files) in records.items()
        if all(package_type_approved(allow_map, (key,), EXEC_TYPE_HOOKS) for key in keys)
    }
    denied = _denied_slugs(records, allow_map)
    holdings: dict[tuple[str | None, str], _Holding] = {}
    for row in sorted(rows):
        owners = sorted(slug for slug, (_keys, files) in records.items() if row in files)
        refusing = [slug for slug in owners if slug in denied]
        if not refusing and any(slug in granted for slug in owners):
            continue  # another still-granted bundle admits this path; a deny wins
        slug = (refusing or owners or [None])[0]
        reason = REASON_NOT_GRANTED if owners else REASON_UNRECORDED
        holdings.setdefault((slug, reason), _Holding(slug, reason)).files.add(row)
    return list(holdings.values())


def _remove_exact(project_root: Path, files: Collection[str], recorded: dict[str, str]):
    """Remove each file whose bytes match its lock hash exactly; return ``(removed, kept)``.

    A file with no lock hash, or edited outside APM, is kept.
    """
    from ..integration import write_seam
    from ..utils.content_hash import compute_file_hash

    removed: set[str] = set()
    kept: set[str] = set()
    for value in files:
        path = Path(project_root) / value
        digest = recorded.get(value)
        if digest is None or digest != compute_file_hash(path):
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
        write_per_file_bundle_record(
            path,
            slug,
            key,
            [value for value in files if value not in removed],
            kind=BUNDLE_RECORD_MARKDOWN,
            project_root=Path(project_root),
            user_scope=scope == InstallScope.USER,
        )


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

    A file is removed only when its bytes match its lock hash exactly; a file
    with no hash or edited outside APM is kept and named.
    """
    from ..core.remediation import BUNDLE_HOOKS_RETIRED
    from ..deps.lockfile import LockFile, get_lockfile_path
    from .local_bundle_hook_trust import _drop_lock_rows, local_bundle_paths

    root = Path(project_root)
    holdings = _unadmitted(root, targets, allow_executables, local_bundle_paths(lock_dir))
    if not holdings:
        return []
    lockfile = LockFile.read(get_lockfile_path(lock_dir))
    recorded = {}
    if lockfile is not None:
        from .local_bundle_hook_trust import _relative_value

        recorded = {
            _relative_value(value, root): digest
            for value, digest in lockfile.local_deployed_file_hashes.items()
        }
    retired: list[str] = []
    for holding in holdings:
        label = f"local bundle '{holding.slug}'" if holding.slug else "a local bundle"
        if dry_run:
            if logger is not None:
                logger.warning(
                    f"Would retire {len(holding.files)} Markdown file(s) with frontmatter "
                    f"hooks from {label} ({holding.reason})."
                )
            continue
        removed, kept = _remove_exact(root, holding.files, recorded)
        if removed:
            _drop_lock_rows(
                lock_dir, {v for v in local_bundle_paths(lock_dir) if _rel(v, root) in removed}
            )
            if holding.slug:
                _forget(root, targets, holding.slug, removed, scope)
        retired.extend(sorted(removed))
        if logger is not None:
            message = (
                f"Retired {len(removed)} Markdown file(s) with frontmatter hooks from {label} "
                f"({holding.reason}). To keep them, {BUNDLE_HOOKS_RETIRED.clause()}."
            )
            if kept:
                message += (
                    " Kept, as they are edited or have no recorded hash; review and "
                    f"remove them by hand: {', '.join(sorted(kept))}."
                )
            logger.warning(message)
    return retired


def _rel(value: str, root: Path) -> str:
    from .local_bundle_hook_trust import _relative_value

    return _relative_value(value, root)
