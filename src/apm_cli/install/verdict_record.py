"""Verified-state record: a no-op ``apm install`` replays the last verified no-op (#4977).

The install engine proves a no-op by re-running every check. That verdict is a
pure function of its inputs, so the run that proved it records an HMAC over
every verdict input, read live, plus its exact output. A later ``apm install``
in the same checkout recomputes the HMAC from live inputs, re-hashes every
deployed file and package copy, and replays the recorded output only on an
exact match. Anything else (a missing, foreign or unreadable record or key, a
changed input, CI) runs today's engine, which re-judges and rewrites the record.

The record authorizes nothing by itself: the key is a per-user secret outside
the project and cache (a cloned repo or a restored cache cannot ship a valid
record), and every input it summarizes is re-read on every hit.

Imports stay light on purpose: the hit runs before the CLI is imported.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

RECORD_VERSION = 1
RECORDS_DIR = "verdicts"
KEY_FILE = "verdict.key"
# Argv the record serves; anything else runs the CLI as usual.
SERVED_ARGV = (("install",), ("install", "--frozen"))

# Environment that can change an install verdict or its output. A hit keys on
# exactly these (their values, or their absence); the static guard in
# tests/unit/install/test_verdict_record.py fails when ``src/apm_cli`` reads an
# environment variable that is neither here nor in ENV_NOT_VERDICT.
ENV_ALLOWLIST = (
    "ADO_APM_PAT",
    "ADO_HOST",
    "APM_ADO_HOSTS",
    "APM_BROAD_FETCH_DEPTH",
    "APM_CACHE_DIR",
    "APM_COPILOT_APP_DB",
    "APM_COPILOT_COWORK_SKILLS_DIR",
    "APM_DEBUG",
    "APM_GITLAB_HOSTS",
    "APM_GITLAB_POLICY_REPO",
    "APM_GIT_CREDENTIAL_TIMEOUT",
    "APM_HOME",
    "APM_LEGACY_SKILL_PATHS",
    "APM_LOG_LEVEL",
    "APM_MCP_REGISTRY_SOURCE",
    "APM_NON_INTERACTIVE",
    "APM_NO_CACHE",
    "APM_NO_REFLINK",
    "APM_NO_SCRIPTS",
    "APM_POLICY_DISABLE",
    "APM_PROGRESS",
    "APM_REPO",
    "APM_RESOLVE_PARALLEL",
    "APM_TEMP_DIR",
    "APM_TIERED_RESOLVER",
    "APM_VERBOSE",
    "ARTIFACTORY_APM_TOKEN",
    "ARTIFACTORY_BASE_URL",
    "ARTIFACTORY_MAX_ARCHIVE_MB",
    "ARTIFACTORY_ONLY",
    "CI",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "COLUMNS",
    "FORCE_COLOR",
    "GH_HOST",
    "GH_TOKEN",
    "GITHUB_ACTIONS",
    "GITHUB_APM_PAT",
    "GITHUB_HOST",
    "GITHUB_TOKEN",
    "GITHUB_URL",
    "GITLAB_HOST",
    "GIT_SSH_COMMAND",
    "LOCALAPPDATA",
    "MCP_REGISTRY_ALLOW_HTTP",
    "MCP_REGISTRY_URL",
    "NO_COLOR",
    "PATHEXT",
    "PROGRAMDATA",
    "PROXY_REGISTRY_ALLOW_HTTP",
    "PROXY_REGISTRY_ONLY",
    "PROXY_REGISTRY_TOKEN",
    "PROXY_REGISTRY_URL",
    "PYTHONIOENCODING",
    "SOURCE_DATE_EPOCH",
    "TERM",
    "USER",
    "VERSION",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)
# Read by the CLI, but never part of a verdict or its output: test seams and the
# fast-path trace switch (its line goes to stderr and is not recorded).
ENV_NOT_VERDICT = (
    "APM_E2E_TESTS",
    "APM_INSTALL_FAST_PATH",
    "APM_TEST_FAIL_LOCK_REPLACE",
    "APM_TEST_LOCK_COMMIT",
)
# A run with any of these set never hits (and never records): the full
# pipeline owns source routing through a registry proxy, and CI commonly
# restores APM_HOME wholesale (QL D2).
_NO_HIT_ENV = (
    "CI",
    "GITHUB_ACTIONS",
    "PROXY_REGISTRY_URL",
    "PROXY_REGISTRY_ONLY",
    "ARTIFACTORY_BASE_URL",
    "ARTIFACTORY_ONLY",
    "APM_NO_CACHE",
    "APM_POLICY_DISABLE",
)

# Target roots whose listing is a verdict input (unmanaged files, D3).
_GOVERNED_DIRS = (
    ".github",
    ".claude",
    ".cursor",
    ".agents",
    ".codex",
    ".gemini",
    ".opencode",
    ".windsurf",
    ".kiro",
    ".vscode",
)
# Merged surfaces APM writes entries into (D4), project then user.
_PROJECT_SURFACES = (
    ".mcp.json",
    ".vscode/mcp.json",
    ".cursor/mcp.json",
    ".claude/settings.json",
    ".codex-native/ownership.json",
    ".apm-local/installation",
    ".git/config",
    ".gitignore",
    "apm.yml",
)
_USER_SURFACES = (".claude.json", ".codex/config.toml")

_VERIFIED_NOOP: list[bool] = []
# O1: the summary's elapsed seconds are templated; any other clock-dependent
# token (a cache age, a countdown) means the output is not recorded at all.
_ELAPSED = "\x00elapsed\x00"
_ELAPSED_RE = re.compile(r"(up to date in )\d+\.\d(s\.)")
_CLOCK_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:ms|s|m|h|d|min|sec|seconds|minutes|hours|days) (?:ago|left|remaining)\b|expires in"
)


def _env(name: str) -> str | None:
    """The one environment read of this module; only classified names."""
    if name not in ENV_ALLOWLIST and name not in ENV_NOT_VERDICT:
        raise KeyError(name)
    return os.environ.get(name)


def note_verified_noop() -> None:
    """The install engine proved this run a no-op: its output may be recorded."""
    _VERIFIED_NOOP.append(True)


def _served(argv: list[str]) -> bool:
    return tuple(argv) in SERVED_ARGV


def _hits_allowed() -> bool:
    return not any((_env(name) or "").strip() for name in _NO_HIT_ENV)


def _apm_home() -> Path:
    from apm_cli.core.scope import get_apm_home

    return get_apm_home()


def _cache_root() -> Path | None:
    from apm_cli.cache.paths import get_cache_root

    try:
        return get_cache_root(ensure=False)
    except (OSError, ValueError):
        return None


def _record_path(root: Path) -> Path:
    key = hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:40]
    return _apm_home() / RECORDS_DIR / f"{key}.json"


def _read_nofollow(path: Path) -> bytes | None:
    """Bytes of a regular file opened without following a final symlink; None if absent."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _key(*, create: bool) -> bytes | None:
    from apm_cli.install import verdict_key

    return verdict_key.load(_apm_home() / KEY_FILE, create=create)


class _Digest:
    """Length-prefixed HMAC over tagged inputs (QL C7)."""

    def __init__(self, key: bytes) -> None:
        self._mac = hmac.new(key, digestmod=hashlib.sha256)
        self.tags: list[tuple[str, str]] = []

    def put(self, tag: str, data: bytes) -> None:
        encoded = tag.encode("utf-8", "surrogateescape")
        self._mac.update(len(encoded).to_bytes(4, "little") + encoded)
        self._mac.update(len(data).to_bytes(8, "little") + data)
        self.tags.append((tag, hashlib.sha256(data).hexdigest()[:16]))

    def file(self, tag: str, path: Path) -> None:
        data = _read_nofollow(path)
        self.put(tag, b"\x00absent" if data is None else b"\x01" + data)

    def hexdigest(self) -> str:
        return self._mac.hexdigest()


def _walk_files(base: Path, skip: tuple[str, ...] = ()) -> Iterator[Path]:
    for directory, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            yield Path(directory) / name


def _inputs(digest: _Digest, root: Path, argv: list[str], lock_text: bytes) -> None:
    """Every verdict input, read live (QL D3-D6, O3)."""
    import locale

    digest.put("version", str(RECORD_VERSION).encode())
    digest.put("build", str(getattr(sys.modules.get("apm_cli"), "__version__", "")).encode())
    digest.put("argv", "\x00".join(argv).encode())
    digest.put("root", os.path.realpath(root).encode("utf-8", "surrogateescape"))
    digest.put("platform", f"{sys.platform}|{os.sep}|{sys.version_info[:2]}".encode())
    digest.put(
        "presentation",
        "|".join(
            (
                str(sys.stdout.isatty()),
                str(sys.stderr.isatty()),
                locale.getpreferredencoding(False),
                str(sys.stdout.encoding),
            )
        ).encode(),
    )
    for name in ENV_ALLOWLIST:
        value = _env(name)
        digest.put(f"env:{name}", b"\x00unset" if value is None else b"\x01" + value.encode())
    digest.put("lock", lock_text)
    for rel in _PROJECT_SURFACES:
        digest.file(f"project:{rel}", root / rel)
    user = Path(os.path.expanduser("~"))
    for rel in _USER_SURFACES:
        digest.file(f"user:{rel}", user / rel)
    home = _apm_home()
    for path in _walk_files(home, skip=(RECORDS_DIR, "cache")):
        if path.name != KEY_FILE:
            digest.file(f"home:{path.relative_to(home).as_posix()}", path)
    cache = _cache_root()
    if cache is not None:
        policy = cache / "policy_v1"
        for path in _walk_files(policy):
            digest.put(
                f"policy-mtime:{path.relative_to(policy).as_posix()}",
                b"%d" % os.stat(path).st_mtime_ns,
            )
            digest.file(f"policy:{path.relative_to(policy).as_posix()}", path)
    for name in _GOVERNED_DIRS:
        base = root / name
        digest.put(f"target:{name}", b"%d" % base.is_dir())
        for directory, dirnames, filenames in os.walk(base):
            dirnames.sort()
            listing = [*sorted(filenames), "/", *dirnames]
            rel = Path(directory).relative_to(root).as_posix()
            digest.put(f"listing:{rel}", "\x00".join(listing).encode("utf-8", "surrogateescape"))


def _deployed_intact(root: Path, lock: dict) -> bool:
    """Every deployed file and package copy, re-hashed no-follow (QL D5)."""
    for dep in lock.get("dependencies") or []:
        if not isinstance(dep, dict):
            return False
        for rel, expected in (dep.get("deployed_file_hashes") or {}).items():
            data = _read_nofollow(root / rel)
            if data is None:
                return False
            if "sha256:" + hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() != expected:
                return False
        repo = str(dep.get("repo_url", ""))
        if not repo or ".." in repo.split("/"):
            return False
        package = root / "apm_modules" / Path(*repo.split("/"))
        if package.is_symlink():
            return False
        for path in _walk_files(package, skip=(".git",)):
            if _read_nofollow(path) is None:
                return False
    return True


def _trace(message: str) -> None:
    if (_env("APM_INSTALL_FAST_PATH") or "").strip().lower() in {"trace", "decide"}:
        sys.stderr.write(f"[i] verdict record: {message}\n")


def replay(argv: list[str]) -> bool:
    """Print the recorded no-op and return True, or return False to run the CLI."""
    started = time.perf_counter()
    if not _served(argv) or not _hits_allowed():
        return False
    root = Path.cwd()
    try:
        record_path = _record_path(root)
        raw = _read_nofollow(record_path)
        if raw is None:
            return False
        key = _key(create=False)
        if key is None:
            _trace("miss (no key)")
            return False
        record = json.loads(raw.decode("utf-8"))
        lock_text = _read_nofollow(root / "apm.lock.yaml")
        if lock_text is None or record.get("v") != RECORD_VERSION:
            return False
        digest = _Digest(key)
        _inputs(digest, root, argv, lock_text)
        if not hmac.compare_digest(digest.hexdigest(), str(record.get("mac", ""))):
            old = dict(record.get("tags") or [])
            changed = sorted(t for t, h in digest.tags if old.get(t) != h)
            _trace(f"miss (inputs changed: {', '.join(changed[:6])})")
            return False
        import yaml

        lock = yaml.load(lock_text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))  # noqa: S506
        if not isinstance(lock, dict) or not _deployed_intact(root, lock):
            _trace("miss (deployed bytes)")
            return False
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        _trace(f"miss ({type(exc).__name__})")
        return False
    if int(record.get("exit", 1)) != 0:
        return False
    elapsed = f"{time.perf_counter() - started:.1f}"
    sys.stdout.write(str(record.get("stdout", "")).replace(_ELAPSED, elapsed))
    sys.stderr.write(str(record.get("stderr", "")))
    sys.stdout.flush()
    _trace("hit")
    return True


class _Tee:
    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        self.parts.append(text)
        return self.stream.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


@contextmanager
def recording(argv: list[str]) -> Iterator[None]:
    """Run the CLI; after a verified no-op with exit 0, record its inputs and output."""
    if not _served(argv) or not _hits_allowed():
        yield
        return
    _VERIFIED_NOOP.clear()
    out, err = _Tee(sys.stdout), _Tee(sys.stderr)
    sys.stdout, sys.stderr = out, err  # type: ignore[assignment]
    code: object = 1
    try:
        yield
        code = 0
    except SystemExit as exc:
        code = 0 if exc.code is None else exc.code
        raise
    finally:
        sys.stdout, sys.stderr = out.stream, err.stream
        if code == 0 and _VERIFIED_NOOP:
            _write(argv, "".join(out.parts), "".join(err.parts))


def _write(argv: list[str], stdout_text: str, stderr_text: str) -> None:
    root = Path.cwd()
    try:
        key = _key(create=True)
        lock_text = _read_nofollow(root / "apm.lock.yaml")
        if key is None or lock_text is None:
            return
        digest = _Digest(key)
        _inputs(digest, root, argv, lock_text)
        stdout_text, stamps = _ELAPSED_RE.subn(rf"\g<1>{_ELAPSED}\g<2>", stdout_text)
        if stamps != 1 or _CLOCK_RE.search(stdout_text) or _CLOCK_RE.search(stderr_text):
            return  # O1: only the elapsed seconds may depend on the clock
        stderr_kept = "".join(
            line
            for line in stderr_text.splitlines(keepends=True)
            if "install fast path:" not in line
        )
        record = {
            "v": RECORD_VERSION,
            "mac": digest.hexdigest(),
            "tags": digest.tags,
            "stdout": stdout_text,
            "stderr": stderr_kept,
            "exit": 0,
        }
        path = _record_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        return
