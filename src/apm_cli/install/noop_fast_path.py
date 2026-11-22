"""No-op install fast path: replay a verified no-op instead of re-running it.

``apm install`` on an unchanged project runs the whole pipeline and then
reports ``No changes``. This module memoizes that answer the way a build
system memoizes a step: it records every input one full no-op run observed,
and a later run replays the recorded output only when each input is still
bit-identical. Anything unknown or different falls through to the full
pipeline; the fast path never skips a check, it proves the check would see
the same bytes.

Recording (a full run, on every eligible invocation that is not a hit):

- every file opened for reading (``open`` audit event) -> SHA-256 of content;
- every directory listed (``os.listdir`` / ``os.scandir``) -> its entries,
  each with type, size and mtime;
- every path probed with ``os.stat`` / ``os.lstat`` / ``os.readlink`` ->
  type, size, mtime and link target;
- every environment variable read -> folded into one digest;
- the effective org policy -> fingerprint (re-discovered on every run);
- the output the command printed.

A stamp is written only when the recorded run was itself a no-op: it wrote
nothing, made no network call, ran no subprocess other than the policy
discovery ``git remote``, never prompted, exited 0 and printed the
``No changes`` summary. Because that run wrote nothing, its inputs are also
its outputs; a run whose inputs are byte-identical repeats it exactly.

Replay (a hit) additionally re-runs, every time: org policy discovery with
its unchanged cache-freshness rules, and the lockfile's deployed-file
content-hash verification (``verify_deployed_file_hashes``). It writes
nothing. Off by default: ``APM_INSTALL_FAST_PATH=1`` turns it on.

Trust boundary: stamps live in the user's cache root, like the cached
package contents themselves; a same-user process that can write the cache
can already alter what an install deploys.
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SCHEMA = 1
_DISABLE_ENV = "APM_INSTALL_FAST_PATH"
_STAMP_BUCKET = "noop_v1"
_SUMMARY = re.compile(r"No changes -- install state already up to date in \d+\.\ds\.")
_SUMMARY_PLACEHOLDER = "\x00APM_ELAPSED\x00"
_SUMMARY_TEMPLATE = "No changes -- install state already up to date in {elapsed:.1f}s."
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


# ---------------------------------------------------------------------------
# Eligibility and keys
# ---------------------------------------------------------------------------


def _disabled() -> bool:
    if sys.platform == "win32":
        # os.path.exists and friends are C functions on Windows: the probe
        # recorder cannot observe them, so the fast path never engages.
        return True
    return os.environ.get(_DISABLE_ENV, "").strip().lower() not in _ENABLED


# Off by default until a clean replay is measured and signed (#4770):
# ``APM_INSTALL_FAST_PATH=1`` enables it; ``=trace`` enables it and reports each
# decision on stderr (tests, benchmarks).
_ENABLED = frozenset({"1", "true", "yes", "on", "trace"})


def eligible(options: dict[str, Any]) -> bool:
    """Only a plain re-install of the manifest (optionally ``--frozen``) qualifies."""
    if _disabled():
        return False
    if options.get("packages") or options.get("mcp_name") is not None:
        return False
    if options.get("update") or options.get("refresh") or options.get("dry_run"):
        return False
    if options.get("no_policy") or _explicit_policy_in(sys.argv[1:]):
        # --no-policy must not trigger discovery; an explicit --policy source composes
        # with discovery in ways the org fingerprint does not capture.
        return False
    return not options.get("root") and not options.get("global_")


def _explicit_policy_in(argv: list[str]) -> bool:
    return any(arg == "--policy" or arg.startswith("--policy=") for arg in argv)


def _code_identity() -> dict[str, Any]:
    from apm_cli import __version__

    package_dir = Path(__file__).resolve().parents[1]
    hasher = hashlib.sha256()
    if getattr(sys, "frozen", False):
        with open(sys.executable, "rb") as handle:
            hasher.update(hashlib.file_digest(handle, "sha256").digest())
    else:
        for dirpath, dirnames, filenames in os.walk(package_dir):
            dirnames.sort()
            for name in sorted(filenames):
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    hasher.update(os.path.relpath(path, package_dir).encode() + b"\x00")
                    with open(path, "rb") as handle:
                        hasher.update(handle.read())
                    hasher.update(b"\x00")
    return {
        "version": __version__,
        "package": str(package_dir),
        "code_sha256": hasher.hexdigest(),
        "python": sys.version,
        "executable": sys.executable,
    }


def _invocation_key(argv: list[str]) -> dict[str, Any]:
    tty = []
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            tty.append(bool(stream.isatty()))
        except (AttributeError, ValueError):
            tty.append(False)
    try:
        columns = os.get_terminal_size().columns if tty[1] else None
    except OSError:
        columns = None
    return {
        "schema": _SCHEMA,
        "argv": list(argv),
        "cwd": os.getcwd(),
        "tty": tty,
        "columns": columns,
        "uid": os.getuid() if hasattr(os, "getuid") else None,
    }


def _stamp_path(key: dict[str, Any]) -> Path:
    from apm_cli.cache.paths import get_cache_root

    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return get_cache_root(ensure=False) / _STAMP_BUCKET / f"{digest}.json"


# ---------------------------------------------------------------------------
# Fingerprints of the observed inputs
# ---------------------------------------------------------------------------


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
    """Every stat field a trust or ownership check may read (not atime)."""
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


def policy_fingerprint(project_root: Path) -> str | None:
    """Re-discover and route the org policy live; ``None`` refuses the fast path.

    Discovery keeps its unchanged cache-freshness rules, and the routed
    decision comes from the canonical owner (``policy/outcome_routing.py``).
    A blocking or unresolved route, a stale cache, a refresh failure or a
    warning always takes the full pipeline, which reports it.
    """
    from apm_cli.policy.discovery import policy_disabled_by_env
    from apm_cli.policy.outcome_routing import classify_project_policy, discover_trust_policy

    if policy_disabled_by_env():
        return "disabled-by-env"
    try:
        result = discover_trust_policy(project_root)
        route = classify_project_policy(project_root, fetch_result=result)
    except Exception:
        return None
    if route.blocks or route.unresolved:
        return None
    if result.cache_stale or result.fetch_error or result.warnings:
        return None
    policy_bytes = b""
    if route.policy is not None:
        try:
            from dataclasses import asdict, is_dataclass

            data = asdict(route.policy) if is_dataclass(route.policy) else repr(route.policy)
            policy_bytes = json.dumps(data, sort_keys=True, default=repr).encode()
        except Exception:
            return None
    digest = hashlib.sha256(policy_bytes).hexdigest()
    return f"{route.outcome}|{route.kind}|{result.source}|{digest}"


def deployed_hashes_intact(project_root: Path) -> bool:
    """Every content hash the lockfile records still matches the deployed bytes."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.policy.ci_checks import verify_deployed_file_hashes

    lock = LockFile.read(get_lockfile_path(project_root))
    if lock is None:
        return False
    mismatches, unresolved = verify_deployed_file_hashes(project_root, lock)
    return not mismatches and not unresolved


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
        elif event == "subprocess.Popen":
            argv = args[1] if len(args) > 1 else None
            if not (_allowed_subprocess(argv) or _within_policy_discovery()):
                self.violate(f"subprocess:{argv!r}"[:200])
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
        if any(isinstance(arg, int) and not isinstance(arg, bool) for arg in args[1:]):
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


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    recorder = _ACTIVE
    if recorder is not None:
        recorder.on_event(event, args)


def _probing(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def probe(path: Any, *args: Any, **kwargs: Any) -> Any:
        recorder = _ACTIVE
        if recorder is not None and not kwargs.get("dir_fd"):
            recorder.on_probe(path)
        return original(path, *args, **kwargs)

    return probe


def _resolving(original: Callable[..., Any]) -> Callable[..., Any]:
    """Record ``os.path.realpath`` answers: its internal lstat calls may be unobservable."""

    @functools.wraps(original)
    def resolve(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(path, *args, **kwargs)
        recorder = _ACTIVE
        if recorder is not None:
            strict = bool(kwargs.get("strict", args[0] if args else False))
            recorder.on_resolve(path, strict, result)
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
        excluded = {os.path.abspath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
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
        self._saved = {
            "stat": os.stat,
            "lstat": os.lstat,
            "readlink": os.readlink,
            "realpath": os.path.realpath,
            "environ_class": os.environ.__class__,
            "environ_start": dict(os.environ),
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
        _ACTIVE = self.recorder
        self._canary()
        return self

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

    def _changed_input(self) -> str:
        recorder = self.recorder
        for path, seen in recorder.observed.items():
            if _snapshot(path) != seen:
                return path
        for key, seen in recorder.resolved.items():
            if _resolution(key) != seen:
                return key
        return "?"

    def _inputs_held(self) -> bool:
        """Every input still looks as it did when the run first observed it."""
        recorder = self.recorder
        if any(_snapshot(path) != seen for path, seen in recorder.observed.items()):
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

    def save(self, project_root: Path) -> bool:
        """Persist the stamp when the recorded run qualifies; return whether it did."""
        recorder = self.recorder
        if recorder.violations:
            return False
        text = "".join(chunk for _, chunk in self.output)
        if len(_SUMMARY.findall(text)) != 1:
            recorder.violations.append(f"summary seen {len(_SUMMARY.findall(text))} times")
            return False
        if not self._inputs_held():
            recorder.violations.append(f"input changed during the run: {self._changed_input()}")
            return False
        if self.policy is None:
            # This run was a no-op but carried no live policy view: mark the
            # invocation pending so the next one records with it.
            return self._write({"key": self.key, "pending": True})
        if policy_fingerprint(project_root) != self.policy:
            recorder.violations.append("org policy changed during the run")
            return False
        output = [
            [channel, _SUMMARY.sub(_SUMMARY_PLACEHOLDER, chunk)] for channel, chunk in self.output
        ]
        stamp = {
            "key": self.key,
            "code": _code_identity(),
            "policy": self.policy,
            "env": _env_digest(
                None if recorder.env_all else sorted(recorder.env), self._saved["environ_start"]
            ),
            "env_names": None if recorder.env_all else sorted(recorder.env),
            "reads": {path: _file_digest(path) for path in sorted(recorder.reads)},
            "listed": {path: _listing(path) for path in sorted(recorder.listed)},
            "probed": {path: _probe(path) for path in sorted(recorder.probed)},
            "resolved": {key: _resolution(key) for key in sorted(recorder.resolved)},
            "output": output,
        }
        if not self._inputs_held():
            recorder.violations.append(f"input changed while stamping: {self._changed_input()}")
            return False
        return self._write(stamp)

    def _write(self, stamp: dict[str, Any]) -> bool:
        try:
            self.stamp.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.stamp.parent, prefix=".stamp-")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(stamp, handle)
            os.replace(tmp, self.stamp)
        except OSError:
            return False
        return True


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


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _first_changed(recorded: dict[str, Any], current: Callable[[str], Any]) -> str | None:
    for path, value in recorded.items():
        if current(path) != value:
            return path
    return None


def _stamp_mismatch(
    stamp: dict[str, Any],
    key: dict[str, Any],
    policy: Callable[[], str | None],
    project_root: Path,
) -> str | None:
    """Return why *stamp* does not prove this run a no-op, or ``None`` when it does.

    Checks run cheapest first; each is a callable so the first failure stops.
    """
    checks: list[tuple[str, Callable[[], object]]] = [
        ("invocation", lambda: stamp.get("key") != key),
        ("environment", lambda: stamp.get("env") != _env_digest(stamp.get("env_names"))),
        ("content", lambda: _first_changed(stamp.get("reads", {}), _file_digest)),
        ("listing", lambda: _first_changed(stamp.get("listed", {}), _listing)),
        ("path", lambda: _first_changed(stamp.get("probed", {}), _probe)),
        ("resolution", lambda: _first_changed(stamp.get("resolved", {}), _resolution)),
        ("apm version", lambda: stamp.get("code") != _code_identity()),
        ("org policy", lambda: (lambda now: now is None or now != stamp.get("policy"))(policy())),
        ("deployed file hash", lambda: not deployed_hashes_intact(project_root)),
    ]
    for reason, changed in checks:
        found = changed()
        if found:
            return reason if found is True else f"{reason} {found}"
    return None


def _replay(stamp: dict[str, Any], started: float) -> None:
    elapsed = time.perf_counter() - started
    summary = _SUMMARY_TEMPLATE.format(elapsed=elapsed)
    for channel, chunk in stamp["output"]:
        stream = sys.stdout if channel == 1 else sys.stderr
        stream.write(chunk.replace(_SUMMARY_PLACEHOLDER, summary))
    sys.stdout.flush()
    sys.stderr.flush()


_STAMP_MAPS = ("reads", "listed", "probed", "resolved")


def _load(path: Path) -> dict[str, Any] | None:
    """Read a stamp; anything malformed is no stamp (the full pipeline runs)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("key"), dict):
        return None
    if data.get("pending") is True:
        return data
    if not all(isinstance(data.get(name), dict) for name in _STAMP_MAPS):
        return None
    if not isinstance(data.get("policy"), str) or not isinstance(data.get("env"), str):
        return None
    output = data.get("output")
    if not isinstance(output, list) or not all(
        isinstance(row, list) and len(row) == 2 and row[0] in (1, 2) and isinstance(row[1], str)
        for row in output
    ):
        return None
    return data


def run_with_fast_path(
    options: dict[str, Any], run: Callable[[], Any], project_root: Path | None = None
) -> Any:
    """Replay a verified no-op, or run *run* fully while recording its inputs."""
    started = time.perf_counter()
    if not eligible(options):
        return run()
    root = project_root or Path.cwd()
    try:
        key = _invocation_key(sys.argv[1:])
        stamp_path = _stamp_path(key)
    except Exception:
        return run()
    if not (root / "apm.lock.yaml").is_file():
        return run()  # a first install is never a no-op
    trace = os.environ.get(_DISABLE_ENV, "").strip().lower() == "trace"
    stamp = _load(stamp_path)
    policy: str | None = None
    if stamp is None:
        reason = "no stamp"
    elif stamp.get("pending"):
        # The previous run was a no-op: record this one with the live policy view.
        reason = "pending"
        policy = policy_fingerprint(root)
    else:
        try:
            reason = _stamp_mismatch(stamp, key, lambda: policy_fingerprint(root), root)
        except Exception as error:
            reason = f"error {type(error).__name__}"
        if reason is None:
            _replay(stamp, started)
            if trace:
                sys.stderr.write("[i] install fast path: replayed a verified no-op\n")
            return None
    # A miss runs no live discovery of its own (only a pending invocation does):
    # cold, warm and changed installs pay only the recorder.
    if trace:
        sys.stderr.write(f"[i] install fast path: full run ({reason})\n")
    return _run_recorded(run, _Recording(key, stamp_path, policy, started, root), root, trace)


def _run_recorded(run: Callable[[], Any], recording: _Recording, root: Path, trace: bool) -> Any:
    try:
        with recording:
            result = run()
    except BaseException as error:
        # ``apm install`` ends with ``ctx.exit(code)``: only a clean exit 0 records.
        if _exit_code(error) == 0:
            _finish(recording, root, trace)
        raise
    _finish(recording, root, trace)
    return result


def _exit_code(error: BaseException) -> int | None:
    from click.exceptions import Exit

    if isinstance(error, Exit):
        return error.exit_code
    if isinstance(error, SystemExit):
        return 0 if error.code in (None, 0) else 1
    return None


def _finish(recording: _Recording, root: Path, trace: bool) -> None:
    """Save the stamp; a recording failure never changes the install's own outcome."""
    try:
        saved = recording.save(root)
    except Exception as error:
        recording.recorder.violations.append(f"save failed: {type(error).__name__}")
        saved = False
    if trace:
        detail = "recorded" if saved else f"not recorded {recording.recorder.violations[:8]}"
        sys.stderr.write(f"[i] install fast path: {detail}\n")


def fast_path(command: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate the ``install`` click callback with the no-op fast path."""

    @functools.wraps(command)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return run_with_fast_path(kwargs, lambda: command(*args, **kwargs))

    return wrapped
