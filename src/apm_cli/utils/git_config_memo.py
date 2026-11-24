"""One command's repeated effective Git config probes are answered once.

``utils/git_env._read_effective_git_config`` spawns ``git config --list`` to
read the configuration one Git child would see. A large install asks the same
question several times per dependency with identical inputs. Inside an
invocation (see :mod:`apm_cli.core.invocation_memo`) this module keeps the
parsed snapshot, which is the INPUT to every URL-rewrite, credential and
header check. It never keeps a verdict: each caller still validates the
snapshot on every call.

A snapshot is reused only while every config file Git could read for that
probe is byte-identical. The key is the exact probe input (the full child
environment, the git executable, and the real path of the repository it
reads), and each lookup re-hashes the full candidate set: the system and
global files Git reports (``git var``), the repository's ``config`` and
``config.worktree``, and every ``include.path``/``includeIf`` target. Files
that do not exist are part of the set, so creating one invalidates. A probe
that fails is never kept, and an ``includeIf "onbranch:"`` snapshot is never
kept.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from apm_cli.core.invocation_memo import invocation_memo

_T = TypeVar("_T")
_MEMO = "git-config-probe"
_lock = threading.Lock()
# A repository chosen through the environment is located by Git, not by APM.
_REPO_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")
_HOME_ENV = ("HOME", "USERPROFILE")
_PATH_ENV = (
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "XDG_CONFIG_HOME",
    "PREFIX",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
)
_NULL_DEVICES = frozenset({"/dev/null", "nul", os.devnull.lower()})
# (path, exists, size, sha256): the stat-free identity of one candidate file.
_FileId = tuple[str, bool, int, str]


@dataclass(frozen=True)
class _Kept:
    candidates: tuple[str, ...]
    fingerprint: tuple[_FileId, ...]
    snapshot: Any = field(repr=False)


def _file_id(path: str) -> _FileId | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except (FileNotFoundError, NotADirectoryError):
        return (path, False, 0, "")
    except OSError:
        return None
    return (path, True, len(data), hashlib.sha256(data).hexdigest())


def fingerprint(
    paths: Iterable[str], file_id: Callable[[str], _FileId | None] = _file_id
) -> tuple[_FileId, ...] | None:
    """Return the content identity of *paths*, or ``None`` if one cannot be read."""
    ids = []
    for path in paths:
        identity = file_id(path)
        if identity is None:
            return None
        ids.append(identity)
    return tuple(ids)


def _real(path: Path | str | None) -> str | None:
    return None if path is None else os.path.realpath(path)


def _repository_files(git_dir: Path | None, worktree: Path | None) -> tuple[str, ...] | None:
    """Return the repository config files Git reads, or ``None`` if APM cannot tell."""
    if git_dir is None and worktree is None:
        return ()
    base = Path(_real(git_dir) or Path(_real(worktree) or "") / ".git")
    # A ``.git`` file (linked worktree, submodule) or a shared common dir is
    # resolved by Git itself; such a probe is not kept.
    if not base.is_dir() or (base / "commondir").exists():
        return None
    # ``commondir`` stays a candidate: creating it later redirects Git elsewhere.
    return (str(base / "config"), str(base / "config.worktree"), str(base / "commondir"))


_MAX_INCLUDE_DEPTH = 10  # Git's own limit on nested includes


def _include_files(
    entries: Iterable[Any], files: Iterable[str], env: Mapping[str, str]
) -> tuple[str, ...] | None:
    """Return every file an include in *entries* may name, or ``None`` if not keepable.

    A relative target resolves against the directory of every existing file
    that could hold it (Git reads includes only from files it read), nested
    to Git's depth limit: an over-approximation only re-probes more.
    """
    homes = [env[name] for name in _HOME_ENV if env.get(name)]
    absolute: set[str] = set()
    relative: set[str] = set()
    for entry in entries:
        key = entry.key.lower()
        if key.startswith("includeif.") and key.endswith(".path"):
            if key[len("includeif.") :].startswith("onbranch:"):
                return None
        elif key != "include.path":
            continue
        value = entry.value
        if value == "~" or value.startswith(("~/", "~\\")):
            if not homes:
                return None
            absolute.update(os.path.join(home, value[2:]) for home in homes)
        elif value.startswith("~"):
            return None  # ``~user/``: another user's home, resolved by Git alone
        elif os.path.isabs(value):
            absolute.add(value)
        else:
            relative.add(value)
    # Git for Windows opens a drive-less rooted path inside its own install.
    if os.name == "nt" and any(not Path(path).drive for path in absolute):
        return None
    known = set(files) | absolute
    readers = {path for path in known if os.path.isfile(path)}
    for _ in range(_MAX_INCLUDE_DEPTH):
        found = {
            os.path.normpath(os.path.join(os.path.dirname(path), rel))
            for path in readers
            for rel in relative
        } - known
        if not found:
            return tuple(sorted(known - set(files)))
        known |= found
        readers = {path for path in found if os.path.isfile(path)}
    return None


def git_config_paths(env: Mapping[str, str]) -> tuple[str, ...] | None:
    """Return the system and global config files Git reads under *env*, or ``None``."""
    from apm_cli.utils.git_env import (
        GitUrlRewriteProbeError,
        _run_git_config_probe,
        get_git_executable,
    )

    git = get_git_executable()
    paths: list[str] = []
    # Git reads no system file under a true GIT_CONFIG_NOSYSTEM, and ``git var``
    # then refuses to name one.
    no_system = env.get("GIT_CONFIG_NOSYSTEM", "").strip().lower() in {"1", "true", "yes", "on"}
    for name in ("GIT_CONFIG_GLOBAL",) if no_system else ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
        try:
            result = _run_git_config_probe(
                [git, "var", name],
                capture_output=True,
                check=False,
                cwd=str(Path(git).resolve().parent),
                env=dict(env),
                timeout_category="Git config probe timed out",
            )
        except (OSError, GitUrlRewriteProbeError):
            return None
        if result.returncode != 0 or not isinstance(result.stdout, bytes):
            return None
        try:
            text = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None  # a path this process cannot name exactly is not fingerprinted
        paths.extend(line for line in text.splitlines() if line)
    # The null device names no file. Any other path this process would open
    # differently from Git (an MSYS-style ``/etc/gitconfig`` on Windows)
    # cannot be fingerprinted, so the probe is not memoized.
    files = tuple(p for p in paths if p.lower() not in _NULL_DEVICES)
    if not all(os.path.isabs(p) and (os.name != "nt" or Path(p).drive) for p in files):
        return None
    return files


def invalidate(path: Path | str) -> None:
    """Drop every kept snapshot that reads a file under *path* (APM wrote its config)."""
    memo = invocation_memo(_MEMO)
    if memo is None:
        return
    root = os.path.join(os.path.realpath(path), "")
    with _lock:
        for key in [k for k, v in memo.items() if isinstance(v, _Kept)]:
            kept = memo[key]
            if any(os.path.join(c, "").startswith(root) for c in kept.candidates):
                del memo[key]


def read(
    probe: Callable[[], _T],
    env: Mapping[str, str],
    *,
    git: str,
    git_dir: Path | None,
    worktree: Path | None,
    config_paths: Callable[[Mapping[str, str]], tuple[str, ...] | None],
    file_id: Callable[[str], _FileId | None] = _file_id,
) -> _T:
    """Return ``probe()``, reusing this invocation's snapshot while its files are unchanged."""
    memo = invocation_memo(_MEMO)
    if memo is None or any(name in env for name in _REPO_ENV):
        return probe()
    key = ("snapshot", git, tuple(sorted(env.items())), _real(git_dir), _real(worktree))
    with _lock:
        kept = memo.get(key)
        key_lock = memo.setdefault(("lock", key), threading.Lock())
    if isinstance(kept, _Kept) and fingerprint(kept.candidates, file_id) == kept.fingerprint:
        return kept.snapshot
    # One miss per key at a time: concurrent workers wait and share its answer.
    with key_lock:
        with _lock:
            kept = memo.get(key)
        if isinstance(kept, _Kept) and fingerprint(kept.candidates, file_id) == kept.fingerprint:
            return kept.snapshot
        return _miss(probe, env, memo, key, git, git_dir, worktree, config_paths, file_id)


def _miss(
    probe: Callable[[], _T],
    env: Mapping[str, str],
    memo: dict[Any, Any],
    key: tuple[Any, ...],
    git: str,
    git_dir: Path | None,
    worktree: Path | None,
    config_paths: Callable[[Mapping[str, str]], tuple[str, ...] | None],
    file_id: Callable[[str], _FileId | None],
) -> _T:
    paths_key = ("paths", git, tuple((name, env.get(name)) for name in _PATH_ENV))
    with _lock:
        scope_files = memo.get(paths_key)
    if scope_files is None:
        scope_files = config_paths(env)
        if scope_files is None:
            return probe()
        with _lock:
            memo[paths_key] = scope_files
    repository = _repository_files(git_dir, worktree)
    if repository is None:
        return probe()
    with _lock:
        hint = memo.get(("includes", key), ())
    candidates = tuple(sorted({*scope_files, *repository, *hint}))
    before = fingerprint(candidates, file_id)
    snapshot = probe()
    entries = snapshot.entries  # type: ignore[attr-defined]
    includes = _include_files(entries, (*scope_files, *repository), env)
    if before is None or includes is None:
        return snapshot
    after_set = tuple(sorted({*scope_files, *repository, *includes}))
    # Keep only when the files Git read were the same bytes before and after the
    # probe; an include first seen now is fingerprinted and kept on the next miss.
    if after_set == candidates and fingerprint(after_set, file_id) == before:
        with _lock:
            memo[key] = _Kept(candidates, before, snapshot)
    else:
        with _lock:
            memo[("includes", key)] = includes
    return snapshot


def forget() -> None:
    """Drop every kept snapshot of the current invocation (tests and reset paths)."""
    memo = invocation_memo(_MEMO)
    if memo is not None:
        with _lock:
            memo.clear()


_TLS_STAMP_ENV = (
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "PREFIX",
)


def ambient_tls_stamp() -> tuple:
    """What the ambient TLS probe read: its env, the config files it names, user-code runs.

    A kept answer is reused only while this is unchanged, so a lifecycle
    script that edits ``~/.gitconfig`` (``forget_invocation_memos``), a new
    HOME or a changed config file all re-probe.
    """
    from apm_cli.core.invocation_memo import forget_epoch

    environ = os.environ
    named = tuple((name, environ.get(name)) for name in _TLS_STAMP_ENV)
    value = dict(named)
    home = value["HOME"] or value["USERPROFILE"]
    xdg = value["XDG_CONFIG_HOME"] or (os.path.join(home, ".config") if home else None)
    candidates = (
        value["GIT_CONFIG_GLOBAL"],
        value["GIT_CONFIG_SYSTEM"],
        os.path.join(home, ".gitconfig") if home else None,
        os.path.join(xdg, "git", "config") if xdg else None,
    )
    files = []
    for path in candidates:
        if not path:
            continue
        try:
            info = os.stat(path)
            files.append((path, info.st_ino, info.st_size, info.st_mtime_ns))
        except OSError:
            files.append((path, None))
    carried = tuple(
        sorted(
            (name, value)
            for name, value in environ.items()
            if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        )
    )
    return (forget_epoch(), named, carried, tuple(files))
