"""Deployed-output oracle: a timed journey must produce the same files on both sides.

A CPU ratio means nothing if the new side does less work. The oracle digests
what a journey leaves behind in each project root (every deployed file, by
content) plus the lockfile, after the timed steps. ``ci_bench report`` flags a
cell INVALID when the new side's output differs from the base side's.

Excluded as implementation detail: ``apm_modules/`` (package store layout),
``.apm-local/`` (private state) and ``.git/``. The work root differs per side
(``work/base`` vs ``work/new``), so it is replaced by a placeholder in every
file before hashing. The lockfile is parsed and digested without
``generated_at`` and ``apm_version``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import yaml

LOCKFILE = "apm.lock.yaml"
_SKIP_DIRS = frozenset({"apm_modules", ".apm-local", ".git"})
_VOLATILE_LOCK_KEYS = ("generated_at", "apm_version")
_PLACEHOLDER = b"<WORK>"
_MAX_DIFF_PATHS = 20


def _work_pattern(work: Path) -> re.Pattern[bytes]:
    native = str(work)
    spellings = {native, work.as_posix(), native.replace("\\", "\\\\")}
    alternatives = sorted((re.escape(s.encode()) for s in spellings if s), key=len, reverse=True)
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile(b"|".join(alternatives), flags)


def _digest(data: bytes, work: re.Pattern[bytes]) -> str:
    return hashlib.sha256(work.sub(_PLACEHOLDER, data)).hexdigest()


def _lock_digest(path: Path, work: re.Pattern[bytes]) -> str | None:
    if not path.is_file():
        return None
    data = yaml.safe_load(work.sub(_PLACEHOLDER, path.read_bytes()).decode("utf-8")) or {}
    if isinstance(data, dict):
        for key in _VOLATILE_LOCK_KEYS:
            data.pop(key, None)
    canonical = json.dumps(data, sort_keys=True, default=str).encode()
    return hashlib.sha256(canonical).hexdigest()


def _files(root: Path, prefix: str, work: re.Pattern[bytes]) -> dict[str, str]:
    """{relative path: content digest} of every deployed entry under *root*."""
    files: dict[str, str] = {}
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in [*names, *(d for d in dirs if os.path.islink(os.path.join(current, d)))]:
            path = Path(current, name)
            rel = prefix + path.relative_to(root).as_posix()
            if rel == prefix + LOCKFILE:
                continue
            if path.is_symlink():
                files[rel] = "link:" + _digest(os.readlink(path).encode(), work)
            elif path.is_file():
                files[rel] = _digest(path.read_bytes(), work)
    return files


def capture(roots: list[Path], work: Path, lock_dirs: list[Path] | None = None) -> dict:
    """The output digest of *roots* after a journey, plus the lockfiles in *lock_dirs*.

    *lock_dirs* defaults to *roots* (project installs); a global install
    deploys under the user home and locks in ``APM_HOME``.
    """
    pattern = _work_pattern(work)
    files: dict[str, str] = {}
    for index, root in enumerate(roots):
        prefix = "" if len(roots) == 1 else f"{index}:{root.name}/"
        if root.is_dir():
            files.update(_files(root, prefix, pattern))
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


def compare(base: list[dict], new: list[dict]) -> dict:
    """Verdict for one cell: ``same``, ``INVALID`` or ``-`` (nothing captured)."""
    base_keys = {k for k in map(key, base) if k is not None}
    new_keys = {k for k in map(key, new) if k is not None}
    if not base_keys or not new_keys:
        return {"verdict": "-"}
    if base_keys == new_keys:
        return {"verdict": "same", "files": next(iter(base_keys))[0]}
    last_base = next((o for o in reversed(base) if o), {})
    last_new = next((o for o in reversed(new) if o), {})
    before, after = last_base.get("manifest") or {}, last_new.get("manifest") or {}
    return {
        "verdict": "INVALID",
        "files": f"{last_base.get('files')}->{last_new.get('files')}",
        "lock_differs": last_base.get("lock") != last_new.get("lock"),
        "missing": sorted(set(before) - set(after))[:_MAX_DIFF_PATHS],
        "added": sorted(set(after) - set(before))[:_MAX_DIFF_PATHS],
        "changed": sorted(p for p in set(before) & set(after) if before[p] != after[p])[
            :_MAX_DIFF_PATHS
        ],
        "base_variants": len(base_keys),
        "new_variants": len(new_keys),
    }
