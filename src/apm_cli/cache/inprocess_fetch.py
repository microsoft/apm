"""In-process fetch of one pinned github.com commit for a cold checkout (#4939).

Cold fetch of one pinned commit without a git child process: one git
smart-HTTP protocol v2 ``fetch`` (``want <sha>``, ``deepen 1``) over the
pooled in-process session of ``anonymous_ref_listing``, the pack parsed
here, every object hashed to its id, the tree walked from the pinned commit,
and the worktree plus a minimal valid ``.git`` (loose objects, HEAD, pinned
config) written into a staged directory APM created.

Integrity: every byte written is the content of an object whose id was
computed here, reached from a commit whose computed id equals the pin, so the
result is the same verdict ``checkout_verify`` reaches with ``git cat-file``.
Any doubt (non-github URL, auth, git config, attributes, links, submodules,
odd names, protocol surprise) returns ``None`` and the caller runs git.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import subprocess
import threading
import time
import zlib
from collections.abc import Mapping
from pathlib import Path

_log = logging.getLogger(__name__)

# W3d F3/F4/F5: bounded response, inflate, objects, delta chains and wall time.
_MAX_BODY = 64 * 1024 * 1024
_MAX_INFLATED = 1024 * 1024 * 1024
_MAX_OBJECT = 256 * 1024 * 1024
_MAX_DELTA_DEPTH = 50
_TIMEOUT = (10.0, 30.0)
_DEADLINE_SECONDS = 120.0
_CONFIG_UNSAFE = frozenset('\r\n"[\\')
_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_WRITE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)
_BAD_NAMES = {"", ".", "..", ".gitattributes", ".gitmodules"}
_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(10)}


class _Refuse(Exception):
    """Not provably equal to git's checkout: run git."""


class _Pending(Exception):
    """A ref-delta whose base is not resolved yet."""


def _pkt(text: str) -> bytes:
    data = text.encode("ascii")
    return b"%04x" % (len(data) + 4) + data


def _request_body(sha: str) -> bytes:
    return (
        _pkt("command=fetch\n")
        + b"0001"
        + _pkt("no-progress\n")
        + _pkt("ofs-delta\n")
        + _pkt("deepen 1\n")
        + _pkt(f"want {sha}\n")
        + _pkt("done\n")
        + b"0000"
    )


def _pack_from_response(body: bytes) -> bytes:
    pos = 0
    in_pack = False
    pack = bytearray()
    end = len(body)
    while pos < end:
        if pos + 4 > end:
            raise _Refuse("truncated pkt-line")
        try:
            size = int(body[pos : pos + 4], 16)
        except ValueError:
            raise _Refuse("bad pkt-line") from None
        if size in (0, 1, 2):
            pos += 4
            if size == 0 and in_pack:
                break
            continue
        if size < 5 or pos + size > end:
            raise _Refuse("bad pkt-line length")
        payload = body[pos + 4 : pos + size]
        pos += size
        if not in_pack:
            line = payload.rstrip(b"\n")
            if line == b"packfile":
                in_pack = True
            elif line.startswith(b"ERR "):
                raise _Refuse("server error")
            continue
        band = payload[0]
        if band == 1:
            pack += payload[1:]
        elif band == 3:
            raise _Refuse("server error band")
    if not in_pack:
        raise _Refuse("no packfile section")
    return bytes(pack)


def _inflate(buf: memoryview, pos: int, size: int, budget: list[int]) -> tuple[bytes, int]:
    """Inflate one object of declared *size*: never more output than declared (F3)."""
    if size > _MAX_OBJECT:
        raise _Refuse("object exceeds the size cap")
    budget[0] -= size
    if budget[0] < 0:
        raise _Refuse("pack inflates past the total cap")
    inflater = zlib.decompressobj()
    parts = []
    produced = 0
    step = max(size + 64, 512)
    while not inflater.eof:
        if pos >= len(buf):
            raise _Refuse("truncated object")
        chunk = buf[pos : pos + step]
        data = inflater.decompress(chunk, size + 1 - produced)
        produced += len(data)
        parts.append(data)
        if produced > size:
            raise _Refuse("object inflates past its declared size")
        if inflater.eof:
            pos += len(chunk) - len(inflater.unused_data)
        else:
            pos += len(chunk) - len(inflater.unconsumed_tail)
            step *= 2
    if produced != size:
        raise _Refuse("object size mismatch")
    return b"".join(parts), pos


def _varint(delta: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = delta[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _apply_delta(base: bytes, delta: bytes, budget: list[int]) -> bytes:
    src_size, pos = _varint(delta, 0)
    dst_size, pos = _varint(delta, pos)
    if src_size != len(base):
        raise _Refuse("delta base size mismatch")
    if dst_size > _MAX_OBJECT:
        raise _Refuse("delta result exceeds the size cap")
    budget[0] -= dst_size
    if budget[0] < 0:
        raise _Refuse("pack inflates past the total cap")
    out = bytearray()
    end = len(delta)
    while pos < end:
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = length = 0
            for bit in range(4):
                if op & (1 << bit):
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    length |= delta[pos] << (8 * bit)
                    pos += 1
            length = length or 0x10000
            if offset + length > len(base):
                raise _Refuse("delta copy out of range")
            out += base[offset : offset + length]
        elif op:
            out += delta[pos : pos + op]
            pos += op
        else:
            raise _Refuse("delta opcode 0")
    if len(out) != dst_size:
        raise _Refuse("delta result size mismatch")
    return bytes(out)


def _object_id(kind: str, data: bytes) -> str:
    digest = hashlib.new("sha1", usedforsecurity=False)
    digest.update(b"%s %d\x00" % (kind.encode("ascii"), len(data)))
    digest.update(data)
    return digest.hexdigest()


def parse_pack(pack: bytes) -> dict[str, tuple[str, bytes]]:
    """``{object id: (kind, content)}`` of every object, each id computed here."""
    if len(pack) < 32 or pack[:4] != b"PACK":
        raise _Refuse("not a pack")
    if hashlib.new("sha1", pack[:-20], usedforsecurity=False).digest() != pack[-20:]:
        raise _Refuse("pack checksum mismatch")
    version = int.from_bytes(pack[4:8], "big")
    count = int.from_bytes(pack[8:12], "big")
    if version not in (2, 3):
        raise _Refuse("pack version")
    buf = memoryview(pack)[:-20]
    budget = [_MAX_INFLATED]
    pos = 12
    raw: dict[int, tuple[int, bytes, object]] = {}
    order: list[int] = []
    for _ in range(count):
        start = pos
        byte = buf[pos]
        pos += 1
        kind = (byte >> 4) & 7
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = buf[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        base: object = None
        if kind == 6:
            byte = buf[pos]
            pos += 1
            offset = byte & 0x7F
            while byte & 0x80:
                byte = buf[pos]
                pos += 1
                offset = ((offset + 1) << 7) | (byte & 0x7F)
            base = start - offset
        elif kind == 7:
            base = bytes(buf[pos : pos + 20]).hex()
            pos += 20
        elif kind not in _TYPES:
            raise _Refuse("unknown object type")
        data, pos = _inflate(buf, pos, size, budget)
        raw[start] = (kind, data, base)
        order.append(start)
    if pos != len(buf):
        raise _Refuse("trailing pack bytes")
    resolved: dict[int, tuple[str, bytes]] = {}
    depth: dict[int, int] = {}  # deltas between each resolved object and its full base
    by_id: dict[str, tuple[str, bytes]] = {}
    offset_of_id: dict[str, int] = {}

    def resolve(start: int) -> tuple[str, bytes]:
        """Iterative, depth-capped (F4): follow bases, then apply deltas outward."""
        chain: list[int] = []
        current = start
        while current not in resolved:
            kind, data, base = raw[current]
            if kind in _TYPES:
                resolved[current] = (_TYPES[kind], data)
                depth[current] = 0
                break
            chain.append(current)
            if len(chain) > _MAX_DELTA_DEPTH:
                raise _Refuse("delta chain exceeds the depth cap")
            if kind == 6:
                if base not in raw:
                    raise _Refuse("ofs-delta base missing")
                current = base
            else:
                if base not in offset_of_id:
                    raise _Pending
                current = offset_of_id[base]
        base_kind, content = resolved[current]
        if depth[current] + len(chain) > _MAX_DELTA_DEPTH:
            raise _Refuse("delta chain exceeds the depth cap")
        for link in reversed(chain):
            content = _apply_delta(content, raw[link][1], budget)
            resolved[link] = (base_kind, content)
            depth[link] = depth[current] + len(chain) - chain.index(link)
        return resolved[start]

    pending: list[int] = []
    for start in order:
        kind = raw[start][0]
        if kind not in _TYPES:
            pending.append(start)
            continue
        obj_kind, content = resolve(start)
        oid = _object_id(obj_kind, content)
        offset_of_id[oid] = start
        by_id[oid] = (obj_kind, content)
    while pending:
        progress: set[int] = set()
        for start in pending:
            try:
                obj_kind, content = resolve(start)
            except _Pending:
                continue
            oid = _object_id(obj_kind, content)
            offset_of_id[oid] = start
            by_id[oid] = (obj_kind, content)
            progress.add(start)
        if not progress:
            raise _Refuse("unresolvable deltas (thin pack)")
        pending = [start for start in pending if start not in progress]
    return by_id


def _tree_entries(data: bytes):
    pos = 0
    end = len(data)
    while pos < end:
        space = data.index(b" ", pos)
        nul = data.index(b"\x00", space)
        mode = data[pos:space].decode("ascii")
        name = data[space + 1 : nul]
        oid = data[nul + 1 : nul + 21].hex()
        pos = nul + 21
        yield mode, name, oid


def _safe_name(raw: bytes) -> str:
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Refuse("non-utf8 name") from None
    from .checkout_verify import _names_git_dir

    lowered = name.lower()
    if (
        _names_git_dir(name)
        or lowered in _BAD_NAMES
        or any(c in name for c in '/\\:\x00*?"<>|')
        or any(ord(c) < 0x20 for c in name)
        or name.endswith((".", " "))
        or name != name.strip()
        or lowered.split(".", 1)[0] in _RESERVED
    ):
        raise _Refuse("name needs git's handling")
    return name


def _plan(
    objects: dict[str, tuple[str, bytes]], sha: str
) -> tuple[list[tuple[str, str, bool]], set[str]]:
    """``([(posix path, blob id, executable)], reached ids)`` of the commit's full worktree."""
    commit = objects.get(sha)
    if commit is None or commit[0] != "commit":
        raise _Refuse("pinned commit not in pack")
    first = commit[1].split(b"\n", 1)[0]
    if not first.startswith(b"tree "):
        raise _Refuse("commit names no tree")
    files: list[tuple[str, str, bool]] = []
    reached = {sha}
    stack = [("", first[5:].decode("ascii"))]
    while stack:
        prefix, tree_id = stack.pop()
        reached.add(tree_id)
        tree = objects.get(tree_id)
        if tree is None or tree[0] != "tree":
            raise _Refuse("tree missing")
        for mode, raw_name, oid in _tree_entries(tree[1]):
            name = _safe_name(raw_name)
            path = f"{prefix}{name}"
            if mode == "40000":
                stack.append((path + "/", oid))
            elif mode in ("100644", "100755"):
                blob = objects.get(oid)
                if blob is None or blob[0] != "blob":
                    raise _Refuse("blob missing")
                files.append((path, oid, mode == "100755"))
                reached.add(oid)
            else:
                raise _Refuse(f"mode {mode} needs git's handling")
    return files, reached


def _gate(url: str, env: Mapping[str, str]) -> tuple[str, str | None, str]:
    """``(upload-pack URL, proxy, CA)`` when git's request is provably anonymous and equal."""
    from ..deps import anonymous_ref_listing as anon

    try:
        path = anon._repository_path(url)
        for name in anon._FALLBACK_ENV:
            if env.get(name):
                raise _Refuse(f"{name} is set")
        config, child_env, prefixes = anon._effective_config(env)
        if any(url.lower().startswith(prefix.lower()) for prefix in prefixes):
            raise _Refuse("insteadOf rewrite")
        allow = child_env.get("GIT_ALLOW_PROTOCOL")
        if allow is not None and "https" not in allow.split(":"):
            raise _Refuse("https not allowed")
        proxy = anon._proxy(child_env)
        ca = anon._verify(child_env, config)
    except anon._Fallback as reason:
        raise _Refuse(str(reason)) from None
    if url != f"https://github.com{path}":
        raise _Refuse("url is not the validated github.com form")
    _require_no_attribute_sources(child_env)
    return f"https://github.com{path}/git-upload-pack", proxy, ca


_ATTR_MEMO = "inprocess-fetch-attribute-sources"
_attr_lock = threading.Lock()


def _attribute_sources(child_env: Mapping[str, str]) -> bool:
    """Whether any attributes source outside the commit could change git's checkout bytes (F7).

    APM's git checkout always runs with the line-ending and symlink pins of
    ``utils/git_env`` (``_safe_git_args``), which outrank system and global
    config, so git writes raw blob bytes unless an attribute says
    ``text``/``eol``/``filter``. The commit's own ``.gitattributes`` refuses
    in the plan and a fresh staged clone has no ``info/attributes``; what is
    left is the global attributes file (``core.attributesFile`` or its XDG
    default) and the system one.
    """
    from ..utils.git_env import _read_effective_git_config, get_git_executable

    entries = _read_effective_git_config(dict(child_env)).entries
    for entry in entries:
        if entry.key.lower() == "core.attributesfile" and entry.value:
            return True
    xdg = child_env.get("XDG_CONFIG_HOME")
    home = child_env.get("HOME") or child_env.get("USERPROFILE")
    base = xdg if xdg else (os.path.join(home, ".config") if home else None)
    if base and os.path.lexists(os.path.join(base, "git", "attributes")):
        return True
    if (child_env.get("GIT_ATTR_NOSYSTEM") or "").lower() in ("1", "true", "yes", "on"):
        return False
    probe = subprocess.run(
        [get_git_executable(), "var", "GIT_ATTR_SYSTEM"],
        env=dict(child_env),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    system = probe.stdout.strip()
    if probe.returncode != 0 or not system:
        return True
    return os.path.lexists(system)


def _require_no_attribute_sources(child_env: Mapping[str, str]) -> None:
    from ..core.invocation_memo import invocation_memo

    # Kept for one command only (dropped after lifecycle scripts); no memo outside one.
    memo = invocation_memo(_ATTR_MEMO)
    fingerprint = tuple(sorted(child_env.items()))
    cached = None
    if memo is not None:
        with _attr_lock:
            cached = memo.get(fingerprint)
    if cached is None:
        try:
            cached = _attribute_sources(child_env)
        except Exception:
            cached = True
        if memo is not None:
            with _attr_lock:
                memo[fingerprint] = cached
    if cached:
        raise _Refuse("an attributes source outside the commit may change checkout bytes")


_BREAKER_MEMO = "inprocess-fetch-rate-limited"


def _rate_limited() -> bool:
    """Whether github.com rate-limited an in-process fetch earlier in this command."""
    from ..core.invocation_memo import invocation_memo

    memo = invocation_memo(_BREAKER_MEMO)
    return bool(memo)


def _admit(status: int, headers: Mapping[str, str]) -> None:
    """Refuse a non-200 answer; a rate-limit answer also sends the rest of the command to git.

    429, 403, ``Retry-After`` or ``X-RateLimit-Remaining: 0`` trip the breaker:
    no retry, no sleep, and no further in-process request in this command.
    """
    from ..core.invocation_memo import invocation_memo

    limited = (
        status in (403, 429)
        or headers.get("Retry-After") is not None
        or (headers.get("X-RateLimit-Remaining") or "").strip() == "0"
    )
    if limited:
        memo = invocation_memo(_BREAKER_MEMO)
        if memo is not None:
            memo["tripped"] = True
        raise _Refuse(f"rate limited (HTTP {status}); git serves the rest of this command")
    if status != 200:
        raise _Refuse(f"HTTP {status}")


def _post(upload_url: str, proxy: str | None, ca: str, body: bytes) -> bytes:
    from ..deps.anonymous_ref_listing import _session_for_refs
    from ..utils.http_credentials import origin_bounded_request

    with origin_bounded_request(
        _session_for_refs(ca),
        "POST",
        upload_url,
        base_url=upload_url,
        headers={
            "Content-Type": "application/x-git-upload-pack-request",
            "Accept": "application/x-git-upload-pack-result",
            "Git-Protocol": "version=2",
        },
        data=body,
        max_hops=0,
        ambient_credentials=False,
        proxies={"https": proxy} if proxy else {},
        verify=ca,
        timeout=_TIMEOUT,
        stream=True,
    ) as response:
        _admit(response.status_code, response.headers)
        kind = response.headers.get("Content-Type", "").split(";")[0].strip()
        if kind != "application/x-git-upload-pack-result":
            raise _Refuse("not an upload-pack result")
        chunks = []
        total = 0
        deadline = time.monotonic() + _DEADLINE_SECONDS
        for chunk in response.iter_content(65536):
            if time.monotonic() > deadline:
                raise _Refuse("fetch exceeds the wall deadline")
            total += len(chunk)
            if total > _MAX_BODY:
                raise _Refuse("pack exceeds the size cap")
            chunks.append(chunk)
        return b"".join(chunks)


def _write_file(path: str, data: bytes, executable: bool) -> None:
    fd = os.open(path, _WRITE_FLAGS, 0o755 if executable else 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _write_git_dir(
    staged: Path,
    url: str,
    sha: str,
    objects: dict[str, tuple[str, bytes]],
    reached: set[str],
    pins: tuple[tuple[str, str], ...],
) -> None:
    for value in (url, *(part for pin in pins for part in pin)):
        if any(c in _CONFIG_UNSAFE for c in value):
            raise _Refuse("config value needs quoting")
    git_dir = os.path.join(str(staged), ".git")
    objects_dir = os.path.join(git_dir, "objects")
    for sub in ("refs/heads", "refs/tags", "objects/info", "objects/pack"):
        os.makedirs(os.path.join(git_dir, *sub.split("/")), exist_ok=True)
    made: set[str] = set()
    for oid in reached:
        kind, data = objects[oid]
        fan = os.path.join(objects_dir, oid[:2])
        if fan not in made:
            os.makedirs(fan, exist_ok=True)
            made.add(fan)
        header = b"%s %d\x00" % (kind.encode("ascii"), len(data))
        _write_file(os.path.join(fan, oid[2:]), zlib.compress(header + data, 1), False)
    _write_file(os.path.join(git_dir, "HEAD"), f"{sha}\n".encode("ascii"), False)
    _write_file(os.path.join(git_dir, "shallow"), f"{sha}\n".encode("ascii"), False)
    sections: dict[str, list[str]] = {"core": ["\trepositoryformatversion = 0"]}
    for key, value in pins:
        section, _, name = key.partition(".")
        sections.setdefault(section, []).append(f"\t{name} = {value}")
    lines = []
    for section, entries in sections.items():
        lines.append(f"[{section}]")
        lines.extend(entries)
    lines.append('[remote "origin"]')
    lines.append(f"\turl = {url}")
    _write_file(os.path.join(git_dir, "config"), ("\n".join(lines) + "\n").encode("utf-8"), False)


def fetch_worktree(
    url: str,
    sha: str,
    staged: Path,
    env: Mapping[str, str] | None,
    pins: tuple[tuple[str, str], ...],
) -> dict[str, tuple[str, bool]] | None:
    """Write commit *sha* of *url* into empty *staged*; ``{path: (blob id, exec)}`` or ``None``."""
    if len(sha) != 40:
        return None
    if _rate_limited():
        _log.debug("in-process fetch skipped: rate limited earlier in this command")
        return None
    try:
        upload_url, proxy, ca = _gate(url, env if env is not None else os.environ)
        body = _post(upload_url, proxy, ca, _request_body(sha))
        objects = parse_pack(_pack_from_response(body))
        files, reached = _plan(objects, sha)
        root = str(staged)
        made = {root}
        manifest: dict[str, tuple[str, bool]] = {}
        for rel, oid, executable in files:
            parts = rel.split("/")
            directory = root
            for part in parts[:-1]:
                directory = os.path.join(directory, part)
                if directory not in made:
                    os.mkdir(directory, 0o755)
                    made.add(directory)
            _write_file(os.path.join(directory, parts[-1]), objects[oid][1], executable)
            manifest[rel] = (oid, executable)
        _write_git_dir(staged, url, sha, objects, reached, pins)
        if os.name != "nt":
            info = os.lstat(root)
            if not stat.S_ISDIR(info.st_mode):
                raise _Refuse("staged root changed")
        return manifest
    except _Refuse as reason:
        _log.debug("in-process fetch falls back to git for %s: %s", url, reason)
        return None
    except Exception as exc:
        _log.debug("in-process fetch falls back to git for %s: %r", url, exc)
        return None
