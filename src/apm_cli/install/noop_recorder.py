"""Input recorder of the no-op install fast path (#4770).

Observes one full ``apm install`` run: every file read (audit ``open``),
directory listed, path probed (``os.stat``/``lstat``/``readlink`` hooks and
``os.path.realpath`` answers), environment variable read, and every write,
subprocess, network or prompt event that disqualifies the run from being
replayed. ``install/noop_fast_path.py`` owns the replay decision and the
stamp; this module only observes. Stdlib-only at module scope.
"""

from __future__ import annotations

import functools
import hashlib
import io
import os
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

DISABLE_ENV = "APM_INSTALL_FAST_PATH"
_DISABLE_ENV = DISABLE_ENV

# Subprocesses a recorded no-op may run: org policy discovery reads the project
# remote (re-evaluated live on every replay) and GitPython probes the version.
_ALLOWED_GIT_SUBCOMMANDS = frozenset({"remote", "version"})
_WRITE_EVENTS = frozenset(
    {
        "os.remove",
        "os.rmdir",
        "os.rename",
        "os.mkdir",
        "os.symlink",
        "os.link",
        "os.truncate",
        "os.chmod",
        "os.chown",
        "os.utime",
        "os.chflags",
        "os.lchflags",
        "os.lchmod",
        "os.setxattr",
        "os.removexattr",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "shutil.make_archive",
        "shutil.unpack_archive",
    }
)
_VIOLATION_EVENTS = frozenset(
    {
        "socket.connect",
        "socket.getaddrinfo",
        "socket.sendto",
        "socket.bind",
        "urllib.Request",
        "builtins.input",
        "getpass.getpass",
        "sqlite3.connect",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        "pty.spawn",
        "ctypes.dlopen",
    }
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _file_digest(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        return "dir"
    except OSError as error:
        return f"error:{error.errno}"


def _probe(path: str) -> list[Any] | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        return [f"error:{error.errno}"]
    entry: list[Any] = list(_identity(info))
    if stat.S_ISLNK(info.st_mode):
        try:
            entry.append(os.readlink(path))
        except OSError as error:
            entry.append(f"error:{error.errno}")
        try:
            entry.append(list(_identity(os.stat(path))))
        except OSError as error:
            entry.append(f"error:{error.errno}")
    return entry


def _identity(info: os.stat_result) -> tuple[int, ...]:
    """Every stat field a trust or ownership check may read (not atime).

    For a directory, size, mtime, ctime and link count only reflect its
    entries, which change whenever anything is added beside them (a temp dir,
    a parent of the project). They are not inputs of a run that did not list
    the directory; a run that did list it records the entries themselves.
    """
    if stat.S_ISDIR(info.st_mode):
        return (info.st_mode, info.st_ino, info.st_dev, info.st_uid, info.st_gid)
    return (
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_ino,
        info.st_dev,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
    )


def _listing(path: str) -> list[Any] | None:
    try:
        with os.scandir(path) as entries:
            rows = []
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                    rows.append([entry.name, *_identity(info)])
                except OSError as error:
                    rows.append([entry.name, f"error:{error.errno}"])
    except FileNotFoundError:
        return None
    except OSError as error:
        return [f"error:{error.errno}"]
    rows.sort(key=lambda row: row[0])
    return rows


def _env_digest(names: list[str] | None, environ: dict[str, str] | None = None) -> str:
    environ = dict(os.environ) if environ is None else environ
    selected = sorted(environ) if names is None else sorted(set(names))
    selected = [name for name in selected if name != _DISABLE_ENV]
    hasher = hashlib.sha256()
    for name in selected:
        value = environ.get(name)
        hasher.update(name.encode("utf-8", "surrogateescape") + b"\x00")
        hasher.update(
            b"\x01" if value is None else b"\x02" + value.encode("utf-8", "surrogateescape")
        )
        hasher.update(b"\x00")
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


_os_stat = os.stat
_os_lstat = os.lstat
_os_realpath = os.path.realpath


def _snapshot(path: str) -> tuple[Any, ...] | None:
    """Identity of *path* (and of its referent, when a link) when first observed.

    Any later change refuses the stamp, so the saved digest is bound to the
    bytes the run consumed.
    """
    try:
        info = _os_lstat(path)
    except OSError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        return _identity(info)
    try:
        referent: tuple[int, ...] | None = _identity(_os_stat(path))
    except OSError:
        referent = None
    return (_identity(info), referent)


class _Recorder:
    """Observed inputs of one run.

    *excluded_roots* (interpreter, APM code, stamps, policy cache) are neither
    inputs nor writes. A write under *scratch_roots* (the temp dir) is allowed
    unless it is also under *protected_roots* (the project). Each input is
    snapshotted when first observed so a change before the stamp is written
    (a concurrent edit) refuses the stamp instead of pairing new bytes with
    old output.
    """

    def __init__(
        self,
        excluded_roots: tuple[str, ...],
        scratch_roots: tuple[str, ...] = (),
        protected_roots: tuple[str, ...] = (),
        expiring_roots: tuple[str, ...] = (),
        immutable_roots: tuple[str, ...] = (),
    ) -> None:
        self.reads: set[str] = set()
        self.listed: set[str] = set()
        self.probed: set[str] = set()
        self.resolved: dict[str, str] = {}
        self.observed: dict[str, Any] = {}
        # Entries of each listed directory as first observed (revalidated before stamping).
        self.listings: dict[str, Any] = {}
        # Temp paths this invocation created: only these are scratch.
        self.created: set[str] = set()
        self.env: set[str] = set()
        self.env_all = False
        self.violations: list[str] = []
        # Trace mode keeps observing after the first violation to list them all.
        self.diagnose = os.environ.get(_DISABLE_ENV, "").strip().lower() == "trace"
        self.excluded_roots = excluded_roots
        self.scratch_roots = scratch_roots
        self.protected_roots = protected_roots
        # Caches whose answer depends on the clock (TTL) are not replayable;
        # content-addressed git buckets inside them are.
        self.expiring_roots = expiring_roots
        self.immutable_roots = immutable_roots

    def _path(self, value: Any) -> str | None:
        if isinstance(value, int) or value is None:
            return None
        try:
            text = os.fsdecode(os.fspath(value))
        except TypeError:
            return None
        if text.startswith("/") and "/." not in text and "//" not in text:
            return text.rstrip("/") or "/"  # already absolute and normal: skip normpath
        if ".." in text.replace("\\", "/").split("/"):
            # abspath would collapse '..' before symlinks resolve, so the
            # fingerprinted file could differ from the one read: refuse.
            self.violate(f"parent-component path:{text}"[:200])
            return None
        return os.path.abspath(text)

    def _excluded(self, path: str) -> bool:
        return _under(path, self.excluded_roots)

    def _in_temp(self, path: str) -> bool:
        return _under(path, self.scratch_roots) and not _under(path, self.protected_roots)

    def _scratch(self, path: str) -> bool:
        """Under a temp path this invocation created (its own output, never an input)."""
        return bool(self.created) and self._in_temp(path) and _under(path, tuple(self.created))

    def _create(self, path: str) -> bool:
        """Record a new temp path; False when *path* is not an invocation-owned temp path."""
        if not self._in_temp(path):
            return False
        self.created.add(path)
        return True

    def _observe(self, path: str) -> None:
        if path not in self.observed:
            self.observed[path] = _snapshot(path)

    def violate(self, reason: str) -> None:
        """One violation settles it: stop observing so the rest of the run pays nothing."""
        global _ACTIVE
        if len(self.violations) < 50:
            self.violations.append(reason)
        if _ACTIVE is self and not self.diagnose:
            _ACTIVE = None

    def on_event(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            self._on_open(args)
        elif event in ("os.listdir", "os.scandir"):
            path = self._path(args[0]) if args else os.getcwd()
            if path is not None and not (self._excluded(path) or self._scratch(path)):
                self.listed.add(path)
                self._observe(path)
                if path not in self.listings:
                    self.listings[path] = _listing(path)
        elif event == "subprocess.Popen":
            argv = args[1] if len(args) > 1 else None
            if not (_allowed_subprocess(argv) or _within_policy_discovery()):
                # Name the program only: a command line may carry credentials.
                program = os.path.basename(os.fsdecode(argv[0])) if argv else "?"
                self.violate(f"subprocess:{program}")
        elif event in _WRITE_EVENTS:
            self._on_write(event, args)
        elif event in _VIOLATION_EVENTS and not _within_policy_discovery():
            self.violate(event)

    def _on_open(self, args: tuple[Any, ...]) -> None:
        path = self._path(args[0])
        if path is None or self._excluded(path):
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        if writes:
            if path == os.devnull or path.startswith("/dev/fd/"):
                return  # discarding output is not a write
            exclusive = (isinstance(mode, str) and "x" in mode) or bool(
                isinstance(flags, int) and flags & os.O_CREAT and flags & os.O_EXCL
            )
            if not (self._scratch(path) or (exclusive and self._create(path))):
                self.violate(f"write:{path}")
        elif self._scratch(path):
            return  # the run's own temporary files are outputs, not inputs
        elif _under(path, self.expiring_roots) and not _under(path, self.immutable_roots):
            self.violate(f"expiring-cache:{path}")
        else:
            self.reads.add(path)
            self._observe(path)

    def _on_write(self, event: str, args: tuple[Any, ...]) -> None:
        """Every path a mutating event names must be scratch, excluded, or a no-op."""
        if _is_noop_write(event, args):
            return
        if event == "os.mkdir" and args:
            path = self._path(args[0])
            if path is not None and self._create(path):
                return
        named = [arg for arg in args if isinstance(arg, (str, bytes, os.PathLike))]
        # CPython passes -1 for an unused dir_fd; only a real descriptor is unattributable.
        if any(
            isinstance(arg, int) and not isinstance(arg, bool) and arg != -1 for arg in args[1:]
        ):
            if event not in ("os.chmod", "os.chown", "os.utime", "os.mkdir"):
                self.violate(f"{event}:dir_fd")
                return
        if not named:
            self.violate(f"{event}:unattributed")
            return
        for arg in named:
            path = self._path(arg)
            if path is None or not (self._excluded(path) or self._scratch(path)):
                self.violate(f"{event}:{path}")
                return

    def on_probe(self, value: Any) -> None:
        path = self._path(value)
        if path is not None and not (self._excluded(path) or self._scratch(path)):
            self.probed.add(path)
            self._observe(path)

    def on_resolve(self, value: Any, strict: bool, result: Any) -> None:
        path = self._path(value)
        if path is not None and not self._excluded(path):
            self.resolved.setdefault(f"{int(bool(strict))}|{path}", os.fsdecode(result))


def _is_noop_write(event: str, args: tuple[Any, ...]) -> bool:
    """``mkdir(exist_ok=True)`` on an existing directory and a same-mode chmod change nothing."""
    if not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return False
    try:
        info = _os_stat(args[0])
    except OSError:
        return False
    if event == "os.mkdir":
        return stat.S_ISDIR(info.st_mode)
    if event == "os.chmod" and len(args) > 1 and isinstance(args[1], int):
        return stat.S_IMODE(info.st_mode) == stat.S_IMODE(args[1])
    return False


_POLICY_DISCOVERY_FRAME = ("discover_policy_with_chain", os.path.join("policy", "discovery.py"))


def _within_policy_discovery() -> bool:
    """True while org policy discovery runs: its network, credential lookups and git
    reads are re-evaluated live on every replay, so they are not recorded inputs."""
    frame = sys._getframe(2)
    name, filename = _POLICY_DISCOVERY_FRAME
    while frame is not None:
        code = frame.f_code
        if code.co_name == name and code.co_filename.endswith(filename):
            return True
        frame = frame.f_back
    return False


def _allowed_subprocess(argv: Any) -> bool:
    if not isinstance(argv, (list, tuple)) or len(argv) < 2:
        return False
    program = os.path.basename(os.fsdecode(argv[0]))
    if program not in {"git", "git.exe"}:
        return False
    return os.fsdecode(argv[1]) in _ALLOWED_GIT_SUBCOMMANDS


_ACTIVE: _Recorder | None = None
_HOOK_INSTALLED = False


_BUSY = threading.local()


def _observing(observe: Callable[[_Recorder], None]) -> None:
    """Run one observation; never re-enter, never let it fail the install.

    The recorder's own snapshots (a directory listing, an lstat) raise audit
    events too: those are not the run's inputs. Any error in observing
    refuses the stamp instead of propagating into the command.
    """
    recorder = _ACTIVE
    if recorder is None or getattr(_BUSY, "active", False):
        return
    _BUSY.active = True
    try:
        observe(recorder)
    except Exception as error:
        recorder.violate(f"recorder error: {type(error).__name__}")
    finally:
        _BUSY.active = False


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if _ACTIVE is not None:
        _observing(lambda recorder: recorder.on_event(event, args))


def _probing(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def probe(path: Any, *args: Any, **kwargs: Any) -> Any:
        if _ACTIVE is not None and not kwargs.get("dir_fd"):
            _observing(lambda recorder: recorder.on_probe(path))
        return original(path, *args, **kwargs)

    return probe


def _resolving(original: Callable[..., Any]) -> Callable[..., Any]:
    """Record ``os.path.realpath`` answers: its internal lstat calls may be unobservable."""

    @functools.wraps(original)
    def resolve(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(path, *args, **kwargs)
        if _ACTIVE is not None:
            strict = bool(kwargs.get("strict", args[0] if args else False))
            _observing(lambda recorder: recorder.on_resolve(path, strict, result))
        return result

    return resolve


def _resolution(key: str) -> str:
    strict, _, path = key.partition("|")
    try:
        return _os_realpath(path, strict=strict == "1")
    except OSError as error:
        return f"error:{error.errno}"


class _RecordingEnviron(os._Environ):  # type: ignore[name-defined,misc]
    def __getitem__(self, key: str) -> str:
        recorder = _ACTIVE
        if recorder is not None:
            recorder.env.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        recorder = _ACTIVE
        if recorder is not None and isinstance(key, str):
            recorder.env.add(key)
        return super().__contains__(key)

    def __iter__(self):
        recorder = _ACTIVE
        if recorder is not None:
            recorder.env_all = True
        return super().__iter__()


class _Tee(io.TextIOBase):
    def __init__(self, stream: Any, sink: list[tuple[int, str]], channel: int) -> None:
        self._stream = stream
        self._sink = sink
        self._channel = channel

    def write(self, text: str) -> int:
        self._sink.append((self._channel, text))
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._stream.encoding

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return self._stream.errors


class _Recording:
    """Context of one recorded full run."""

    def __init__(
        self,
        key: dict[str, Any],
        stamp: Path,
        policy: str | None,
        started: float,
        project_root: Path,
    ) -> None:
        self.key = key
        self.stamp = stamp
        self.policy = policy
        self.started = started
        self.output: list[tuple[int, str]] = []
        excluded = _interpreter_roots()
        excluded.add(str(Path(__file__).resolve().parents[1]))
        excluded.add(str(stamp.parent))
        excluded.update(_policy_cache_roots())
        expiring, immutable = _cache_roots()
        protected = _with_realpaths(_protected_roots(project_root))
        excluded_roots = _with_realpaths(excluded)
        self.recorder = _Recorder(
            excluded_roots,
            _with_realpaths({tempfile.gettempdir()}),
            protected,
            _with_realpaths(expiring),
            _with_realpaths(immutable),
        )
        self._saved: dict[str, Any] = {}
        if any(_under(root, excluded_roots) for root in protected):
            # The project or user state lives inside an interpreter prefix,
            # whose reads are not recorded: nothing there could be proven.
            self.recorder.violate("protected root inside an excluded prefix")

    def __enter__(self) -> _Recording:
        global _ACTIVE, _HOOK_INSTALLED
        if not _HOOK_INSTALLED:
            sys.addaudithook(_audit_hook)
            _HOOK_INSTALLED = True
        self.environ_start = dict(os.environ)
        self._saved = {
            "stat": os.stat,
            "lstat": os.lstat,
            "readlink": os.readlink,
            "realpath": os.path.realpath,
            "environ_class": os.environ.__class__,
            "stdout": sys.stdout,
            "stderr": sys.stderr,
        }
        os.stat = _probing(os.stat)
        os.lstat = _probing(os.lstat)
        os.readlink = _probing(os.readlink)
        os.path.realpath = _resolving(os.path.realpath)
        os.environ.__class__ = _RecordingEnviron
        sys.stdout = _Tee(sys.stdout, self.output, 1)
        sys.stderr = _Tee(sys.stderr, self.output, 2)
        self._redirect_log_streams()
        _ACTIVE = self.recorder
        self._canary()
        return self

    def _redirect_log_streams(self) -> None:
        """Logging handlers bound to the real streams must also reach the tee."""
        import logging

        self._log_streams: list[tuple[Any, Any]] = []
        originals = {id(self._saved["stdout"]): sys.stdout, id(self._saved["stderr"]): sys.stderr}
        loggers = [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]
        for logger in loggers:
            for handler in getattr(logger, "handlers", ()):
                stream = getattr(handler, "stream", None)
                if isinstance(handler, logging.StreamHandler) and id(stream) in originals:
                    self._log_streams.append((handler, stream))
                    handler.stream = originals[id(stream)]

    def _canary(self) -> None:
        """Prove the probe hooks see this interpreter's stat calls, or record nothing."""
        canary = os.path.join(os.getcwd(), f".apm-fast-path-canary-{os.getpid()}")
        seen = []
        for probe in (
            os.path.exists,
            os.path.isdir,
            os.path.lexists,
            os.path.realpath,
            lambda p: Path(p).exists(),
            lambda p: Path(p).is_file(),
            lambda p: Path(p).resolve(),
        ):
            self._forget(canary)
            probe(canary)
            seen.append(
                canary in self.recorder.probed
                or any(key.endswith("|" + canary) for key in self.recorder.resolved)
            )
        self._forget(canary)
        if not all(seen):
            self.recorder.violate("instrumentation-gap")

    def changed_input(self) -> str:
        recorder = self.recorder
        for path, seen in recorder.observed.items():
            if _snapshot(path) != seen:
                return path
        for path, entries in recorder.listings.items():
            if _listing(path) != entries:
                return f"listing {path}"
        for key, seen in recorder.resolved.items():
            if _resolution(key) != seen:
                return key
        return "?"

    def inputs_held(self) -> bool:
        """Every input still looks as it did when the run first observed it."""
        recorder = self.recorder
        if any(_snapshot(path) != seen for path, seen in recorder.observed.items()):
            return False
        if any(_listing(path) != entries for path, entries in recorder.listings.items()):
            return False
        return all(_resolution(key) == seen for key, seen in recorder.resolved.items())

    def _forget(self, path: str) -> None:
        self.recorder.probed.discard(path)
        for key in (f"0|{path}", f"1|{path}"):
            self.recorder.resolved.pop(key, None)
        self.recorder.observed.pop(path, None)

    def __exit__(self, *exc: object) -> None:
        global _ACTIVE
        _ACTIVE = None
        os.path.realpath = self._saved["realpath"]
        os.stat = self._saved["stat"]
        os.lstat = self._saved["lstat"]
        os.readlink = self._saved["readlink"]
        os.environ.__class__ = self._saved["environ_class"]
        sys.stdout = self._saved["stdout"]
        sys.stderr = self._saved["stderr"]
        for handler, stream in getattr(self, "_log_streams", ()):
            handler.stream = stream


def _interpreter_roots() -> set[str]:
    """Where the interpreter and installed distributions live (not whole prefixes,
    which may also hold project files or local dependencies)."""
    import sysconfig

    roots: set[str] = set()
    for scheme_vars in (None, {"base": sys.base_prefix, "platbase": sys.base_exec_prefix}):
        paths = sysconfig.get_paths(vars=scheme_vars) if scheme_vars else sysconfig.get_paths()
        for name in ("stdlib", "platstdlib", "purelib", "platlib"):
            if paths.get(name):
                roots.add(os.path.abspath(paths[name]))
    if getattr(sys, "frozen", False):
        roots.add(os.path.abspath(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))))
    return roots


def _protected_roots(project_root: Path) -> set[str]:
    """Never scratch, even inside the temp dir: the project and every user-state root."""
    from apm_cli.cache.paths import get_cache_root
    from apm_cli.core.scope import get_apm_home

    return {
        str(project_root),
        os.path.expanduser("~"),
        str(get_apm_home()),
        str(get_cache_root(ensure=False)),
    }


def _with_realpaths(roots: set[str]) -> tuple[str, ...]:
    paths = {os.path.abspath(root) for root in roots}
    paths.update(os.path.realpath(root) for root in list(paths))
    return tuple(sorted(paths))


def _cache_roots() -> tuple[set[str], set[str]]:
    """(expiring cache roots, content-addressed roots inside them)."""
    from apm_cli.cache.paths import GIT_CHECKOUTS_BUCKET, GIT_DB_BUCKET, get_cache_root
    from apm_cli.core.scope import get_apm_home

    root = str(get_cache_root(ensure=False))
    expiring = {root, str(get_apm_home() / "cache")}
    immutable = {os.path.join(root, GIT_DB_BUCKET), os.path.join(root, GIT_CHECKOUTS_BUCKET)}
    return expiring, immutable


def _policy_cache_roots() -> set[str]:
    """Policy caches are re-evaluated live on replay, so their bytes are not inputs."""
    try:
        from apm_cli.cache.paths import get_cache_root

        root = get_cache_root(ensure=False)
    except Exception:
        return set()
    return {os.path.abspath(os.path.join(root, "policy_v1"))}
