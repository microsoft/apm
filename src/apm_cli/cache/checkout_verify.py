"""Verify a git checkout against its commit without trusting any stored object.

Git does not re-hash loose objects when it reads them, so a checkout (or the
object store behind it) can hold bytes that no longer match the commit it
claims. Before APM hashes or deploys a package from a checkout, this owner
proves the chain from the commit id down to every byte it hands out:

1. the commit object re-hashes to the commit id;
2. every tree it reads re-hashes to its id;
3. every working-tree file holds exactly its blob's bytes, or, where the
   commit's own ``.gitattributes`` asks for ``eol=crlf``, those bytes with
   line feeds written as CRLF. Line endings are worked out here, from the
   verified attributes; no Git filter or driver ever runs. Paths with a
   ``filter``, ``ident`` or ``working-tree-encoding`` attribute must hold
   their blob's exact bytes;
4. a tracked link holds its resolved target's bytes (or its own link text
   when the target cannot be resolved), as :mod:`apm_cli.utils.git_sparse`
   materializes it;
5. each file's executable bit matches its tree mode (POSIX hosts);
6. nothing else is present: no untracked file, and in a sparse cone no file
   the cone would not check out.

Verification records, in this process only, the digest of every file it
accepted. Consumers then check the bytes they actually copied or read
against that record (:func:`require_verified_copy`, :func:`read_verified_file`),
so a checkout that changes after verification is never trusted.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..utils.git_env import git_subprocess_env
from ..utils.git_object_id import git_object_id, object_format
from ..utils.git_sparse import _LINK_MODE, _TREE_MODE, _LinkResolver, _TreeEntry
from ..utils.paths import relative_to
from .cached_repo import cached_repo_env, inert_git_args

_MODES = {"40000": _TREE_MODE, "160000": "160000"}
_EXECUTABLE = "100755"
_LONE_LF = re.compile(rb"(?<!\r)\n")


class CheckoutVerificationError(RuntimeError):
    """A checkout's bytes do not match the commit it claims."""


@dataclass(frozen=True)
class _Expect:
    oid: str
    attribute_path: str
    executable: bool


@dataclass(frozen=True)
class _Manifest:
    """What verification accepted: ``{path: (blob id of the bytes on disk, executable)}``."""

    algorithm: str
    files: dict[str, tuple[str, bool]]


_MANIFESTS: dict[tuple[str, str], _Manifest] = {}
_MANIFEST_LOCK = threading.Lock()


class _Objects:
    """Read objects through one inert ``git cat-file --batch``; every object is re-hashed."""

    def __init__(self, git_exe: str, repo: Path, env: dict[str, str] | None, algorithm: str):
        self._algorithm = algorithm
        self._process = subprocess.Popen(
            [git_exe, *inert_git_args(), "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=cached_repo_env(git_subprocess_env(env), repo, offline=True),
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
        if git_object_id(self._algorithm, kind, data) != oid:
            raise CheckoutVerificationError(f"{kind} {oid[:12]} does not hash to its id")
        return data

    def close(self) -> None:
        """Stop the reader even if it is blocked writing an object nobody read."""
        process = self._process
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


class _Tree:
    """Verified tree listings of one commit, read only when needed."""

    def __init__(self, objects: _Objects, root: str, width: int) -> None:
        self._objects = objects
        self._width = width
        self._root = root
        self._listings: dict[str, dict[str, _TreeEntry] | None] = {}

    def _parse(self, oid: str) -> dict[str, _TreeEntry]:
        data = self._objects.read(oid, "tree")
        listing: dict[str, _TreeEntry] = {}
        position = 0
        while position < len(data):
            space = data.index(b" ", position)
            nul = data.index(b"\0", space)
            mode = data[position:space].decode("ascii")
            name = os.fsdecode(data[space + 1 : nul])
            child = data[nul + 1 : nul + 1 + self._width].hex()
            position = nul + 1 + self._width
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise CheckoutVerificationError(f"tree {oid[:12]} names an unsafe entry")
            mode = _MODES.get(mode, mode.zfill(6))
            kind = "tree" if mode == _TREE_MODE else "commit" if mode == "160000" else "blob"
            listing[name] = _TreeEntry(mode, kind, child)
        return listing

    def listing(self, directory: str) -> dict[str, _TreeEntry] | None:
        """The entries of *directory*, or None when the commit has no such tree."""
        if directory not in self._listings:
            if not directory:
                oid: str | None = self._root
            else:
                parent, _, name = directory.rpartition("/")
                above = self.listing(parent)
                entry = above.get(name) if above is not None else None
                oid = entry.oid if entry is not None and entry.kind == "tree" else None
            self._listings[directory] = None if oid is None else self._parse(oid)
        return self._listings[directory]

    def entry(self, path: str) -> _TreeEntry | None:
        parent, _, name = path.rpartition("/")
        listing = self.listing(parent)
        return None if listing is None else listing.get(name)

    def walk(self, directory: str) -> dict[str, _TreeEntry]:
        """Every entry under *directory* (trees included), keyed by full path."""
        found: dict[str, _TreeEntry] = {}
        pending = [directory]
        while pending:
            current = pending.pop()
            for name, entry in (self.listing(current) or {}).items():
                path = _join(current, name)
                found[path] = entry
                if entry.kind == "tree":
                    pending.append(path)
        return found


# -- .gitattributes, evaluated in-process ----------------------------------

_BUILTIN_MACROS = {"binary": (("diff", False), ("merge", False), ("text", False))}
_TRANSFORMS = ("filter", "ident", "working-tree-encoding")


def _glob(pattern: str) -> re.Pattern[str]:
    """Translate a gitattributes glob (``FNM_PATHNAME``, ``**``) to a regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i) and (i == 0 or pattern[i - 1] == "/"):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("/.*")
            i += 3
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 2 :]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1 : end]
            if body[:1] in {"!", "^"}:
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        elif pattern[i] == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _attribute(token: str) -> tuple[str, object]:
    if token.startswith("-"):
        return token[1:], False
    if token.startswith("!"):
        return token[1:], None
    name, sep, value = token.partition("=")
    return name, (value if sep else True)


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(line: str) -> tuple[str, str] | None:
    """Git's C-quoted pattern opening *line* and the text after it, or None if malformed."""
    raw = line.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 1
    while i < len(raw):
        char = raw[i : i + 1]
        if char == b'"':
            rest = raw[i + 1 :].decode("utf-8", "replace")
            if rest and not rest[0].isspace():
                return None
            return out.decode("utf-8", "replace"), rest
        if char != b"\\":
            out += char
            i += 1
            continue
        escape = raw[i + 1 : i + 2].decode("ascii", "replace")
        octal = raw[i + 1 : i + 4]
        if escape in _C_ESCAPES:
            out.append(_C_ESCAPES[escape])
            i += 2
        elif len(octal) == 3 and all(c in b"01234567" for c in octal) and int(octal, 8) < 256:
            out.append(int(octal, 8))
            i += 4
        else:
            return None
    return None


# Names some file systems resolve to ".git": any case, trailing dots or spaces, 8.3 short names.
_GIT_DIR_ALIAS = re.compile(r"(?:\.git|git~[0-9]+)[. ]*", re.IGNORECASE | re.ASCII)


# Codepoints HFS+ ignores when comparing names (git's is_hfs_dotgit).
_HFS_IGNORABLE = dict.fromkeys(
    [0x200C, 0x200D, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
)


def _names_git_dir(name: str) -> bool:
    """Whether *name* is ``.git`` on any host: NTFS short names and trailing dots/spaces, HFS+ ignorables."""
    if _GIT_DIR_ALIAS.fullmatch(name) is not None:
        return True
    if name.isascii():  # every HFS+ ignorable is non-ASCII
        return False
    folded = name.translate(_HFS_IGNORABLE)
    return folded != name and _GIT_DIR_ALIAS.fullmatch(folded) is not None


class _Attributes:
    """Attributes from the commit's own ``.gitattributes`` files only."""

    def __init__(self, tree: _Tree, objects: _Objects) -> None:
        self._tree = tree
        self._objects = objects
        self._rules: dict[str, list[tuple[bool, re.Pattern[str], list[tuple[str, object]]]]] = {}
        self._unknown: set[str] = set()
        self._macros: dict[str, tuple[tuple[str, object], ...]] = dict(_BUILTIN_MACROS)
        self._load("")

    def _load(self, directory: str) -> None:
        if directory in self._rules or directory in self._unknown:
            return
        rules: list[tuple[bool, re.Pattern[str], list[tuple[str, object]]]] = []
        entry = self._tree.entry(_join(directory, ".gitattributes"))
        if entry is not None and entry.kind == "blob" and entry.mode != _LINK_MODE:
            text = self._objects.read(entry.oid, "blob").decode("utf-8", "replace")
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith('"'):
                    quoted = _unquote(line)
                    if quoted is None:
                        self._unknown.add(directory)
                        return
                    pattern, tokens = quoted[0], quoted[1].split()
                else:
                    pattern, *tokens = line.split()
                if pattern.startswith("[attr]"):
                    if not directory:
                        self._macros[pattern[6:]] = tuple(_attribute(t) for t in tokens)
                    continue
                if pattern.startswith("!") or pattern.endswith("/"):
                    continue
                anchored = "/" in pattern
                rules.append(
                    (anchored, _glob(pattern.lstrip("/")), [_attribute(t) for t in tokens])
                )
        self._rules[directory] = rules

    def _fill(self, state: dict[str, object], assigned: list[tuple[str, object]]) -> None:
        """Resolve as Git's attr.c does: highest priority first, each name set once.

        A macro expands only when its own final state is set, so a later ``-macro``
        cancels it, and every macro expands at most once per path.
        """
        pending = [reversed(assigned)]
        while pending:
            step = next(pending[-1], None)
            if step is None:
                pending.pop()
                continue
            name, value = step
            if name in state:
                continue
            state[name] = value
            if value is True and name in self._macros:
                pending.append(reversed(self._macros[name]))

    def state(self, path: str) -> dict[str, object] | None:
        """The attributes of *path*, or None when a ``.gitattributes`` cannot be read here."""
        parts = path.split("/")
        assigned: list[tuple[str, object]] = []
        for depth in range(len(parts)):
            directory = "/".join(parts[:depth])
            self._load(directory)
            if directory in self._unknown:
                return None
            relative = "/".join(parts[depth:])
            for anchored, regex, attributes in self._rules[directory]:
                if regex.fullmatch(relative if anchored else parts[-1]):
                    assigned.extend(attributes)
        state: dict[str, object] = {}
        self._fill(state, assigned)
        return state


class _FoldedAttributes(_Attributes):
    """``core.ignorecase`` matching: every pattern compiled case-insensitively (W3d U3)."""

    def _load(self, directory: str) -> None:
        fresh = directory not in self._rules
        super()._load(directory)
        rules = self._rules.get(directory)
        if fresh and rules is not None:
            self._rules[directory] = [
                (anchored, re.compile(regex.pattern, re.IGNORECASE), attrs)
                for anchored, regex, attrs in rules
            ]


def _accepted(attributes: _Attributes, attribute_path: str, blob: bytes) -> tuple[bytes, ...]:
    """The byte strings a pinned checkout may write for *blob* at *attribute_path*."""
    state = attributes.state(attribute_path)
    if state is None or any(state.get(name) not in (None, False) for name in _TRANSFORMS):
        return (blob,)
    text = state.get("text", state.get("crlf"))
    eol = state.get("eol", "lf" if state.get("crlf") == "input" else None)
    if text is False or eol != "crlf":
        return (blob,)
    return (blob, _LONE_LF.sub(b"\r\n", blob))


# -- expected files ------------------------------------------------------------


def _scope_dirs(scope: tuple[str, ...]) -> set[str]:
    """The root and every ancestor directory of the scope (cone-level files live here)."""
    dirs = {""}
    for path in scope:
        parts = path.split("/")
        dirs.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
    return dirs


def _expected(
    tree: _Tree, objects: _Objects, scope: tuple[str, ...] | None, cone: bool
) -> dict[str, _Expect]:
    """``{worktree path: expectation}`` for every file the checkout must hold."""
    entries: dict[str, _TreeEntry] = {}
    for directory in scope if scope is not None else ("",):
        entries.update(tree.walk(directory))
        top = tree.entry(directory) if directory else None
        if top is not None and top.mode == _LINK_MODE:  # expanded in place
            entries[directory] = top
    expected: dict[str, _Expect] = {}
    if scope is not None and cone:
        for directory in _scope_dirs(scope):
            for name, entry in (tree.listing(directory) or {}).items():
                if entry.kind == "blob" and _join(directory, name) not in entries:
                    path = _join(directory, name)
                    expected[path] = _Expect(entry.oid, path, entry.mode == _EXECUTABLE)
    links = [path for path, entry in entries.items() if entry.mode == _LINK_MODE]
    full = tree.walk("") if links and scope is not None else entries
    resolver = _LinkResolver(full, lambda oids: {oid: objects.read(oid, "blob") for oid in oids})
    for path, entry in entries.items():
        if entry.kind != "blob":
            continue
        if entry.mode != _LINK_MODE:
            expected[path] = _Expect(entry.oid, path, entry.mode == _EXECUTABLE)
            continue
        try:
            writes = resolver.plan(path)
        except (RuntimeError, ValueError):  # PathTraversalError is a ValueError
            expected[path] = _Expect(entry.oid, path, False)  # stays as its own text
            continue
        for write in writes:
            if write.source is not None:
                source = full[write.source]
                expected[write.dest] = _Expect(source.oid, write.source, write.mode == _EXECUTABLE)
    return expected


def _worktree_files(repo: Path) -> Iterator[tuple[str, Path]]:
    """Every entry to check: files, and directory links (never followed; they never verify)."""
    for directory, subdirs, names in os.walk(repo):
        here = Path(directory)
        if here == repo:
            subdirs[:] = [d for d in subdirs if d != ".git"]
            names = [n for n in names if n != ".git"]
        links = [d for d in subdirs if (here / d).is_symlink()]
        subdirs[:] = [d for d in subdirs if d not in links]
        for name in (*names, *links):
            path = here / name
            yield relative_to(path, repo).as_posix(), path


def _in_scope(path: str, scope: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(f"{p}/") for p in scope)


def _check_mode(rel: str, info: os.stat_result, executable: bool) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise CheckoutVerificationError(f"{rel} is not a regular file")
    if os.name != "nt" and bool(info.st_mode & stat.S_IXUSR) != executable:
        raise CheckoutVerificationError(f"{rel} does not have its committed file mode")


@dataclass
class _Run:
    algorithm: str
    objects: _Objects
    tree: _Tree
    attributes: _Attributes


def _accept_file(run: _Run, rel: str, path: Path, want: _Expect) -> str:
    """Return the blob id of the accepted on-disk bytes, or raise."""
    _check_mode(rel, os.lstat(path), want.executable)
    data = path.read_bytes()
    observed = git_object_id(run.algorithm, "blob", data)
    if observed == want.oid:
        return observed
    blob = run.objects.read(want.oid, "blob")
    if data not in _accepted(run.attributes, want.attribute_path, blob):
        raise CheckoutVerificationError(f"{rel} does not hold its committed bytes")
    return observed


def _lookup(run: _Run, rel: str, scope: tuple[str, ...] | None, cone: bool) -> _Expect:
    """The expectation for a file outside the required set (non-cone scopes only)."""
    if scope is None or cone or _in_scope(rel, scope):
        raise CheckoutVerificationError(f"{rel} is not produced by the commit")
    entry = run.tree.entry(rel)
    if entry is None or entry.kind != "blob":
        raise CheckoutVerificationError(f"{rel} is not produced by the commit")
    return _Expect(entry.oid, rel, entry.mode == _EXECUTABLE)


def _verify(
    repo: Path,
    sha: str,
    scope: tuple[str, ...] | None,
    cone: bool,
    git_exe: str,
    env: dict[str, str] | None,
) -> _Manifest:
    algorithm = object_format(sha)
    from .pack_objects import PackObjects

    pack = PackObjects(repo, algorithm, env)
    try:
        return _verify_with(repo, sha, scope, cone, algorithm, _PackSource(pack, algorithm))
    except _PackMiss:
        pass  # all-or-nothing per package (W3d 2a): git answers the whole package
    finally:
        pack.close()
    objects = _Objects(git_exe, repo, env, algorithm)
    try:
        return _verify_with(repo, sha, scope, cone, algorithm, objects)
    finally:
        objects.close()


class _PackMiss(Exception):
    """The pack could not serve an object: verify the package through git instead."""


class _PackSource:
    def __init__(self, pack, algorithm: str) -> None:
        self._pack = pack
        self._algorithm = algorithm

    def read(self, oid: str, kind: str) -> bytes:
        found = self._pack.read(oid)
        if found is None:
            raise _PackMiss(oid)
        if found[0] != kind:
            raise CheckoutVerificationError(f"object {oid[:12]} is not a {kind}")
        if git_object_id(self._algorithm, kind, found[1]) != oid:
            raise CheckoutVerificationError(f"{kind} {oid[:12]} does not hash to its id")
        return found[1]

    def close(self) -> None:
        pass


def _verify_with(repo, sha, scope, cone, algorithm, objects) -> _Manifest:
    commit = objects.read(sha, "commit")
    first = commit.split(b"\n", 1)[0]
    if not first.startswith(b"tree "):
        raise CheckoutVerificationError(f"commit {sha[:12]} names no tree")
    tree = _Tree(objects, first[5:].decode("ascii"), 32 if algorithm == "sha256" else 20)
    run = _Run(algorithm, objects, tree, _Attributes(tree, objects))
    expected = _expected(tree, objects, scope, cone)
    accepted: dict[str, tuple[str, bool]] = {}
    for rel, path in _worktree_files(repo):
        want = expected.get(rel) or _lookup(run, rel, scope, cone)
        accepted[rel] = (_accept_file(run, rel, path, want), want.executable)
    missing = sorted(set(expected) - set(accepted))
    if missing:
        raise CheckoutVerificationError(f"{missing[0]} is missing from the checkout")
    return _Manifest(algorithm, accepted)


def _key(repo: Path, sha: str) -> tuple[str, str]:
    from apm_cli.utils.realpath import resolve_path

    return sha.lower(), str(resolve_path(Path(repo)))


def verify_checkout(
    repo: Path,
    sha: str,
    *,
    paths: Iterable[str] | None = None,
    cone: bool = False,
    git_exe: str | None = None,
    env: dict[str, str] | None = None,
) -> bool:
    """Whether *repo*'s working tree is exactly what commit *sha* produces.

    *paths* is a sparse checkout's directories: every file under them must
    be present. With *cone*, the checkout holds exactly what ``git
    sparse-checkout --cone`` writes (the root's and each ancestor
    directory's own files too) and nothing else; without it, any other
    present file must still be a verified file of the commit.
    """
    scope = tuple(sorted(p.strip("/") for p in paths)) if paths else None
    if git_exe is None:
        from ..utils.git_env import get_git_executable

        git_exe = get_git_executable()
    try:
        manifest = _verify(Path(repo), sha.lower(), scope, cone, git_exe, env)
    except (CheckoutVerificationError, OSError, ValueError, subprocess.SubprocessError):
        return False
    with _MANIFEST_LOCK:
        _MANIFESTS[_key(repo, sha)] = manifest
    return True


def _full_sha(repo: Path, sha: str | None) -> str:
    """*sha*, or the clone's HEAD commit when *sha* is unknown or abbreviated and HEAD extends it."""
    if sha and len(sha) in (40, 64):
        return sha.lower()
    from .integrity import _read_head_sha

    head = _read_head_sha(Path(repo))
    return head if head and head.startswith((sha or "").lower()) else (sha or "")


def require_verified_checkout(
    repo: Path,
    sha: str | None = None,
    *,
    paths: Iterable[str] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Raise unless *repo* is exactly commit *sha* (see :func:`verify_checkout`)."""
    full = _full_sha(repo, sha)
    if not verify_checkout(Path(repo), full, paths=paths, env=env):
        raise CheckoutVerificationError(
            f"checkout {Path(repo).name} does not match its commit {(full or '?')[:12]}"
        )


def _manifest(
    repo: Path, sha: str | None, paths: Iterable[str] | None, env: dict[str, str] | None
) -> _Manifest:
    full = _full_sha(repo, sha)
    with _MANIFEST_LOCK:
        manifest = _MANIFESTS.get(_key(repo, full))
    if manifest is None:
        require_verified_checkout(repo, full, paths=paths, env=env)
        with _MANIFEST_LOCK:
            manifest = _MANIFESTS[_key(repo, full)]
    return manifest


def _check_bytes(manifest: _Manifest, rel: str, data: bytes, info: os.stat_result) -> None:
    want = manifest.files.get(rel)
    if want is None or git_object_id(manifest.algorithm, "blob", data) != want[0]:
        raise CheckoutVerificationError(f"{rel} does not hold the verified bytes of its commit")
    _check_mode(rel, info, want[1])


_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


def _read_regular(rel: str, path: Path) -> tuple[bytes, os.stat_result]:
    """Read *path* only as a regular file: never through a link, a pipe or a device."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise CheckoutVerificationError(f"{rel} is not a regular file")
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as exc:  # replaced by a link since the lstat
        raise CheckoutVerificationError(f"{rel} is not a regular file") from exc
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        swapped = os.name != "nt" and (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        if not stat.S_ISREG(opened.st_mode) or swapped:
            raise CheckoutVerificationError(f"{rel} is not a regular file")
        return handle.read(), opened


def require_verified_copy(
    dest: Path,
    repo: Path,
    sha: str | None,
    subdir: str = "",
    env: dict[str, str] | None = None,
) -> None:
    """Raise unless every file copied from *repo* (its *subdir*) into *dest* holds verified bytes.

    The bytes are checked against the record of an earlier verification of
    *repo* in this process, or *repo* is verified now (scoped to *subdir*).
    """
    prefix = subdir.strip("/")
    manifest = _manifest(repo, sha, [prefix] if prefix else None, env)
    # A commit never holds a .git entry; one in a copy would make it a repository.
    for _directory, subdirs, names in os.walk(dest):
        if any(_names_git_dir(entry) for entry in (*subdirs, *names)):
            raise CheckoutVerificationError("the copy holds a .git entry")
    copied: set[str] = set()
    for rel, path in _worktree_files(Path(dest)):
        source = _join(prefix, rel)
        _check_bytes(manifest, source, *_read_regular(source, path))
        copied.add(source)
    under = f"{prefix}/" if prefix else ""
    missing = sorted(rel for rel in manifest.files if rel.startswith(under) and rel not in copied)
    if missing:
        raise CheckoutVerificationError(f"{missing[0]} is missing from the copy")


def read_verified_file(
    repo: Path, rel: str, sha: str | None = None, *, env: dict[str, str] | None = None
) -> bytes:
    """Return the bytes of *rel* in checkout *repo*, checked against its verified record."""
    manifest = _manifest(repo, sha or Path(repo).parent.name, None, env)
    data, info = _read_regular(rel, Path(repo) / rel)
    _check_bytes(manifest, rel.strip("/"), data, info)
    return data


def require_verified_bytes(
    repo: Path, sha: str | None, rel: str, data: bytes, scope: str = ""
) -> None:
    """Raise unless *data*, read from *rel* in checkout *repo*, are the bytes verification accepted."""
    manifest = _manifest(repo, sha, [scope.strip("/")] if scope.strip("/") else None, None)
    want = manifest.files.get(rel.strip("/"))
    if want is None or git_object_id(manifest.algorithm, "blob", data) != want[0]:
        raise CheckoutVerificationError(f"{rel} does not hold the verified bytes of its commit")


# Guarantees the fresh landing proved while writing, each usable by ONE read.
_FRESH_USES = frozenset({"verify", "links"})
_INPROCESS: dict[tuple[str, str], tuple[int, int, set[str]]] = {}


def register_inprocess_manifest(repo: Path, sha: str, files: dict[str, tuple[str, bool]]) -> None:
    """Record the verdict an in-process fetch reached while writing *repo*.

    The manifest stays for copy verification. The skip of the commit verify and
    of the link scan applies once each, to the read of this fresh landing, and
    only while *repo* is the same directory (dev, ino); every later read of the
    cache verifies as usual.
    """
    key = _key(repo, sha)
    info = os.lstat(repo)
    with _MANIFEST_LOCK:
        _MANIFESTS[key] = _Manifest(object_format(sha), dict(files))
        _INPROCESS[key] = (info.st_dev, info.st_ino, set(_FRESH_USES))


def inprocess_verified(repo: Path, sha: str | None = None, *, use: str) -> bool:
    """Whether this read is the first *use* of a directory this process wrote and verified."""
    if not _INPROCESS:  # nothing fetched in-process this run: skip the path resolve
        return False
    key = _key(repo, sha or Path(repo).parent.name)
    with _MANIFEST_LOCK:
        entry = _INPROCESS.get(key)
        if entry is None or key not in _MANIFESTS or use not in entry[2]:
            return False
        entry[2].discard(use)
        identity = entry[:2]
    try:
        info = os.lstat(repo)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity


def forget_verdicts() -> None:
    """Drop every record (an evicted or rebuilt checkout, or a new run)."""
    with _MANIFEST_LOCK:
        _MANIFESTS.clear()
        _INPROCESS.clear()
