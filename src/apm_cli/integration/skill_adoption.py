"""Cold-restore adoption of unchanged deployed skill trees (#3391).

Without any local deployment receipt for it, an existing skill directory is
adopted only when its whole file tree equals what this install would write:
every admitted source member after final link rendering, with no extra or
missing files. The verified on-disk hash then stands in for the missing
baseline, so the guarded write path rewrites identical bytes and records a
receipt. Lockfile hashes are never consulted. Any difference returns no
adoption, so the caller keeps its fail-closed refusal.
"""

from __future__ import annotations

import filecmp
import os
from collections.abc import Callable, Iterator
from pathlib import Path

from apm_cli.utils.atomic_io import normalize_crlf_to_lf
from apm_cli.utils.content_hash import compute_file_hash

ResolveLinks = Callable[..., tuple[str, int]]
WalkSource = Callable[[Path], Iterator[tuple[str, list[str], list[str]]]]


def is_skill_dir_identical_to_source(dir_a: Path, dir_b: Path) -> bool:
    """Check if two directory trees have identical raw file contents."""
    return dircmp_equal(filecmp.dircmp(str(dir_a), str(dir_b)))


def dircmp_equal(dcmp) -> bool:
    """Recursively check if dircmp shows identical contents."""
    if dcmp.left_only or dcmp.right_only or dcmp.funny_files:
        return False
    _, mismatches, errors = filecmp.cmpfiles(
        dcmp.left, dcmp.right, dcmp.common_files, shallow=False
    )
    if mismatches or errors:
        return False
    return all(dircmp_equal(sub_dcmp) for sub_dcmp in dcmp.subdirs.values())


def render_skill_markdown(
    resolve_links: ResolveLinks, source_file: Path, target_file: Path, source_root: Path
) -> tuple[str, int]:
    """Return the rendered markdown text and the number of links it rewrites.

    The deployer writes the rendered text only when the count is nonzero;
    otherwise the member stays a byte copy of its source.
    """
    content = source_file.read_text(encoding="utf-8")
    return resolve_links(content, source_file, target_file, preserved_source_root=source_root)


def _expected_bytes(
    resolve_links: ResolveLinks, source_file: Path, target_file: Path, source_root: Path
) -> bytes:
    if target_file.suffix == ".md":
        rendered, count = render_skill_markdown(
            resolve_links, source_file, target_file, source_root
        )
        if count:
            return normalize_crlf_to_lf(rendered).encode("utf-8")
    return source_file.read_bytes()


def adoptable_skill_members(
    source: Path,
    destination: Path,
    *,
    walk_source: WalkSource,
    ignore: Callable[[str, list[str]], list[str]],
    resolve_links: ResolveLinks,
) -> frozenset[Path]:
    """Return destination members when the tree equals this install's output, else empty.

    Call before copying so earlier writes in the same pass cannot complete a
    tree that was not identical on disk.
    """
    if destination.is_symlink() or not destination.is_dir():
        return frozenset()
    expected: dict[Path, Path] = {}
    for directory, directories, filenames in walk_source(source):
        ignored = set(ignore(directory, [*directories, *filenames]))
        directories[:] = [name for name in directories if name not in ignored]
        for name in filenames:
            if name not in ignored:
                member = Path(directory) / name
                expected[destination / member.relative_to(source)] = member
    actual: set[Path] = set()
    for directory, directories, filenames in os.walk(destination):
        if any((Path(directory) / name).is_symlink() for name in (*directories, *filenames)):
            return frozenset()
        actual.update(Path(directory) / name for name in filenames)
    if not expected or actual != set(expected):
        return frozenset()
    try:
        for target, member in expected.items():
            if member.is_symlink() or not member.is_file() or not target.is_file():
                return frozenset()
            if target.read_bytes() != _expected_bytes(resolve_links, member, target, source):
                return frozenset()
    except (OSError, UnicodeError):
        return frozenset()
    return frozenset(expected)


def adoption_baselines(
    source: Path,
    destination: Path,
    *,
    walk_source: WalkSource,
    ignore: Callable[[str, list[str]], list[str]],
    resolve_links: ResolveLinks,
    owned: Callable[[Path], bool],
) -> dict[Path, str]:
    """Map adoptable members to verified on-disk hashes when every member is claimed.

    Identical files without an ownership claim never gain local authority.
    """
    members = adoptable_skill_members(
        source, destination, walk_source=walk_source, ignore=ignore, resolve_links=resolve_links
    )
    if not all(owned(path) for path in members):
        return {}
    return {path: compute_file_hash(path) for path in members}
