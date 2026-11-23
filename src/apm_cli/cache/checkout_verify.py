"""Verify a git checkout against its commit without trusting any stored object.

Git does not re-hash loose objects when it reads them, so a checkout (or the
object store behind it) can hold bytes that no longer match the commit it
claims. Before APM hashes or deploys a package from a checkout, this owner
proves the chain from the commit id down to every byte on disk:

1. the commit object re-hashes to the commit id;
2. every tree reachable from the commit's root tree re-hashes to its id;
3. every working-tree file holds exactly the bytes its blob id names (a
   file whose bytes differ is accepted only when Git's own checkout filters
   turn the verified blob into exactly those bytes);
4. a tracked link holds its resolved target's verified bytes (or its own
   link text when the target cannot be resolved), as
   :mod:`apm_cli.utils.git_sparse` materializes it;
5. the working tree holds no file outside ``.git`` that the commit does not
   produce.

A verdict is remembered only in this process, per commit and checkout path:
nothing persistent stands in for the proof.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
from collections.abc import Iterable
from pathlib import Path

from ..utils.git_sparse import _LINK_MODE, _TREE_MODE, _LinkResolver, _TreeEntry, _within

_VERIFIED: set[tuple[str, str, tuple[str, ...] | None]] = set()
_VERIFIED_LOCK = threading.Lock()
_MODES = {"40000": _TREE_MODE, "160000": "160000"}


class CheckoutVerificationError(RuntimeError):
    """A checkout's bytes do not match the commit it claims."""


def _algorithm(sha: str) -> str:
    if len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("not a full object id")
    return "sha256" if len(sha) == 64 else "sha1"


def _object_id(algorithm: str, kind: str, data: bytes) -> str:
    return hashlib.new(algorithm, f"{kind} {len(data)}\0".encode() + data).hexdigest()


class _VerifiedObjects:
    """Read objects through one ``git cat-file --batch``; every object is re-hashed."""

    def __init__(self, git_exe: str, repo: Path, env: dict[str, str] | None, algorithm: str):
        self._algorithm = algorithm
        self._process = subprocess.Popen(
            [git_exe, "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )

    def read(self, oid: str, kind: str) -> bytes:
        process = self._process
        if process.stdin is None or process.stdout is None:
            raise CheckoutVerificationError("git cat-file is not available")
        process.stdin.write(oid.encode("ascii") + b"\n")
        process.stdin.flush()
        header = process.stdout.readline().split()
        if len(header) != 3 or header[0].decode("ascii", "replace") != oid:
            raise CheckoutVerificationError(f"object {oid[:12]} is missing")
        if header[1].decode("ascii", "replace") != kind:
            raise CheckoutVerificationError(f"object {oid[:12]} is not a {kind}")
        data = process.stdout.read(int(header[2]))
        process.stdout.read(1)
        if _object_id(self._algorithm, kind, data) != oid:
            raise CheckoutVerificationError(f"{kind} {oid[:12]} does not hash to its id")
        return data

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait()


def _tree_entries(objects: _VerifiedObjects, root: str, width: int) -> dict[str, _TreeEntry]:
    entries: dict[str, _TreeEntry] = {}
    pending = [("", root)]
    while pending:
        prefix, oid = pending.pop()
        data = objects.read(oid, "tree")
        position = 0
        while position < len(data):
            space = data.index(b" ", position)
            nul = data.index(b"\0", space)
            mode = data[position:space].decode("ascii")
            name = os.fsdecode(data[space + 1 : nul])
            child = data[nul + 1 : nul + 1 + width].hex()
            position = nul + 1 + width
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise CheckoutVerificationError(f"tree {oid[:12]} names an unsafe entry")
            path = f"{prefix}{name}"
            mode = _MODES.get(mode, mode.zfill(6))
            kind = "tree" if mode == _TREE_MODE else "commit" if mode == "160000" else "blob"
            entries[path] = _TreeEntry(mode, kind, child)
            if kind == "tree":
                pending.append((f"{path}/", child))
    return entries


def _in_scope(path: str, paths: tuple[str, ...] | None) -> bool:
    return paths is None or any(path == p or _within(path, p) for p in paths)


def _expected_files(
    entries: dict[str, _TreeEntry],
    objects: _VerifiedObjects,
    paths: tuple[str, ...] | None,
) -> dict[str, tuple[str, str]]:
    """``{worktree path: (blob id, attribute path)}`` the checkout must hold."""

    def load(oids: list[str]) -> dict[str, bytes]:
        return {oid: objects.read(oid, "blob") for oid in oids}

    resolver = _LinkResolver(entries, load)
    files: dict[str, tuple[str, str]] = {}
    for path, entry in entries.items():
        if entry.kind != "blob" or not _in_scope(path, paths):
            continue
        if entry.mode != _LINK_MODE:
            files[path] = (entry.oid, path)
            continue
        try:
            writes = resolver.plan(path)
        except (RuntimeError, ValueError):  # PathTraversalError is a ValueError
            files[path] = (entry.oid, path)  # an unresolvable link stays as its own text
            continue
        for write in writes:
            if write.source is not None:
                files[write.dest] = (entries[write.source].oid, write.source)
    return files


def _filtered_bytes(git_exe: str, repo: Path, env, path: str, oid: str) -> bytes | None:
    """The bytes Git's checkout filters write for blob *oid* at *path* (pinned line endings)."""
    from ..utils.git_env import git_line_ending_pin_args

    pins = list(git_line_ending_pin_args())
    result = subprocess.run(
        [git_exe, *pins, "-C", str(repo), "cat-file", "--filters", f"--path={path}", oid],
        capture_output=True,
        env=env,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _worktree_files(repo: Path) -> Iterable[tuple[str, Path]]:
    for directory, subdirs, names in os.walk(repo):
        here = Path(directory)
        if here == repo:
            subdirs[:] = [d for d in subdirs if d != ".git"]
            names = [n for n in names if n != ".git"]
        for name in names:
            path = here / name
            yield path.relative_to(repo).as_posix(), path


def _verify(
    repo: Path,
    sha: str,
    paths: tuple[str, ...] | None,
    git_exe: str,
    env: dict[str, str] | None,
) -> None:
    algorithm = _algorithm(sha)
    objects = _VerifiedObjects(git_exe, repo, env, algorithm)
    try:
        commit = objects.read(sha, "commit")
        first = commit.split(b"\n", 1)[0]
        if not first.startswith(b"tree "):
            raise CheckoutVerificationError(f"commit {sha[:12]} names no tree")
        root = first[5:].decode("ascii")
        width = 32 if algorithm == "sha256" else 20
        entries = _tree_entries(objects, root, width)
        expected = _expected_files(entries, objects, paths)
        seen: set[str] = set()
        for rel, path in _worktree_files(repo):
            want = expected.get(rel)
            if want is None:
                if paths is None or _in_scope(rel, paths):
                    raise CheckoutVerificationError(f"{rel} is not produced by the commit")
                continue
            oid, attribute_path = want
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise CheckoutVerificationError(f"{rel} is not a regular file")
            data = path.read_bytes()
            if _object_id(algorithm, "blob", data) != oid:
                objects.read(oid, "blob")  # the blob itself must still be genuine
                if _filtered_bytes(git_exe, repo, env, attribute_path, oid) != data:
                    raise CheckoutVerificationError(f"{rel} does not hold its committed bytes")
            seen.add(rel)
        missing = sorted(set(expected) - seen)
        if missing:
            raise CheckoutVerificationError(f"{missing[0]} is missing from the checkout")
    finally:
        objects.close()


def verify_checkout(
    repo: Path,
    sha: str,
    *,
    paths: Iterable[str] | None = None,
    git_exe: str | None = None,
    env: dict[str, str] | None = None,
) -> bool:
    """Whether *repo*'s working tree is exactly what commit *sha* produces.

    *paths* limits the check to a sparse checkout's directories; files
    outside them are neither expected nor allowed to be read. The positive
    verdict is remembered for this process only.
    """
    scope = tuple(sorted(p.strip("/") for p in paths)) if paths else None
    key = (sha.lower(), str(Path(repo).resolve()), scope)
    with _VERIFIED_LOCK:
        if key in _VERIFIED:
            return True
    if git_exe is None:
        from ..utils.git_env import get_git_executable

        git_exe = get_git_executable()
    try:
        _verify(Path(repo), sha.lower(), scope, git_exe, env)
    except (CheckoutVerificationError, OSError, ValueError, subprocess.SubprocessError):
        return False
    with _VERIFIED_LOCK:
        _VERIFIED.add(key)
    return True


def require_verified_checkout(
    repo: Path,
    sha: str | None = None,
    *,
    paths: Iterable[str] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Raise unless *repo* is exactly commit *sha*.

    An unknown or abbreviated *sha* stands for the clone's HEAD commit when
    HEAD extends it.
    """
    if not sha or len(sha) not in (40, 64):
        from .integrity import _read_head_sha

        head = _read_head_sha(Path(repo))
        if head and head.startswith((sha or "").lower()):
            sha = head
    if not verify_checkout(Path(repo), sha or "", paths=paths, env=env):
        raise CheckoutVerificationError(
            f"checkout {Path(repo).name} does not match its commit {(sha or '?')[:12]}"
        )


def forget_verdicts() -> None:
    """Drop every remembered verdict (an evicted or rebuilt checkout)."""
    with _VERIFIED_LOCK:
        _VERIFIED.clear()
