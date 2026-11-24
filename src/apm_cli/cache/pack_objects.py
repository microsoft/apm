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
import stat
import struct
import zlib
from pathlib import Path

from .inprocess_fetch import (
    _MAX_DELTA_DEPTH,
    _MAX_INFLATED,
    _TRUE,
    _TYPES,
    _apply_delta,
    _inflate,
    _Refuse,
)

_IDX_MAGIC = b"\xfftOc"
_BASE_CACHE_BYTES = 32 * 1024 * 1024


def _switched_off(name: str, env=None) -> bool:
    """A kill switch reads like ``APM_NO_INPROCESS_FETCH``: 1, true, yes or on,
    in the command's git env or the process env."""
    return any((source.get(name) or "").lower() in _TRUE for source in (env or {}, os.environ))


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
    # Git reads grafts from $GIT_DIR/info/grafts; objects/info/grafts is refused too.
    if any(os.path.lexists(os.path.join(d, "info", "grafts")) for d in (str(base), own)):
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

    def __init__(self, repo: Path, algorithm: str, env=None) -> None:
        self._packs: list[_Pack] | None = None
        if algorithm != "sha1" or _switched_off("APM_NO_INPROCESS_VERIFY", env):
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

    def read(self, oid: str, budget: list[int] | None = None) -> tuple[str, bytes] | None:
        """One object, inflating within *budget* (shared by the caller) or a fresh cap."""
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
                    cap = budget if budget is not None else [_MAX_INFLATED]
                    return self._at(pack, offset, 0, cap)
        except (_Refuse, IndexError, ValueError, struct.error, OSError, zlib.error):
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
    repo: Path, sha: str, env: dict[str, str], *, inflate_budget: int = _MAX_INFLATED
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
        _safe_name,
        _write_file,
    )
    from .inprocess_fetch import _tree_entries as entries_of

    if _switched_off("APM_NO_INPROCESS_CHECKOUT", env):
        return None
    objects = PackObjects(repo, "sha1", env)
    # One inflate budget for the whole tree: every blob is held until written.
    budget = [inflate_budget]
    try:

        def read(oid: str, kind: str) -> bytes:
            found = objects.read(oid, budget)
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
    except (_Refuse, ValueError, IndexError, OSError, zlib.error):
        return None
    finally:
        objects.close()
    # The gate reads git config (a spawn), so it runs only once the pack can serve
    # the whole tree, and still before the first write. It checks the attributes
    # git's own checkout would read: the checkout env, not the parent's.
    from .inprocess_fetch import _require_inert_checkout_attributes

    try:
        _require_inert_checkout_attributes(env)
    except Exception:
        return None
    root = str(repo)
    manifest: dict[str, tuple[str, bool]] = {}
    made: dict[str, tuple[int, int]] = {}
    try:
        # C8: the staged root holds only .git, so mkdir (never exist_ok) and
        # O_EXCL|O_NOFOLLOW prove every component is one this call created.
        for path in sorted(dirs):
            full = os.path.join(root, *path.split("/"))
            os.mkdir(full, 0o755)
            info = os.lstat(full)
            made[path] = (info.st_dev, info.st_ino)
        for path, oid, executable in files:
            _write_file(os.path.join(root, *path.split("/")), blobs[oid], executable)
            manifest[path] = (oid, executable)
        if not _sealed(root, made, set(manifest)):
            raise _Refuse("the staged tree changed while it was written")
        _write_index(root, files)
        head = os.path.join(root, ".git", "HEAD")
        with open(head, "w", encoding="ascii", newline="\n") as handle:
            handle.write(sha + "\n")
    except Exception:
        _discard_worktree(root)
        return None
    return manifest


def _sealed(root: str, made: dict[str, tuple[int, int]], files: set[str]) -> bool:
    """C8 seal: the staged tree holds exactly the directories this call made (same
    device and inode) and the files it wrote, with no link anywhere."""
    seen_dirs: dict[str, tuple[int, int]] = {}
    seen_files: set[str] = set()
    stack = [""]
    while stack:
        rel = stack.pop()
        for name in os.listdir(os.path.join(root, *rel.split("/")) if rel else root):
            path = f"{rel}/{name}" if rel else name
            if path == ".git":
                continue
            info = os.lstat(os.path.join(root, *path.split("/")))
            if stat.S_ISDIR(info.st_mode):
                seen_dirs[path] = (info.st_dev, info.st_ino)
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                seen_files.add(path)
            else:
                return False
    return seen_dirs == made and seen_files == files


def _discard_worktree(root: str) -> None:
    """Remove everything but ``.git`` (and the index this call may have written)."""
    from ..utils.file_ops import robust_rmtree

    with contextlib.suppress(OSError):
        os.unlink(os.path.join(root, ".git", "index"))
    for name in os.listdir(root):
        if name == ".git":
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path) and not os.path.islink(path):
            robust_rmtree(Path(path), ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                os.unlink(path)


def _write_index(root: str, files: list[tuple[str, str, bool]]) -> None:
    """A version-2 index of the written files, as ``git checkout`` leaves it (C6)."""
    import hashlib

    body = bytearray(b"DIRC" + struct.pack(">II", 2, len(files)))
    mask = 0xFFFFFFFF
    for path, oid, executable in sorted(files, key=lambda f: f[0].encode("utf-8")):
        info = os.lstat(os.path.join(root, *path.split("/")))
        name = path.encode("utf-8")
        body += struct.pack(
            ">10I",
            int(info.st_ctime) & mask,
            info.st_ctime_ns % 1_000_000_000,
            int(info.st_mtime) & mask,
            info.st_mtime_ns % 1_000_000_000,
            info.st_dev & mask,
            info.st_ino & mask,
            0o100755 if executable else 0o100644,
            getattr(info, "st_uid", 0) & mask,
            getattr(info, "st_gid", 0) & mask,
            info.st_size & mask,
        )
        body += bytes.fromhex(oid) + struct.pack(">H", min(len(name), 0xFFF)) + name
        body += b"\0" * (8 - (62 + len(name)) % 8)
    body += hashlib.sha1(bytes(body)).digest()  # noqa: S324 -- git's index checksum
    index = os.path.join(root, ".git", "index")
    fd = os.open(
        index,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o644,
    )
    try:
        os.write(fd, bytes(body))
    finally:
        os.close(fd)
