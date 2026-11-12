"""Carry a package's source licence file with what APM materializes (#3556).

A dependency that is a subdirectory of a repository is materialized from that
subdirectory only, so the repository's ``LICENSE`` (or ``COPYING`` /
``NOTICE``) at an ancestor directory never travels with it. Whole-repo
packages keep their root licence, but skills deployed from below the root
lose it. This module is the single owner of that carry:

* which file names count as a licence file;
* which file is "nearest" (package path up to the repository root);
* copying it verbatim -- bytes are never read, parsed, or interpreted;
* the lockfile record (``path``, ``source``, ``hash``) for each copy;
* the set of carried paths that the package content hash must exclude so
  existing lockfiles keep verifying (the copies are integrity-checked by
  their own recorded hash instead).

APM keeps the declared-license model of #1777: nothing here detects or
infers a licence from file text.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from apm_cli.utils.content_hash import compute_file_hash, verify_package_hash
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
    validate_path_segments,
)

__all__ = [
    "CarriedLicense",
    "carried_copies_intact",
    "carried_license_paths",
    "carry_licenses",
    "is_license_file_name",
    "records_from_lock_data",
    "records_to_lock_data",
    "verify_locked_package",
]

_EXACT_NAMES = {"LICENSE": 0, "COPYING": 2, "NOTICE": 3}
_LICENSE_PREFIX = "LICENSE."
# Never descend into VCS metadata or nested dependency trees when looking for
# skill directories inside a materialized package.
_SKIPPED_DIRS = frozenset({".git", "apm_modules", "node_modules"})
_SKILL_MANIFEST = "SKILL.md"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class CarriedLicense:
    """One verbatim licence copy that APM placed inside a materialized package.

    ``path`` is the copy's location relative to the package root, ``source``
    is the original file relative to the repository root at the locked
    commit, and ``hash`` is :func:`compute_file_hash` of its bytes.
    """

    path: str
    source: str
    hash: str


def _license_rank(name: str) -> int | None:
    upper = name.upper()
    if upper in _EXACT_NAMES:
        return _EXACT_NAMES[upper]
    if upper.startswith(_LICENSE_PREFIX) and len(upper) > len(_LICENSE_PREFIX):
        return 1
    return None


def is_license_file_name(name: str) -> bool:
    """Return whether *name* is ``LICENSE``, ``LICENSE.*``, ``COPYING`` or ``NOTICE``."""
    return _license_rank(name) is not None


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _licence_in(directory: Path) -> Path | None:
    """Return the highest-precedence regular licence file directly in *directory*."""
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    ranked = sorted(
        (rank, name)
        for name in names
        if (rank := _license_rank(name)) is not None and _is_regular_file(directory / name)
    )
    return directory / ranked[0][1] if ranked else None


def _has_licence(directory: Path) -> bool:
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    return any(is_license_file_name(name) for name in names)


def _nearest_licence(start: Path, stop: Path) -> Path | None:
    """Walk from *start* up to and including *stop*; return the nearest licence."""
    current = start
    while True:
        found = _licence_in(current)
        if found is not None:
            return found
        if current in (stop, current.parent):
            return None
        current = current.parent


def _copy_verbatim(source: Path, destination: Path) -> None:
    destination.write_bytes(source.read_bytes())


def _skill_dirs(package_root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, directories, files in os.walk(package_root, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in _SKIPPED_DIRS and not (Path(directory) / name).is_symlink()
        )
        current = Path(directory)
        if current != package_root and _SKILL_MANIFEST in files:
            found.append(current)
    return found


def carry_licenses(
    package_root: Path,
    *,
    repository_root: Path | None = None,
    package_subpath: str = "",
) -> tuple[CarriedLicense, ...]:
    """Copy the nearest licence into *package_root* and next to each skill in it.

    *repository_root* is the checked-out repository the package was carved
    from (subdirectory packages only; ``None`` for whole-repo packages, whose
    root licence is already part of the package). Directories that already
    hold a licence file keep it untouched. Returns the copies made, sorted by
    path; an empty tuple means the package is materialized exactly as before.
    """
    package_root = Path(package_root)
    prefix = PurePosixPath(package_subpath.replace("\\", "/").strip("/"))
    if package_subpath:
        validate_path_segments(package_subpath, context="package path", reject_empty=True)
    carried: dict[str, CarriedLicense] = {}

    if repository_root is not None and not _has_licence(package_root):
        repository_root = repository_root.resolve()
        package_dir = repository_root.joinpath(*prefix.parts)
        ensure_path_within(package_dir, repository_root)
        if package_dir != repository_root:
            found = _nearest_licence(package_dir.parent, repository_root)
            if found is not None:
                ensure_path_within(found, repository_root)
                _copy_verbatim(found, package_root / found.name)
                carried[found.name] = CarriedLicense(
                    path=found.name,
                    source=found.relative_to(repository_root).as_posix(),
                    hash=compute_file_hash(found),
                )

    for skill_dir in _skill_dirs(package_root):
        if _has_licence(skill_dir):
            continue
        found = _nearest_licence(skill_dir.parent, package_root)
        if found is None:
            continue
        ensure_path_within(found, package_root)
        destination = skill_dir / found.name
        _copy_verbatim(found, destination)
        found_rel = found.relative_to(package_root).as_posix()
        origin = carried.get(found_rel)
        rel = destination.relative_to(package_root).as_posix()
        carried[rel] = CarriedLicense(
            path=rel,
            source=origin.source if origin else (prefix / found_rel).as_posix(),
            hash=compute_file_hash(destination),
        )
    return tuple(carried[key] for key in sorted(carried))


def carried_license_paths(records: Iterable[CarriedLicense]) -> frozenset[str]:
    """Return the package-relative paths the package content hash excludes."""
    return frozenset(record.path for record in records)


def carried_copies_intact(package_root: Path, records: Iterable[CarriedLicense]) -> bool:
    """Return whether every recorded copy is a contained regular file with its hash."""
    package_root = Path(package_root)
    for record in records:
        copy = package_root.joinpath(*PurePosixPath(record.path).parts)
        if (
            has_symlink_component(package_root, copy)
            or not _is_regular_file(copy)
            or compute_file_hash(copy) != record.hash
        ):
            return False
    return True


def verify_locked_package(
    package_root: Path, expected_hash: str, records: Iterable[CarriedLicense]
) -> bool:
    """Verify a cached package against its locked hash and carried licence copies."""
    records = tuple(records)
    return carried_copies_intact(package_root, records) and verify_package_hash(
        package_root, expected_hash, exclude=carried_license_paths(records)
    )


def records_to_lock_data(records: Iterable[CarriedLicense]) -> list[dict[str, str]]:
    """Serialize carried licences for ``apm.lock.yaml``."""
    return [{"path": r.path, "source": r.source, "hash": r.hash} for r in records]


def _valid_relpath(value: Any, *, licence_name: bool) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return False
    try:
        validate_path_segments(value, context="carried licence", reject_empty=True)
    except PathTraversalError:
        return False
    return not licence_name or is_license_file_name(PurePosixPath(value).name)


def records_from_lock_data(data: Any) -> tuple[CarriedLicense, ...]:
    """Parse lockfile entries, dropping anything that is not a safe licence copy."""
    if not isinstance(data, list):
        return ()
    records: dict[str, CarriedLicense] = {}
    for item in data:
        if not isinstance(item, Mapping):
            continue
        path, source, digest = item.get("path"), item.get("source"), item.get("hash")
        if (
            _valid_relpath(path, licence_name=True)
            and _valid_relpath(source, licence_name=True)
            and isinstance(digest, str)
            and _SHA256.fullmatch(digest) is not None
        ):
            records.setdefault(path, CarriedLicense(path=path, source=source, hash=digest))
    return tuple(records[key] for key in sorted(records))
