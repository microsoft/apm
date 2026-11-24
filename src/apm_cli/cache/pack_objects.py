"""Read Git objects from a repository's packs in process (lever 2a, research).

Serves ``checkout_verify``: every object it returns is re-hashed by the caller
against the id asked for, so the pack is a byte source, never a trust root.
Anything outside the plain case answers ``None`` and the caller asks
``git cat-file`` instead: SHA-256, a ``.git`` file, nested alternates, a
multi-pack index, replace refs or grafts, a loose or missing object, a
64-bit offset, a REF_DELTA base outside its pack, a delta chain past 50, or
any parse or size-cap refusal.
"""

from __future__ import annotations

import bisect
import contextlib
import mmap
import os
import struct
from pathlib import Path

from .inprocess_fetch import (
    _MAX_DELTA_DEPTH,
    _MAX_INFLATED,
    _TYPES,
    _apply_delta,
    _inflate,
    _Refuse,
)

_IDX_MAGIC = b"\xfftOc"
_BASE_CACHE_BYTES = 32 * 1024 * 1024


class _Unsupported(Exception):
    """The repository is outside the plain case: every read goes to git."""


class _Pack:
    def __init__(self, idx_path: str, pack_path: str) -> None:
        with open(idx_path, "rb") as handle:
            idx = handle.read()
        if idx[:4] != _IDX_MAGIC or struct.unpack(">I", idx[4:8])[0] != 2:
            raise _Unsupported("not a v2 pack index")
        fanout = struct.unpack(">256I", idx[8 : 8 + 1024])
        count = fanout[255]
        names = 8 + 1024
        offsets = names + 20 * count + 4 * count
        if len(idx) < offsets + 4 * count + 40:
            raise _Unsupported("truncated pack index")
        self._idx = idx
        self._fanout = fanout
        self._names = names
        self._offsets = offsets
        self._file = open(pack_path, "rb")  # noqa: SIM115
        try:
            self.data = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            self._file.close()
            raise
        if bytes(self.data[:4]) != b"PACK":
            self.close()
            raise _Unsupported("not a pack")
        self.view = memoryview(self.data)
        self.bases: dict[int, tuple[str, bytes]] = {}
        self.base_bytes = 0

    def offset(self, raw: bytes) -> int | None:
        first = raw[0]
        lo = self._fanout[first - 1] if first else 0
        hi = self._fanout[first]
        names = self._names
        idx = self._idx

        class _Key:
            def __getitem__(self, i: int) -> bytes:
                start = names + 20 * i
                return idx[start : start + 20]

            def __len__(self) -> int:
                return hi

        i = bisect.bisect_left(_Key(), raw, lo, hi)
        if i >= hi or idx[names + 20 * i : names + 20 * i + 20] != raw:
            return None
        value = struct.unpack(">I", idx[self._offsets + 4 * i : self._offsets + 4 * i + 4])[0]
        if value & 0x80000000:
            raise _Refuse("64-bit pack offset")
        return value

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.view.release()
        with contextlib.suppress(Exception):
            self.data.close()
        with contextlib.suppress(Exception):
            self._file.close()


def _objects_dirs(repo: Path) -> list[str]:
    git = repo / ".git"
    if git.is_file():
        raise _Unsupported("gitdir file")
    base = git if git.is_dir() else repo
    own = str(base / "objects")
    for marker in ("info/grafts",):
        if os.path.lexists(os.path.join(own, marker)):
            raise _Unsupported("grafts")
    dirs = [own]
    alternates = os.path.join(own, "info", "alternates")
    if os.path.lexists(alternates):
        with open(alternates, encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                path = line if os.path.isabs(line) else os.path.join(own, line)
                if os.path.lexists(os.path.join(path, "info", "alternates")):
                    raise _Unsupported("nested alternates")
                dirs.append(os.path.normpath(path))
    for objects in dirs:
        if os.path.lexists(os.path.join(objects, "info", "http-alternates")):
            raise _Unsupported("http alternates")
        with contextlib.suppress(FileNotFoundError):
            if any(n.endswith(".promisor") for n in os.listdir(os.path.join(objects, "pack"))):
                raise _Unsupported("promisor pack")
    for root in {str(base), *(os.path.dirname(d) for d in dirs[1:])}:
        config = os.path.join(root, "config")
        if os.path.isfile(config):
            with open(config, "rb") as handle:
                if b"promisor" in handle.read().lower():
                    raise _Unsupported("promisor remote")
        if os.path.isdir(os.path.join(root, "refs", "replace")):
            raise _Unsupported("replace refs")
        packed = os.path.join(root, "packed-refs")
        if os.path.isfile(packed):
            with open(packed, "rb") as handle:
                if b"refs/replace/" in handle.read():
                    raise _Unsupported("replace refs")
    return dirs


class PackObjects:
    """``read(oid, kind) -> bytes | None`` from the packs; ``None`` means ask git."""

    def __init__(self, repo: Path, algorithm: str) -> None:
        self._packs: list[_Pack] | None = None
        if algorithm != "sha1" or os.environ.get("APM_NO_INPROCESS_VERIFY"):
            return
        packs: list[_Pack] = []
        try:
            for objects in _objects_dirs(Path(repo)):
                pack_dir = os.path.join(objects, "pack")
                if os.path.lexists(os.path.join(pack_dir, "multi-pack-index")):
                    raise _Unsupported("multi-pack index")
                with contextlib.suppress(FileNotFoundError):
                    for name in sorted(os.listdir(pack_dir)):
                        if name.endswith(".idx"):
                            pack = os.path.join(pack_dir, name[:-4] + ".pack")
                            if os.path.isfile(pack):
                                packs.append(_Pack(os.path.join(pack_dir, name), pack))
        except (_Unsupported, _Refuse, OSError, ValueError, struct.error):
            for pack in packs:
                pack.close()
            return
        self._packs = packs

    def read(self, oid: str) -> tuple[str, bytes] | None:
        if not self._packs:
            return None
        try:
            raw = bytes.fromhex(oid)
        except ValueError:
            return None
        if len(raw) != 20:
            return None
        try:
            for pack in self._packs:
                offset = pack.offset(raw)
                if offset is not None:
                    return self._at(pack, offset, 0, [_MAX_INFLATED])
        except (_Refuse, IndexError, ValueError, struct.error, OSError):
            return None
        return None

    def _at(self, pack: _Pack, offset: int, depth: int, budget: list[int]) -> tuple[str, bytes]:
        cached = pack.bases.get(offset)
        if cached is not None:
            return cached
        if depth > _MAX_DELTA_DEPTH:
            raise _Refuse("delta chain too deep")
        buf = pack.view
        pos = offset
        byte = buf[pos]
        pos += 1
        kind = (byte >> 4) & 7
        size = byte & 15
        shift = 4
        while byte & 0x80:
            byte = buf[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        if kind in _TYPES:
            data, _ = _inflate(buf, pos, size, budget)
            result = (_TYPES[kind], data)
        elif kind == 6:
            byte = buf[pos]
            pos += 1
            back = byte & 0x7F
            while byte & 0x80:
                byte = buf[pos]
                pos += 1
                back = ((back + 1) << 7) | (byte & 0x7F)
            if back <= 0 or back > offset:
                raise _Refuse("bad delta offset")
            base_kind, base = self._at(pack, offset - back, depth + 1, budget)
            delta, _ = _inflate(buf, pos, size, budget)
            result = (base_kind, _apply_delta(base, delta, budget))
        elif kind == 7:
            base_off = pack.offset(bytes(buf[pos : pos + 20]))
            if base_off is None:
                raise _Refuse("REF_DELTA base outside the pack")
            base_kind, base = self._at(pack, base_off, depth + 1, budget)
            delta, _ = _inflate(buf, pos + 20, size, budget)
            result = (base_kind, _apply_delta(base, delta, budget))
        else:
            raise _Refuse("unknown object type")
        if depth and pack.base_bytes + len(result[1]) <= _BASE_CACHE_BYTES:
            pack.bases[offset] = result
            pack.base_bytes += len(result[1])
        return result

    def close(self) -> None:
        for pack in self._packs or ():
            pack.close()
        self._packs = None


def materialize_tree(
    repo: Path, sha: str, env: dict[str, str]
) -> dict[str, tuple[str, bool]] | None:
    """Write the pinned commit's worktree into *repo* from its packs (lever C), or ``None``.

    ``None`` means nothing was written and git must check out. Every object is
    re-hashed; names, modes and caps follow B1's ``_plan``; links, gitlinks,
    ``.gitattributes``/``.gitmodules`` and any attribute or core/filter config
    source refuse before the first write.
    """
    from ..utils.git_object_id import git_object_id
    from .inprocess_fetch import (
        _MAX_ENTRIES,
        _MAX_PATH,
        _require_no_attribute_sources,
        _safe_name,
        _write_file,
    )
    from .inprocess_fetch import _tree_entries as entries_of

    if os.environ.get("APM_NO_INPROCESS_CHECKOUT"):
        return None
    for key, value in env.items():
        if key.startswith("GIT_CONFIG_KEY_") and value.lower().startswith(("core.", "filter.")):
            return None
    try:
        _require_no_attribute_sources(env)
    except _Refuse:
        return None
    objects = PackObjects(repo, "sha1")
    try:

        def read(oid: str, kind: str) -> bytes:
            found = objects.read(oid)
            if found is None or found[0] != kind or git_object_id("sha1", kind, found[1]) != oid:
                raise _Refuse("object not served")
            return found[1]

        first = read(sha, "commit").split(b"\n", 1)[0]
        if not first.startswith(b"tree "):
            raise _Refuse("commit names no tree")
        dirs: list[str] = []
        files: list[tuple[str, str, bool]] = []
        stack = [("", first[5:].decode("ascii"))]
        count = 0
        while stack:
            prefix, tree_id = stack.pop()
            for mode, raw_name, oid in entries_of(read(tree_id, "tree")):
                count += 1
                if count > _MAX_ENTRIES or len(prefix) + len(raw_name) > _MAX_PATH:
                    raise _Refuse("cap")
                path = prefix + _safe_name(raw_name)
                if mode == "40000":
                    dirs.append(path)
                    stack.append((path + "/", oid))
                elif mode in ("100644", "100755"):
                    files.append((path, oid, mode == "100755"))
                else:
                    raise _Refuse("mode needs git")
        blobs = {oid: read(oid, "blob") for _, oid, _ in files}
    except (_Refuse, ValueError, IndexError, OSError):
        return None
    finally:
        objects.close()
    root = str(repo)
    for path in sorted(dirs):
        os.mkdir(os.path.join(root, *path.split("/")), 0o755)
    manifest: dict[str, tuple[str, bool]] = {}
    for path, oid, executable in files:
        _write_file(os.path.join(root, *path.split("/")), blobs[oid], executable)
        manifest[path] = (oid, executable)
    head = os.path.join(root, ".git", "HEAD")
    with open(head, "w", encoding="ascii", newline="\n") as handle:
        handle.write(sha + "\n")
    return manifest
