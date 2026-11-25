"""Deployed-output oracle: a timed journey must produce the same files on both sides.

A CPU ratio means nothing if the new side does less work. The oracle digests
what a journey leaves behind in its output roots (every deployed file, by
content) plus the lockfile, after the timed steps: the project roots, the user
home for a global install, the archive for ``pack`` (members, not bytes) and
the consumer project for ``install-bundle``. ``ci_bench report`` flags a
cell INVALID when the new side's output differs from the base side's.

Excluded as implementation detail: ``apm_modules/`` (package store layout),
``.apm-local/`` (private state) and ``.git/``. The work root differs per side
(``work/base`` vs ``work/new``), so it is replaced by a placeholder in every
file before hashing. Every lockfile is parsed and digested without
``generated_at``, ``apm_version`` and ``packed_at`` (also inside archives).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import zipfile
from pathlib import Path

import yaml

LOCKFILE = "apm.lock.yaml"
_SKIP_DIRS = frozenset({"apm_modules", ".apm-local", ".git"})
_VOLATILE_LOCK_KEYS = frozenset({"generated_at", "apm_version", "packed_at"})
_PLACEHOLDER = b"<WORK>"
_MAX_DIFF_PATHS = 20
_ARCHIVE_SUFFIXES = frozenset({".gz", ".tgz", ".tar", ".zip"})


def _work_pattern(work: Path) -> re.Pattern[bytes]:
    native = str(work)
    spellings = {native, work.as_posix(), native.replace("\\", "\\\\")}
    alternatives = sorted((re.escape(s.encode()) for s in spellings if s), key=len, reverse=True)
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile(b"|".join(alternatives), flags)


def _digest(data: bytes, work: re.Pattern[bytes]) -> str:
    return hashlib.sha256(work.sub(_PLACEHOLDER, data)).hexdigest()


def _stable(data: object) -> object:
    """*data* without the run-specific keys (timestamps, CLI version) at any depth."""
    if isinstance(data, dict):
        return {k: _stable(v) for k, v in data.items() if k not in _VOLATILE_LOCK_KEYS}
    if isinstance(data, list):
        return [_stable(v) for v in data]
    return data


def _lock_bytes_digest(raw: bytes, work: re.Pattern[bytes]) -> str:
    data = yaml.safe_load(work.sub(_PLACEHOLDER, raw).decode("utf-8")) or {}
    canonical = json.dumps(_stable(data), sort_keys=True, default=str).encode()
    return hashlib.sha256(canonical).hexdigest()


def _lock_digest(path: Path, work: re.Pattern[bytes]) -> str | None:
    return _lock_bytes_digest(path.read_bytes(), work) if path.is_file() else None


def _member_digest(name: str, data: bytes, work: re.Pattern[bytes]) -> str:
    if name.rsplit("/", 1)[-1] == LOCKFILE:
        return "lock:" + _lock_bytes_digest(data, work)
    return _digest(data, work)


def _archive_digest(path: Path, work: re.Pattern[bytes]) -> str | None:
    """Member names and contents of a tar or zip archive (no timestamps or modes)."""
    members: list[str] = []
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                handle = archive.extractfile(member) if member.isfile() else None
                content = (
                    _member_digest(member.name, handle.read(), work)
                    if handle
                    else member.type.decode()
                )
                members.append(f"{member.name}\0{content}")
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                digest = _member_digest(info.filename, archive.read(info), work)
                members.append(f"{info.filename}\0{digest}")
    else:
        return None
    return "archive:" + hashlib.sha256("\n".join(sorted(members)).encode()).hexdigest()


def _entry_digest(path: Path, work: re.Pattern[bytes]) -> str | None:
    if path.is_symlink():
        return "link:" + _digest(os.readlink(path).encode(), work)
    if not path.is_file():
        return None
    if path.name == LOCKFILE:
        return "lock:" + str(_lock_digest(path, work))
    if path.suffix in _ARCHIVE_SUFFIXES:
        archive = _archive_digest(path, work)
        if archive is not None:
            return archive
    return _digest(path.read_bytes(), work)


def _files(root: Path, prefix: str, work: re.Pattern[bytes], skip: set[Path]) -> dict[str, str]:
    """{relative path: content digest} of every deployed entry under *root*."""
    files: dict[str, str] = {}
    if not root.exists() or root in skip:
        return files
    if not root.is_dir():
        digest = _entry_digest(root, work)
        return {prefix.rstrip("/"): digest} if digest is not None else files
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and Path(current, d) not in skip)
        for name in [*names, *(d for d in dirs if os.path.islink(os.path.join(current, d)))]:
            path = Path(current, name)
            digest = _entry_digest(path, work)
            if digest is not None:
                files[prefix + path.relative_to(root).as_posix()] = digest
    return files


def capture(
    roots: list[Path],
    work: Path,
    lock_dirs: list[Path] | None = None,
    skip: tuple[Path, ...] = (),
) -> dict:
    """The output digest of *roots* after a journey, plus the lockfiles in *lock_dirs*.

    A root may be a directory or a single file. Lockfiles anywhere are digested
    canonically. *lock_dirs* defaults to *roots* (project installs); a global
    install deploys under the user home and locks in ``APM_HOME``. *skip*
    prunes subtrees that hold private state.
    """
    pattern = _work_pattern(work)
    pruned = set(skip)
    files: dict[str, str] = {}
    for index, root in enumerate(roots):
        prefix = "" if len(roots) == 1 else f"{index}:{root.name}/"
        files.update(_files(root, prefix, pattern, pruned))
    locks = {
        str(index): _lock_digest(directory / LOCKFILE, pattern)
        for index, directory in enumerate(roots if lock_dirs is None else lock_dirs)
    }
    tree = hashlib.sha256(
        "\n".join(f"{rel}\0{digest}" for rel, digest in sorted(files.items())).encode()
    ).hexdigest()
    lock = hashlib.sha256(json.dumps(locks, sort_keys=True).encode()).hexdigest()
    return {"files": len(files), "tree": tree, "lock": lock, "manifest": files}


def key(output: dict | None) -> tuple | None:
    """The comparable part of a captured output (no manifest)."""
    if not output:
        return None
    return (output.get("files"), output.get("tree"), output.get("lock"))


def compare(base: list[tuple[str, dict]], new: list[tuple[str, dict]]) -> dict:
    """Verdict for one cell from ``(pair id, output)`` lists of each side.

    ``same`` needs every pair's base and new output to match and every pair to
    share one output: a side whose output varies across pairs is not a stable
    measurement either. ``INCOMPLETE`` when some pair lacks an output on
    either side (parity unproven); ``-`` only when no pair captured one (a
    harness without the oracle).
    """
    base_by_pair = {pair: key(o) for pair, o in base if key(o) is not None}
    new_by_pair = {pair: key(o) for pair, o in new if key(o) is not None}
    if not base_by_pair and not new_by_pair:
        return {"verdict": "-"}
    pairs = {pair for pair, _ in base} | {pair for pair, _ in new}
    uncovered = sorted(p for p in pairs if p not in base_by_pair or p not in new_by_pair)
    if uncovered:
        return {
            "verdict": "INCOMPLETE",
            "uncovered_pairs": len(uncovered),
            "pairs": len(pairs),
            "first": uncovered[0],
        }
    paired = sorted(pairs)
    variants = {base_by_pair[p] for p in paired} | {new_by_pair[p] for p in paired}
    if len(variants) == 1:
        return {"verdict": "same", "files": next(iter(variants))[0]}
    mismatched = [p for p in paired if base_by_pair[p] != new_by_pair[p]]
    pair = mismatched[0] if mismatched else paired[-1]
    outputs = {side: dict(rows)[pair] for side, rows in (("base", base), ("new", new))}
    before = outputs["base"].get("manifest") or {}
    after = outputs["new"].get("manifest") or {}
    return {
        "verdict": "INVALID",
        "files": f"{outputs['base'].get('files')}->{outputs['new'].get('files')}",
        "lock_differs": outputs["base"].get("lock") != outputs["new"].get("lock"),
        "missing": sorted(set(before) - set(after))[:_MAX_DIFF_PATHS],
        "added": sorted(set(after) - set(before))[:_MAX_DIFF_PATHS],
        "changed": sorted(p for p in set(before) & set(after) if before[p] != after[p])[
            :_MAX_DIFF_PATHS
        ],
        "mismatched_pairs": len(mismatched),
        "pairs": len(paired),
        "variants": len(variants),
    }
