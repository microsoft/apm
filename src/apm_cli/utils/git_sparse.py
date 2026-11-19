"""Sparse-cone setup (perf #1433) and Git-object link materialization (#3457).

Extracted so the persistent git cache (``cache.git_cache``), the
shared-bare materialization path (``deps.bare_cache``) and the downloader
configure sparse-cone with identical subprocess semantics, and replace
tracked links with their target content through one owner
(:func:`materialize_tracked_symlinks`).
"""

from __future__ import annotations

import bisect
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .git_env import git_line_ending_pin_args, run_git_text
from .path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
    validate_path_segments,
)

FULL_CHECKOUT_TIMEOUT_SECONDS = 300


def _literal_pathspec(path: str) -> str:
    """Return a Git pathspec that treats every character in *path* literally."""
    return f":(literal){path}"


_LINK_MODE = "120000"
_EXECUTABLE_MODE = "100755"
_TREE_MODE = "040000"
# Matches Linux MAXSYMLINKS: bounds link chains and nested directory-link
# expansion so a cyclic package fails instead of recursing without end.
_MAX_LINK_HOPS = 40
# Shared budget across every planned write, so acyclic fan-out (several
# directory links per level) cannot grow exponentially before rejection.
_MAX_LINK_WRITES = 100_000


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    kind: str
    oid: str


@dataclass(frozen=True)
class _LinkWrite:
    """One working-tree write: a directory, or the bytes of ``source``."""

    dest: str
    mode: str
    source: str | None = None


_ROOT_TREE = _TreeEntry(_TREE_MODE, "tree", "")


def _git_output(
    git_exe: str,
    repo_dir: Path,
    args: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
    stdin: bytes | None = None,
) -> bytes:
    return subprocess.run(
        [git_exe, *(extra_git_args or []), "-C", str(repo_dir), *args],
        input=stdin,
        capture_output=True,
        timeout=timeout,
        env=env,
        check=True,
    ).stdout


def _tree_entries(
    git_exe: str,
    repo_dir: Path,
    paths: list[str] | None,
    *,
    with_trees: bool,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
) -> dict[str, _TreeEntry]:
    """Return HEAD tree entries, optionally limited to literal *paths*."""
    args = ["ls-tree", "-r", "-z", *(["-t"] if with_trees else []), "HEAD"]
    if paths:
        args += ["--", *(_literal_pathspec(path) for path in paths)]
    output = _git_output(
        git_exe, repo_dir, args, env=env, timeout=timeout, extra_git_args=extra_git_args
    )
    entries: dict[str, _TreeEntry] = {}
    for record in output.split(b"\0"):
        metadata, separator, relative = record.partition(b"\t")
        if not separator:
            continue
        mode, kind, oid = metadata.decode("ascii").split()
        entries[os.fsdecode(relative)] = _TreeEntry(mode, kind, oid)
    return entries


def _link_texts(
    git_exe: str,
    repo_dir: Path,
    oids: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
) -> dict[str, bytes]:
    """Read raw link blobs in one ``git cat-file --batch`` call."""
    if not oids:
        return {}
    output = _git_output(
        git_exe,
        repo_dir,
        ["cat-file", "--batch"],
        env=env,
        timeout=timeout,
        extra_git_args=extra_git_args,
        stdin=b"".join(oid.encode("ascii") + b"\n" for oid in oids),
    )
    texts: dict[str, bytes] = {}
    offset = 0
    for oid in oids:
        newline = output.index(b"\n", offset)
        header = output[offset:newline].split()
        offset = newline + 1
        if len(header) != 3:
            raise RuntimeError(f"Git object {oid} is missing from the checked-out commit.")
        size = int(header[2])
        texts[oid] = output[offset : offset + size]
        offset += size + 1
    return texts


def _within(path: str, tree: str) -> bool:
    return not tree or path == tree or path.startswith(f"{tree}/")


class _Unresolvable(Exception):
    """Internal resolution outcome, reported against the top-level link."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _require_safe_entry_path(path: str) -> None:
    """Reject Git entry names that are not plain relative paths on every host.

    A POSIX-authored name such as ``a/..\\b`` or ``c:d`` would traverse or
    address an alternate data stream on Windows.
    """
    validate_path_segments(path, context="Git entry path", reject_empty=True)
    if any(char in path for char in ":\\\0"):
        raise PathTraversalError(f"Git entry path '{path}' is not portable to every host")


class _LinkResolver:
    """Resolve tracked links against one commit tree, never the host filesystem."""

    def __init__(
        self,
        entries: dict[str, _TreeEntry],
        load_texts: Callable[[list[str]], dict[str, bytes]],
    ) -> None:
        self._entries = entries
        self._paths = sorted(entries)
        self._load_texts = load_texts
        self._texts: dict[str, bytes] = {}
        self._writes = 0

    def preload(self, links: list[str]) -> None:
        self._texts.update(self._load_texts(sorted({self._entries[link].oid for link in links})))

    def _text(self, link: str) -> str:
        oid = self._entries[link].oid
        if oid not in self._texts:
            self._texts.update(self._load_texts([oid]))
        return os.fsdecode(self._texts[oid])

    def _write(self, dest: str, mode: str, source: str | None = None) -> _LinkWrite:
        self._writes += 1
        if self._writes > _MAX_LINK_WRITES:
            raise _Unresolvable("too-large")
        try:
            _require_safe_entry_path(dest)
            if source is not None:
                _require_safe_entry_path(source)
        except PathTraversalError:
            raise _Unresolvable("unsafe") from None
        return _LinkWrite(dest, mode, source)

    def _resolve(self, link: str) -> tuple[str, _TreeEntry]:
        """Follow every link component like the kernel, contained in the tree."""
        pending = link.split("/")
        resolved: list[str] = []
        hops = 0
        while pending:
            part = pending.pop(0)
            # Any component after a file, even "", "." or "..", is ENOTDIR.
            parent = "/".join(resolved)
            if parent and self._entries[parent].kind != "tree":
                raise _Unresolvable("broken")
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise _Unresolvable("escape")
                resolved.pop()
                continue
            resolved.append(part)
            entry = self._entries.get("/".join(resolved))
            if entry is None:
                raise _Unresolvable("broken")
            if entry.mode != _LINK_MODE:
                continue
            hops += 1
            if hops > _MAX_LINK_HOPS:
                raise _Unresolvable("cycle")
            text = self._text("/".join(resolved))
            if text.startswith("/") or PureWindowsPath(text).drive:
                raise _Unresolvable("escape")
            resolved.pop()
            pending = text.split("/") + pending
        target = "/".join(resolved)
        return target, (self._entries[target] if target else _ROOT_TREE)

    def _descendants(self, tree: str) -> list[str]:
        if not tree:
            return self._paths
        start = bisect.bisect_left(self._paths, f"{tree}/")
        # "0" sorts right after "/", so this bound ends the subtree.
        stop = bisect.bisect_left(self._paths, f"{tree}0")
        return self._paths[start:stop]

    def _expand(self, source: str, dest: str, chain: tuple[str, ...]) -> list[_LinkWrite]:
        chain = (*chain, source)
        if len(chain) > _MAX_LINK_HOPS:
            raise _Unresolvable("cycle")
        target, entry = self._resolve(source)
        if entry.kind == "blob":
            return [self._write(dest, entry.mode, target)]
        if entry.kind != "tree":
            raise _Unresolvable("broken")
        # Expanding a tree that contains a link on the chain never ends.
        if any(_within(link, target) for link in chain):
            raise _Unresolvable("cycle")
        writes = [self._write(dest, _TREE_MODE)]
        offset = len(target) + 1 if target else 0
        for path in self._descendants(target):
            child = self._entries[path]
            child_dest = f"{dest}/{path[offset:]}"
            if child.mode == _LINK_MODE:
                writes.extend(self._expand(path, child_dest, chain))
            elif child.kind == "blob":
                writes.append(self._write(child_dest, child.mode, path))
            else:
                # Trees and submodule gitlinks check out as directories.
                writes.append(self._write(child_dest, _TREE_MODE))
        return writes

    def plan(self, link: str) -> list[_LinkWrite]:
        """Plan the writes that replace *link* with its target's content."""
        try:
            return self._expand(link, link, ())
        except _Unresolvable as outcome:
            text = self._text(link)
            if outcome.kind in {"escape", "unsafe"}:
                where = (
                    "resolves outside the repository's checked-out commit"
                    if outcome.kind == "escape"
                    else "reaches a path that is not portable to every host"
                )
                raise PathTraversalError(
                    f"Symlink '{link}' targets '{text}', which {where}."
                ) from None
            reason = {
                "cycle": "forms a cycle",
                "too-large": f"expands to more than {_MAX_LINK_WRITES} entries",
            }.get(outcome.kind, "is not a tracked file or directory in the checked-out commit")
            raise RuntimeError(
                f"Symlink '{link}' targets '{text}', which {reason}; "
                "repair its target in the package repository."
            ) from None


def _plan_tracked_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str] | None,
    *,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
) -> tuple[dict[str, list[_LinkWrite]], dict[str, Exception]]:
    """Plan link materialization; the common no-link case costs one ``ls-tree``."""
    scoped = _tree_entries(
        git_exe,
        repo_dir,
        paths,
        with_trees=False,
        env=env,
        timeout=timeout,
        extra_git_args=extra_git_args,
    )
    links = sorted(path for path, entry in scoped.items() if entry.mode == _LINK_MODE)
    if not links:
        return {}, {}
    entries = _tree_entries(
        git_exe,
        repo_dir,
        None,
        with_trees=True,
        env=env,
        timeout=timeout,
        extra_git_args=extra_git_args,
    )
    # Link blobs load on demand: a partial clone fetches only the links
    # this scope actually follows.
    resolver = _LinkResolver(
        entries,
        lambda oids: _link_texts(
            git_exe, repo_dir, oids, env=env, timeout=timeout, extra_git_args=extra_git_args
        ),
    )
    resolver.preload(links)
    plans: dict[str, list[_LinkWrite]] = {}
    failures: dict[str, Exception] = {}
    for link in links:
        try:
            plans[link] = resolver.plan(link)
        except (PathTraversalError, RuntimeError) as exc:
            failures[link] = exc
    return plans, failures


class _CheckoutBytes:
    """Return the bytes Git checks out for a tracked blob path."""

    def __init__(
        self,
        git_exe: str,
        repo_dir: Path,
        *,
        env: dict[str, str] | None,
        timeout: int,
        extra_git_args: list[str] | None,
    ) -> None:
        self._git_exe = git_exe
        self._repo_dir = repo_dir
        self._env = env
        self._timeout = timeout
        # Checkout conversion needs the same line-ending pins as the checkout
        # itself; callers that pass no pins would otherwise inherit the host
        # ``core.eol`` (#3270).
        self._extra_git_args = [*(extra_git_args or []), *git_line_ending_pin_args()]
        self._cache: dict[str, bytes] = {}

    def __call__(self, source: str) -> bytes:
        if source not in self._cache:
            self.prime([source])
        return self._cache[source]

    def prime(self, sources: list[str]) -> None:
        """Load every source; Git checks out the missing ones in one batch."""
        missing: list[str] = []
        for source in dict.fromkeys(sources):
            if source in self._cache:
                continue
            _require_safe_entry_path(source)
            content = self._worktree_bytes(source)
            if content is None:
                missing.append(source)
            else:
                self._cache[source] = content
        if missing:
            self._cache.update(self._check_out(missing))

    def _worktree_bytes(self, source: str) -> bytes | None:
        # A tracked blob Git already wrote is exactly what a POSIX link follows.
        path = self._repo_dir / source
        try:
            regular = stat.S_ISREG(path.lstat().st_mode)
        except FileNotFoundError:
            return None
        if regular and not has_symlink_component(self._repo_dir, path, raise_on_error=True):
            return path.read_bytes()
        return None

    def _check_out(self, sources: list[str]) -> dict[str, bytes]:
        """Run Git's own checkout for targets a sparse cone left out.

        A throwaway index (``read-tree HEAD``) and work tree let
        ``checkout-index`` apply exactly the conversion ``git checkout``
        applies, with ``.gitattributes`` read from the commit through that
        index. ``cat-file --filters`` reads no index, so in a sparse checkout
        it silently skipped attributes such as ``eol=crlf`` (#3457).
        """
        from ..config import get_apm_temp_dir

        scratch = Path(tempfile.mkdtemp(prefix="apm-link-", dir=get_apm_temp_dir()))
        try:
            env = dict(self._env if self._env is not None else os.environ)
            env["GIT_INDEX_FILE"] = str(scratch / "index")
            tree = scratch / "tree"
            tree.mkdir()
            # A full, non-sparse throwaway index: no skip-worktree bits and no
            # sparse-directory entries can hide a requested path.
            head = ["-c", "core.sparseCheckout=false", "-c", "index.sparse=false"]
            for args, stdin in (
                ([*head, "read-tree", "HEAD"], None),
                (
                    [*head, f"--work-tree={tree}", "checkout-index", "--stdin", "-z"],
                    b"".join(os.fsencode(source) + b"\0" for source in sources),
                ),
            ):
                _git_output(
                    self._git_exe,
                    self._repo_dir,
                    args,
                    env=env,
                    timeout=self._timeout,
                    extra_git_args=self._extra_git_args,
                    stdin=stdin,
                )
            loaded: dict[str, bytes] = {}
            for source in sources:
                path = tree / source
                try:
                    regular = stat.S_ISREG(path.lstat().st_mode)
                except FileNotFoundError:
                    regular = False
                if not regular:
                    raise RuntimeError(f"Git did not check out '{source}' as a regular file.")
                loaded[source] = path.read_bytes()
            return loaded
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def _write_link_plan(
    repo_dir: Path, writes: list[_LinkWrite], checkout_bytes: _CheckoutBytes
) -> None:
    root = writes[0].dest
    _require_safe_entry_path(root)
    top = repo_dir / root
    ensure_path_within(top.parent, repo_dir)
    if has_symlink_component(repo_dir, top.parent, raise_on_error=True):
        raise PathTraversalError(f"Refusing to materialize '{root}' through a linked directory.")
    if os.path.islink(top) or top.is_file():
        top.unlink()
    elif top.is_dir():
        shutil.rmtree(top)
    for write in writes:
        dest = repo_dir / write.dest
        if write.source is None:
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(checkout_bytes(write.source))
        if write.mode == _EXECUTABLE_MODE:
            mode = dest.stat().st_mode
            dest.chmod(mode | ((mode & 0o444) >> 2))


def materialize_tracked_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str] | None,
    *,
    env: dict[str, str] | None,
    timeout: int = 30,
    extra_git_args: list[str] | None = None,
    strict: bool = True,
) -> list[str]:
    """Replace each tracked link in *paths* with its target content (#3457).

    Dependency checkouts pin link text (``git_env.git_symlink_pin_args``),
    so Git writes every link as a file holding its target and never needs OS
    symlink capability. This owner resolves each mode-120000 entry under
    *paths* (``None`` = whole repository) against the checked-out commit's
    tree, following links in every component with POSIX semantics, and
    writes the target's checkout bytes and executable bit, or expands a
    directory link recursively. Every host therefore produces the
    tree a POSIX checkout yields when its links are followed, so the package
    ``content_hash`` is identical on Linux, macOS and Windows. Targets come
    from Git objects, so links that leave a sparse cone resolve without
    widening it (#2707); a partial clone lazily fetches only those blobs.

    Links that escape the commit tree, are broken, or loop are not written.
    With ``strict`` the first one raises before any write; otherwise they
    stay as Git link text for :func:`validate_materialized_symlinks` to
    reject when a package is selected from a shared checkout.

    Returns the repository-relative links that were materialized.
    """
    plans, failures = _plan_tracked_symlinks(
        git_exe, repo_dir, paths, env=env, timeout=timeout, extra_git_args=extra_git_args
    )
    if strict and failures:
        raise failures[min(failures)]
    checkout_bytes = _CheckoutBytes(
        git_exe,
        repo_dir,
        env=env,
        timeout=max(timeout, FULL_CHECKOUT_TIMEOUT_SECONDS),
        extra_git_args=extra_git_args,
    )
    checkout_bytes.prime(
        [write.source for writes in plans.values() for write in writes if write.source]
    )
    for link in sorted(plans):
        _write_link_plan(repo_dir, plans[link], checkout_bytes)
    return sorted(plans)


def validate_materialized_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str] | None,
    *,
    env: dict[str, str] | None,
    timeout: int = 30,
    extra_git_args: list[str] | None = None,
) -> None:
    """Reject escaping, broken or cyclic tracked links in *paths* before copy."""
    _, failures = _plan_tracked_symlinks(
        git_exe, repo_dir, paths, env=env, timeout=timeout, extra_git_args=extra_git_args
    )
    if failures:
        raise failures[min(failures)]


def sparse_checkout_active(
    git_exe: str,
    repo_dir: Path,
    *,
    env: dict[str, str] | None,
    timeout: int = 10,
    extra_git_args: list[str] | None = None,
) -> bool:
    """Return whether Git still considers *repo_dir* a sparse checkout."""
    head = [git_exe, *(extra_git_args or [])]
    result = run_git_text(
        [*head, "-C", str(repo_dir), "config", "--bool", "core.sparseCheckout"],
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def apply_sparse_cone(
    git_exe: str,
    repo_dir: Path,
    paths: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int = 30,
    extra_git_args: list[str] | None = None,
) -> None:
    """Initialize cone-mode sparse checkout and set the requested paths.

    Issues ``git sparse-checkout init --cone`` followed by
    ``git sparse-checkout set <paths...>`` inside ``repo_dir``. Both
    subprocesses run with ``check=True``; failures propagate to the
    caller so silent fallback to a full checkout (which would defeat
    the perf invariant from #1433) is impossible.

    Args:
        git_exe: Absolute path to the git executable.
        repo_dir: Repository working tree to configure.
        paths: Top-level cone paths to materialize. Must be non-empty.
        env: Subprocess environment (auth / safe.bareRepository etc.).
        timeout: Per-subprocess timeout in seconds.
        extra_git_args: Extra args inserted between the git executable
            and the first subcommand (e.g. ``["-c", "core.longpaths=true"]``
            on Windows so the long staged path under ``checkouts_v1/``
            does not trip MAX_PATH when git locks ``.git/config``).
    """
    if not paths:
        return
    head = [git_exe, *(extra_git_args or [])]
    run_git_text(
        [*head, "-C", str(repo_dir), "sparse-checkout", "init", "--cone"],
        capture_output=True,
        timeout=timeout,
        env=env,
        check=True,
    )
    run_git_text(
        [*head, "-C", str(repo_dir), "sparse-checkout", "set", *paths],
        capture_output=True,
        timeout=timeout,
        env=env,
        check=True,
    )
