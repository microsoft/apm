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
from collections.abc import Callable, Mapping
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
            if len(out) + length > dst_size:
                raise _Refuse("delta result size mismatch")
            out += base[offset : offset + length]
        elif op:
            if len(out) + op > dst_size:
                raise _Refuse("delta result size mismatch")
            out += delta[pos : pos + op]
            pos += op
        else:
            raise _Refuse("delta opcode 0")
    if len(out) != dst_size:
        raise _Refuse("delta result size mismatch")
    return bytes(out)


def _object_id(kind: str, data: bytes) -> str:
    from ..utils.git_object_id import git_object_id

    return git_object_id("sha1", kind, data)


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


# W3d W2/W3: git config keys that change how git would make this anonymous
# request force git; everything else is inert for it. ``credential.*`` is
# consulted by git only after a 401, which B1 never answers (W1).
_TRUST_KEYS = ("http.sslbackend", "http.sslcainfo")
_FORCING_PREFIXES = ("url.", "http.", "protocol.https.")
_FORCING_KEYS = frozenset({"protocol.allow", "core.gitproxy", "core.askpass"})
# W3d W4: attributes outside the commit are inert only with these tokens.
_INERT_ATTRIBUTES = frozenset({"diff", "merge", "binary", "delta", "export-ignore", "export-subst"})
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
# W3d R2: git would fsck what it receives; B1 does not, so git keeps that request.
_FSCK_KEYS = frozenset({"transfer.fsckobjects", "fetch.fsckobjects"})


def _classify(entries) -> tuple[dict[str, str], tuple[str, ...]]:
    """``(trust values, insteadOf prefixes)``; raises ``_Refuse`` naming the forcing key."""
    from ..deps.anonymous_ref_listing import _list_key

    values: dict[str, str] = {}
    headers: dict[str, list[tuple[str, str]]] = {}
    prefixes: list[str] = []
    for entry in entries:
        key = entry.key.lower()
        if key.startswith("credential."):
            continue
        if key in _FSCK_KEYS:
            if entry.value.strip().lower() not in _FALSE:
                raise _Refuse(f"git config {entry.key}")
            continue
        list_key = _list_key(entry.key)
        if list_key is not None:
            live = headers.get(list_key[1], [])
            headers[list_key[1]] = [*live, (entry.key, entry.value)] if entry.value else []
        elif key in _TRUST_KEYS:
            values[key] = entry.value
        elif key.startswith("url.") and key.endswith(".insteadof"):
            prefixes.append(entry.value)
        elif key.startswith("url.") and key.endswith(".pushinsteadof"):
            continue
        elif key.startswith(_FORCING_PREFIXES) or key in _FORCING_KEYS:
            raise _Refuse(f"git config {entry.key}")
    for live in headers.values():
        if live:
            raise _Refuse(f"git config {live[-1][0]}")
    return values, tuple(prefixes)


def _frozen_entries(child_env: Mapping[str, str]):
    """The config git sees under *child_env*, read from the env ``git_network_env`` froze.

    ``git_network_env`` re-reads the user's config on every call (through the
    file-fingerprinted #5283 memo) and freezes it into ``GIT_CONFIG_KEY_n`` /
    ``GIT_CONFIG_VALUE_n`` with system and global files disabled, so these
    entries are exactly what ``git config --list`` would print for a fetch
    run from outside any repository. ``None`` when the env is not frozen.
    """
    from types import SimpleNamespace

    if (
        child_env.get("GIT_CONFIG_NOSYSTEM") != "1"
        or child_env.get("GIT_CONFIG_GLOBAL") != os.devnull
        or child_env.get("GIT_CONFIG_PARAMETERS")
    ):
        return None
    try:
        count = int(child_env.get("GIT_CONFIG_COUNT") or "0")
    except ValueError:
        return None
    entries = []
    for index in range(count):
        key = child_env.get(f"GIT_CONFIG_KEY_{index}")
        value = child_env.get(f"GIT_CONFIG_VALUE_{index}")
        if key is None or value is None:
            return None
        entries.append(SimpleNamespace(key=key, value=value))
    return entries


_NETWORK_ENV_MEMO = "inprocess-fetch-network-env"
_network_env_lock = threading.Lock()
_PROBE_URL = "https://github.com/apm/probe"


def _config_stamp(paths: tuple[str, ...]) -> tuple | None:
    """The #5283 content identity of *paths* (absent files stamp as absent); ``None`` on an include (G2/G3)."""
    from ..utils import git_config_memo

    stamp = git_config_memo.fingerprint(paths)
    if stamp is None:
        return None
    for path, exists, _size, _digest in stamp:
        if exists:
            try:
                with open(path, "rb") as handle:
                    if b"include" in handle.read().lower():
                        return None
            except OSError:
                return None
    return stamp


def _config_paths(env: Mapping[str, str]) -> tuple[str, ...] | None:
    """System and global config files git reads under *env*, shared with the #5283 memo's cache."""
    from ..core.invocation_memo import invocation_memo
    from ..utils import git_config_memo
    from ..utils.git_env import get_git_executable

    memo = invocation_memo(git_config_memo._MEMO)
    key = (
        "paths",
        get_git_executable(),
        tuple((name, env.get(name)) for name in git_config_memo._PATH_ENV),
    )
    if memo is not None:
        with git_config_memo._lock:
            known = memo.get(key)
        if known is not None:
            return known
    paths = git_config_memo.git_config_paths(env)
    if paths is not None and memo is not None:
        with git_config_memo._lock:
            memo[key] = paths
    return paths


def _binary_identity(git: str) -> tuple | None:
    try:
        info = os.stat(git)
    except OSError:
        return None
    return (git, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _config_env(*envs: Mapping[str, str]) -> bool:
    """Whether config carried in GIT_CONFIG_* env names an include (G3: then nothing is kept)."""
    for env in envs:
        if "include" in (env.get("GIT_CONFIG_PARAMETERS") or "").lower():
            return True
        for name, value in env.items():
            if name.startswith("GIT_CONFIG_KEY_") and value.lower().startswith(
                ("include.", "includeif.")
            ):
                return True
    return False


def _network_env(env: Mapping[str, str]) -> dict[str, str]:
    """``git_network_env`` for the probe URL, kept for one command while nothing it read changed.

    W3d G1-G4: the key is the URL, the input env, the process env (the parent
    config read) and the git binary's identity; every config file git reads
    (child and parent env) is re-stamped on each lookup with the #5283
    fingerprint; an include, or config carried in GIT_CONFIG_* env, disables
    keeping. Only the frozen env is kept: the caller classifies it, checks
    rewrites, fallback env, allowed protocols and trust on every call.
    Single-flight, so racing workers probe once.
    """
    from ..core.invocation_memo import invocation_memo
    from ..utils.git_env import get_git_executable, git_network_env, git_subprocess_env

    memo = invocation_memo(_NETWORK_ENV_MEMO)
    parent_env = git_subprocess_env()
    binary = _binary_identity(get_git_executable())
    if memo is None or binary is None or _config_env(env, os.environ):
        return git_network_env(_PROBE_URL, dict(env))
    key = (_PROBE_URL, tuple(sorted(env.items())), tuple(sorted(os.environ.items())), binary)
    with _network_env_lock:
        paths = memo.get(("paths", key))
        if paths is None:
            child = _config_paths(env)
            parent = _config_paths(parent_env)
            if child is None or parent is None:
                return git_network_env(_PROBE_URL, dict(env))
            paths = tuple(sorted({*child, *parent}))
            memo[("paths", key)] = paths
        before = _config_stamp(paths)
        kept = memo.get(key)
        if kept is not None and before is not None and kept[0] == before:
            return dict(kept[1])
        child_env = git_network_env(_PROBE_URL, dict(env))
        if before is not None and _config_stamp(paths) == before:
            memo[key] = (before, dict(child_env))
        return child_env


def _effective(env: Mapping[str, str]) -> tuple:
    """``(trust values, child env, insteadOf prefixes, entries)`` for git's fetch under *env*.

    Classified on every call (W3d R1): the user's config is re-read through
    ``git_network_env`` each time; no verdict is kept.
    """
    from ..utils.git_env import _read_effective_git_config

    try:
        child_env = _network_env(env)
        entries = _frozen_entries(child_env)
        if entries is None:
            entries = _read_effective_git_config(child_env).entries
    except Exception as exc:
        raise _Refuse(f"git config unreadable: {type(exc).__name__}") from None
    values, prefixes = _classify(entries)
    return values, child_env, prefixes, entries


def _trust(child_env: Mapping[str, str], values: Mapping[str, str]) -> tuple[str, str]:
    """W3: ``("file", CA path)`` for git's OpenSSL trust, ``("os", "")`` for schannel's."""
    from ..deps import anonymous_ref_listing as anon

    backend = values.get("http.sslbackend", "").lower()
    if backend == "schannel" and os.name == "nt":
        # schannel ignores http.sslCAInfo unless schannelUseSSLCAInfo (an http.*
        # key, which forces git); a CA env var would make the choice ambiguous.
        if child_env.get("GIT_SSL_CAINFO"):
            raise _Refuse("GIT_SSL_CAINFO with schannel")
        return "os", ""
    if backend not in ("", "openssl"):
        raise _Refuse(f"git config http.sslBackend={backend}")
    if os.name == "nt" and not (
        child_env.get("GIT_SSL_CAINFO")
        or values.get("http.sslcainfo")
        or child_env.get("SSL_CERT_FILE")
    ):
        # W3d R5: Git for Windows' curl bundle is not certifi; no named CA file, no guess.
        raise _Refuse("no CA file named for git's OpenSSL backend on Windows")
    try:
        return "file", anon._verify(child_env, values)
    except anon._Fallback as reason:
        raise _Refuse(str(reason)) from None


def _gate(url: str, env: Mapping[str, str]) -> tuple[str, str | None, tuple[str, str]]:
    """``(upload-pack URL, proxy, trust)`` when git's request is provably anonymous and equal."""
    from ..deps import anonymous_ref_listing as anon

    if any(
        (source.get("APM_NO_INPROCESS_FETCH") or "").lower() in _TRUE
        for source in (env, os.environ)
    ):
        raise _Refuse("APM_NO_INPROCESS_FETCH is set")
    try:
        path = anon._repository_path(url)
    except anon._Fallback as reason:
        raise _Refuse(str(reason)) from None
    for name in anon._FALLBACK_ENV:
        if env.get(name):
            raise _Refuse(f"{name} is set")
    values, child_env, prefixes, entries = _effective(env)
    if any(url.lower().startswith(prefix.lower()) for prefix in prefixes):
        raise _Refuse("git config url.*.insteadOf rewrites this URL")
    allow = child_env.get("GIT_ALLOW_PROTOCOL")
    if allow is not None and "https" not in allow.split(":"):
        raise _Refuse("https not allowed")
    for name in anon._FALLBACK_ENV:
        if child_env.get(name):
            raise _Refuse(f"{name} is set for git")
    try:
        proxy = anon._proxy(child_env)
    except anon._Fallback as reason:
        raise _Refuse(str(reason)) from None
    trust = _trust(child_env, values)
    if url != f"https://github.com{path}":
        raise _Refuse("url is not the validated github.com form")
    _require_inert_attributes(child_env, entries)
    return f"https://github.com{path}/git-upload-pack", proxy, trust


def _inert_attributes(path: str) -> bool:
    """W4: whether an attributes file holds only tokens that never change checkout bytes.

    Pattern-agnostic: any ``text``/``eol``/``crlf``/``filter``/``ident``/
    ``working-tree-encoding`` token, any ``[attr]`` macro, a quoted pattern or
    any token outside the allowlist refuses, whatever path it names.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeError):
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("[attr]", '"')):
            return False
        for token in line.split()[1:]:
            name = token.lstrip("-!").split("=", 1)[0].lower()
            if name not in _INERT_ATTRIBUTES and not name.startswith("linguist-"):
                return False
    return True


def _attribute_files(child_env: Mapping[str, str], entries) -> list[tuple[str, str]]:
    """``[(source, path)]`` of every attributes file outside the commit git would read.

    APM's git checkout always runs with the line-ending and symlink pins of
    ``utils/git_env`` (``_safe_git_args``), which outrank system and global
    config, so git writes raw blob bytes unless an attribute says
    ``text``/``eol``/``filter``. The commit's own ``.gitattributes`` refuses
    in the plan and a fresh staged clone has no ``info/attributes``; what is
    left is the global attributes file (``core.attributesFile`` or its XDG
    default) and the system one.
    """
    files: list[tuple[str, str]] = []
    configured = None
    for entry in entries:
        if entry.key.lower() == "core.attributesfile":
            configured = entry.value
    if configured:
        files.append(("core.attributesFile", os.path.expanduser(configured)))
    else:
        xdg = child_env.get("XDG_CONFIG_HOME")
        home = child_env.get("HOME") or child_env.get("USERPROFILE")
        base = xdg if xdg else (os.path.join(home, ".config") if home else None)
        if base:
            files.append(("global attributes", os.path.join(base, "git", "attributes")))
    if (child_env.get("GIT_ATTR_NOSYSTEM") or "").lower() not in _TRUE:
        files.append(("system attributes", _system_attributes_path(child_env)))
    return files


_system_paths: dict[tuple, str] = {}
_system_paths_lock = threading.Lock()


def _system_attributes_path(child_env: Mapping[str, str]) -> str:
    """Where this git build reads system attributes (W3d G5: the path string only).

    Kept per (git binary identity, attribute env); a failed probe is never
    kept; the file's content is checked by the caller on every call.
    """
    from ..utils.git_env import get_git_executable

    git = get_git_executable()
    key = (
        _binary_identity(git),
        child_env.get("GIT_ATTR_NOSYSTEM"),
        child_env.get("PREFIX"),
    )
    known = _system_paths.get(key)
    if known is None:
        with _system_paths_lock:
            known = _system_paths.get(key)
            if known is None:
                known = _probe_system_attributes(git, child_env)
                if known and key[0] is not None:
                    _system_paths[key] = known
    if not known:
        raise _Refuse("system attributes path unknown (git var GIT_ATTR_SYSTEM)")
    if os.name == "nt" and known.startswith("/"):
        raise _Refuse(f"system attributes path not a Windows path: {known}")
    return known


def _probe_system_attributes(git: str, child_env: Mapping[str, str]) -> str:
    """``git var GIT_ATTR_SYSTEM``, or ``""`` when this git cannot name it (kept: no re-probe)."""
    try:
        probe = subprocess.run(
            [git, "var", "GIT_ATTR_SYSTEM"],
            env=dict(child_env),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _require_inert_attributes(child_env: Mapping[str, str], entries) -> None:
    """Every attributes file outside the commit is re-read and checked on every call (G5)."""
    for source, path in _attribute_files(child_env, entries):
        if os.path.lexists(path) and not _inert_attributes(path):
            raise _Refuse(f"{source} {path} has a checkout-affecting attribute")


_BREAKER_MEMO = "inprocess-fetch-rate-limited"


def _rate_limited() -> bool:
    """Whether github.com rate-limited an in-process fetch earlier in this command."""
    from ..core.invocation_memo import invocation_memo

    memo = invocation_memo(_BREAKER_MEMO)
    return bool(memo)


def _admit(status: int, headers: Mapping[str, str]) -> None:
    """Refuse a non-200 answer; a rate-limit answer also sends the rest of the command to git.

    A throttle (``deps/github_rate_limit``) or any 403 trips the breaker: no
    retry, no sleep, and no further in-process request in this command.
    """
    from ..core.invocation_memo import invocation_memo
    from ..deps.github_rate_limit import classify_github_throttle

    if status == 403 or classify_github_throttle(status, headers) is not None:
        memo = invocation_memo(_BREAKER_MEMO)
        if memo is not None:
            memo["tripped"] = True
        raise _Refuse(f"rate limited (HTTP {status}); git serves the rest of this command")
    if headers.get("WWW-Authenticate") is not None:
        # W1: an auth challenge is git's to answer; nothing is retried in process.
        raise _Refuse("auth challenge")
    if status != 200:
        raise _Refuse(f"HTTP {status}")


_os_session = None
_os_session_lock = threading.Lock()


def _session(trust: tuple[str, str]):
    """ONE pooled keep-alive session (and SSL context) per trust source per process."""
    global _os_session
    if trust[0] == "file":
        from ..deps.anonymous_ref_listing import _session_for_refs

        return _session_for_refs(trust[1])
    with _os_session_lock:
        if _os_session is None:
            import http.cookiejar
            import ssl

            import truststore
            from requests.adapters import HTTPAdapter

            from ..utils.http_credentials import CredentialScopedSession
            from ..version import get_version

            class _OsTrustContext(truststore.SSLContext):
                """The OS store only (schannel's trust): requests' CA bundle is not added."""

                def load_verify_locations(self, *args, **kwargs) -> None:
                    return None

            context = _OsTrustContext(ssl.PROTOCOL_TLS_CLIENT)

            class _OsTrustAdapter(HTTPAdapter):
                def init_poolmanager(self, *args, **kwargs):
                    kwargs["ssl_context"] = context
                    return super().init_poolmanager(*args, **kwargs)

                def proxy_manager_for(self, proxy, **proxy_kwargs):
                    proxy_kwargs["ssl_context"] = context
                    return super().proxy_manager_for(proxy, **proxy_kwargs)

            session = CredentialScopedSession(ambient_credentials=False)
            session.trust_env = False
            session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            session.headers.clear()
            session.headers.update(
                {
                    "User-Agent": f"apm/{get_version()}",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Pragma": "no-cache",
                }
            )
            session.mount("https://", _OsTrustAdapter(pool_connections=4, pool_maxsize=16))
            _os_session = session
        return _os_session


def _post(upload_url: str, proxy: str | None, trust: tuple[str, str], body: bytes) -> bytes:
    from ..utils.http_credentials import origin_bounded_request

    deadline = time.monotonic() + _DEADLINE_SECONDS
    with origin_bounded_request(
        _session(trust),
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
        verify=trust[1] if trust[0] == "file" else True,
        timeout=_TIMEOUT,
        stream=True,
    ) as response:
        _admit(response.status_code, response.headers)
        kind = response.headers.get("Content-Type", "").split(";")[0].strip()
        if kind != "application/x-git-upload-pack-result":
            raise _Refuse("not an upload-pack result")
        if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            raise _Refuse("encoded response")
        read1 = getattr(response.raw, "read1", None)
        if not callable(read1):
            raise _Refuse("no single-read body access")
        # One socket read per step, so a trickling server costs at most the
        # deadline plus one read timeout.
        chunks = []
        total = 0
        while True:
            if time.monotonic() > deadline:
                raise _Refuse("fetch exceeds the wall deadline")
            chunk = read1(65536, decode_content=False)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > _MAX_BODY:
                raise _Refuse("pack exceeds the size cap")
            chunks.append(chunk)


_SETTLED_MEMO = "inprocess-fetch-settled"
_first_fetch_lock = threading.Lock()


def _admitted_post(
    upload_url: str,
    proxy: str | None,
    trust: tuple[str, str],
    body: bytes,
    *,
    post: Callable[..., bytes] | None = None,
) -> bytes:
    """``_post`` behind the breaker, with the command's first answer awaited alone.

    Until one in-process fetch of this command has been answered 200, fetches
    wait their turn and re-check the breaker, so a throttle on the first answer
    sends every other download to git without a second request. The lock is
    held only for that first answer: once it is 200, queued fetches leave the
    lock and run concurrently, each re-checking the breaker just before its
    request (a later throttle still stops every fetch not yet sent).
    """
    from ..core.invocation_memo import invocation_memo

    send = post or _post
    settled = invocation_memo(_SETTLED_MEMO)
    if settled is not None and not settled.get("ok"):
        with _first_fetch_lock:
            if _rate_limited():
                raise _Refuse("rate limited earlier in this command")
            if not settled.get("ok"):
                answer = send(upload_url, proxy, trust, body)
                settled["ok"] = True
                return answer
    if _rate_limited():  # another download tripped it while this one was gated or queued
        raise _Refuse("rate limited earlier in this command")
    return send(upload_url, proxy, trust, body)


def _note(url: str, outcome: str) -> None:
    """Debug-log each hit and fallback with the forcing key; never a credential (W3d R3)."""
    _log.debug("in-process fetch %s: %s", _redact(url), _redact(outcome))


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
        upload_url, proxy, trust = _gate(url, env if env is not None else os.environ)
        body = _admitted_post(upload_url, proxy, trust, _request_body(sha))
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
        _note(url, "hit")
        return manifest
    except _Refuse as reason:
        _note(url, f"fallback: {reason}")
        return None
    except Exception as exc:
        _note(url, f"fallback: {type(exc).__name__}: {exc}")
        return None


def _redact(value: object) -> str:
    from ..utils.git_env import redact_git_diagnostic

    return redact_git_diagnostic(str(value))
