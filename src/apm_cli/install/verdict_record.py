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

RECORD_VERSION = 3
# A record's provenance (W3d A5): "proven" when the engine proved the recorded run a
# no-op; "attested" when a successful full install committed the state it keys, or
# when a run replayed an attested record. Provenance never upgrades.
PROVEN, ATTESTED = "proven", "attested"
RECORDS_DIR = "verdicts"
KEY_FILE = "verdict.key"
# Argv the record serves; anything else runs the CLI as usual.
SERVED_ARGV = (("install",), ("install", "--frozen"))

# Environment that can change an install verdict or its output. A hit keys on these (their
# values, or their absence) plus every name the recorded run read (_env_log); the static guard
# in tests/integration/test_verdict_record.py fails when ``src/apm_cli`` reads an environment
# variable that is neither here nor in ENV_NOT_VERDICT. Owner-read roots (the APM home, harness
# user roots) are named only by their owners and keyed through the run's own reads.
ENV_ALLOWLIST = (
    "ADO_APM_PAT",
    "ADO_HOST",
    "APM_ADO_HOSTS",
    "APM_BROAD_FETCH_DEPTH",
    "APM_CACHE_DIR",
    "APM_COPILOT_APP_DB",
    "APM_COPILOT_COWORK_SKILLS_DIR",
    "APM_DEBUG",
    "APM_E2E_TESTS",
    "APM_GITLAB_HOSTS",
    "APM_GITLAB_POLICY_REPO",
    "APM_GIT_CREDENTIAL_TIMEOUT",
    "APM_LEGACY_SKILL_PATHS",
    "APM_LOG_LEVEL",
    "APM_MCP_REGISTRY_SOURCE",
    "APM_NON_INTERACTIVE",
    "APM_NO_CACHE",
    "APM_NO_INPROCESS_SPARSE",
    "APM_NO_REFLINK",
    "APM_NO_SCRIPTS",
    "APM_PROGRESS",
    "APM_REPO",
    "APM_RESOLVE_PARALLEL",
    "APM_TEMP_DIR",
    "APM_TIERED_RESOLVER",
    "APM_VERBOSE",
    "APPDATA",
    "ARTIFACTORY_APM_TOKEN",
    "ARTIFACTORY_BASE_URL",
    "ARTIFACTORY_MAX_ARCHIVE_MB",
    "ARTIFACTORY_ONLY",
    "CI",
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
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "MCP_REGISTRY_ALLOW_HTTP",
    "MCP_REGISTRY_URL",
    "NO_COLOR",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROXY_REGISTRY_ONLY",
    "PROXY_REGISTRY_TOKEN",
    "PROXY_REGISTRY_URL",
    "PYTHONIOENCODING",
    "SOURCE_DATE_EPOCH",
    "SSL_CERT_DIR",
    "TERM",
    "USER",
    "USERPROFILE",
    "VERSION",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
# Read by the CLI, but never part of a verdict or its output: test seams and the
# fast-path trace switch (its line goes to stderr and is not recorded).
ENV_NOT_VERDICT = (
    "APM_INSTALL_FAST_PATH",
    "APM_TEST_FAIL_LOCK_REPLACE",
    "APM_TEST_LOCK_COMMIT",
    # CI detection for the record only: CI never hits or records (QL D2).
    "TF_BUILD",
    "JENKINS_URL",
    "BUILD_ID",
    "TEAMCITY_VERSION",
    "CODEBUILD_BUILD_ID",
    "GITLAB_CI",
    "BUILDKITE",
    "CIRCLECI",
    "BITBUCKET_BUILD_NUMBER",
    "APPVEYOR",
    "DRONE",
)
# A run with any of these set never hits (and never records): the full
# pipeline owns source routing through a registry proxy, and CI commonly
# restores APM_HOME wholesale (QL D2).
_NO_HIT_ENV = (
    "CI",
    "GITHUB_ACTIONS",
    "TF_BUILD",
    "JENKINS_URL",
    "BUILD_ID",
    "TEAMCITY_VERSION",
    "CODEBUILD_BUILD_ID",
    "GITLAB_CI",
    "BUILDKITE",
    "CIRCLECI",
    "BITBUCKET_BUILD_NUMBER",
    "APPVEYOR",
    "DRONE",
    "PROXY_REGISTRY_URL",
    "PROXY_REGISTRY_ONLY",
    "ARTIFACTORY_BASE_URL",
    "ARTIFACTORY_ONLY",
    "APM_NO_CACHE",
    "SSL_CERT_DIR",  # a CA directory: its files are not keyed
)

# Project target roots: every file in them (listing and bytes) is a verdict input,
# which covers unmanaged files (D3) and the merged configs kept there (D4). The
# coverage cell in tests/integration/test_verdict_record.py fails when a target
# root (integration/targets.KNOWN_TARGETS) or an MCP adapter's config path is not
# covered here, in _PROJECT_SURFACES or in _USER_SURFACES.
_GOVERNED_DIRS = (
    ".agents",
    ".claude",
    ".codex",
    ".cursor",
    ".gemini",
    ".github",
    ".grok",
    ".kiro",
    ".opencode",
    ".vscode",
    ".windsurf",
    "agent-data",
)
# Merged surfaces APM writes entries into (D4), project then user.
_PROJECT_SURFACES = (
    ".mcp.json",
    ".vscode/mcp.json",
    ".cursor/mcp.json",
    ".claude/settings.json",
    ".codex-native/ownership.json",
    ".apm-local/installation",
    ".apm-local/.gitignore",
    ".git/config",
    ".gitignore",
    "apm.yml",
    "opencode.json",
)
_USER_SURFACES = (
    ".apm-local/installation",
    ".apm-local/.gitignore",
    ".claude.json",
    ".codex/config.toml",
    ".codeium/windsurf/mcp_config.json",
    ".hermes/config.yaml",
)

_VERIFIED_NOOP: list[str] = []
# A full install that committed a convergence-clean state, and the target line it
# announced (W3d A1, A2): written as an attested record, never as replayable output.
_ATTESTED_INSTALL: list[str] = []
# recording()'s entry state, which an attested check inside the CLI keys (O3).
_RUN_ENTRY: list[Any] = []
_RUN_TARGETS: list[str] = []
# Same values as install/noop_diff.disabled() (APM_INSTALL_FAST_PATH kill switch).
_KILL_VALUES = frozenset({"0", "false", "no", "off"})
# O1: the summary's elapsed seconds are templated; any other clock-dependent
# token (a cache age, a countdown) means the output is not recorded at all.
# W3d A2 (allowlist): a full install attests only when every warning-or-error
# line it printed is a notice the attested hit re-judges and prints live.
_WARNING_LINE = re.compile(r"^\s*\[[!x]\]")
_LIVE_NOTICES = (re.compile(r"^\s*\[!\] Parked MCP server '"),)
_ELAPSED = "\x00elapsed\x00"
_ELAPSED_RE = re.compile(r"(up to date in )\d+\.\d(s\.)")
_CLOCK_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:ms|s|m|h|d|min|sec|seconds|minutes|hours|days) (?:ago|left|remaining)\b|expires in"
)


# The recorded run's entry state. The CLI changes its own process as it runs
# (on Windows it sets PYTHONIOENCODING and reconfigures stdout to UTF-8), and a
# hit sees the state before the CLI runs, so a writer keys the entry state.
_ENTRY_ENV: dict[str, str] | None = None
_ENTRY_PRESENTATION: bytes | None = None


def _env(name: str, *, read_by_run: bool = False) -> str | None:
    """The one environment read of this module: classified names, or names the
    recorded run itself read (see :func:`_env_log`)."""
    if not read_by_run and name not in ENV_ALLOWLIST and name not in ENV_NOT_VERDICT:
        raise KeyError(name)
    if _ENTRY_ENV is not None:
        return _ENTRY_ENV.get(name.upper() if os.name == "nt" else name)
    return os.environ.get(name)


def _env_prefixed(prefix: str) -> list[tuple[str, str]]:
    """Every variable a child git process reads by its documented prefix (``GIT_``)."""
    source = os.environ if _ENTRY_ENV is None else _ENTRY_ENV
    return sorted((name, value) for name, value in source.items() if name.startswith(prefix))


def _env_snapshot() -> dict[str, str]:
    return dict(os.environ)


def _presentation() -> bytes:
    """O3: how this process presents output (ttys and encodings)."""
    import locale

    return "|".join(
        (
            str(sys.stdout.isatty()),
            str(sys.stderr.isatty()),
            locale.getpreferredencoding(False),
            str(sys.stdout.encoding),
        )
    ).encode()


_ENV_READS: set[str] = set()


@contextmanager
def _env_log() -> Iterator[None]:
    """Log each variable the recorded run reads by name (``get``, ``[]``, ``in``).

    The static allowlist cannot see names built at run time (an MCP server's
    ``${VAR}`` placeholders, an adapter's XDG root); the record keys every such
    name too, so a change in any value the run consulted misses.
    """
    original = os.environ
    base = type(original)
    read = base.__getitem__
    note = _ENV_READS.add
    # A run reads the environment tens of thousands of times: one call, no super().
    if os.name == "nt":

        class _Logged(base):  # type: ignore[misc,valid-type]
            def __getitem__(self, key: Any) -> Any:
                note(key.upper() if isinstance(key, str) else key)
                return read(self, key)

    else:

        class _Logged(base):  # type: ignore[misc,valid-type,no-redef]
            def __getitem__(self, key: Any) -> Any:
                note(key)
                return read(self, key)

    logged = _Logged(
        original._data,  # type: ignore[attr-defined]
        original.encodekey,  # type: ignore[attr-defined]
        original.decodekey,  # type: ignore[attr-defined]
        original.encodevalue,  # type: ignore[attr-defined]
        original.decodevalue,  # type: ignore[attr-defined]
    )
    # A logging view over the same mapping, never a different environment.
    os.environ = logged  # type: ignore[assignment]  # noqa: B003
    try:
        yield
    finally:
        if os.environ is logged:
            os.environ = original  # type: ignore[assignment]  # noqa: B003


_PROVEN_LOCK: list[bytes | None] = []
# apm.yml and the lock as the run found them (W3d R4), and its start (verdict_inputs).
_ENTRY_FILES: list[bytes | None] = []
_ENTRY_TIME: list[Any] = []


def _entry_bytes(name: str) -> bytes | None:
    try:
        return _read_nofollow(Path.cwd() / name)
    except OSError:
        return None


def _note_proven_lock() -> None:
    """The lock bytes the proving install held its lifecycle lock over."""
    try:
        _PROVEN_LOCK.append(_read_nofollow(Path.cwd() / "apm.lock.yaml"))
    except OSError:
        _PROVEN_LOCK.append(None)


def note_verified_noop(provenance: str = PROVEN) -> None:
    """The install engine proved this run a no-op (under its lifecycle lock), or
    matched an attested record (*provenance* ``attested``)."""
    _VERIFIED_NOOP.append(provenance)
    _note_proven_lock()


def _served(argv: list[str]) -> bool:
    return tuple(argv) in SERVED_ARGV


def _location_trusted(root: Path) -> bool:
    """The key and records live outside the project and the cache root (W3d B-c, QL D2)."""
    home = Path(os.path.realpath(_apm_home()))
    for base in (Path(os.path.realpath(root)), _cache_root()):
        if base is None:
            continue
        base = Path(os.path.realpath(base))
        if home == base or base in home.parents:
            return False
    return True


def _hits_allowed() -> bool:
    mode = _fast_path_mode()
    if mode in _KILL_VALUES or mode == "decide":
        return False  # the kill switch disables the record; deciding never replays
    if any((_env(name) or "").strip() for name in _NO_HIT_ENV):
        return False
    from apm_cli.core.experimental import is_enabled

    try:
        # The opt-in install-time audit scans every install (diff_apply declines too).
        return not is_enabled("external_scanners", create_config=False)
    except Exception:
        return False


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


def _read_nofollow(path: Path | str) -> bytes | None:
    """Bytes of a regular file opened no-follow, non-blocking, judged by fstat; None if absent."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"not a regular file: {path}")
        if os.name == "nt" and os.path.islink(path):
            raise OSError(f"linked file: {path}")
        return handle.read()


def _key(*, create: bool) -> bytes | None:
    from apm_cli.install import verdict_key

    return verdict_key.load(_apm_home() / KEY_FILE, create=create)


class _Digest:
    """Length-prefixed HMAC over tagged inputs (QL C7)."""

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._mac = hmac.new(key, digestmod=hashlib.sha256)
        self.tags: list[tuple[str, str]] = []

    def put(self, tag: str, data: bytes) -> None:
        encoded = tag.encode("utf-8", "surrogateescape")
        self._mac.update(len(encoded).to_bytes(4, "little") + encoded)
        self._mac.update(len(data).to_bytes(8, "little") + data)
        # Keyed per-input digests (diagnostics only): a secret value never leaves unkeyed.
        self.tags.append((tag, hmac.new(self._key, data, hashlib.sha256).hexdigest()[:16]))

    def file(self, tag: str, path: Path | str) -> None:
        data = _read_nofollow(path)
        self.put(tag, b"\x00absent" if data is None else b"\x01" + data)

    def hexdigest(self) -> str:
        return self._mac.hexdigest()


def _tree(
    base: str, skip: tuple[str, ...] = ()
) -> Iterator[tuple[str, list[os.DirEntry[str]], list[str]]]:
    """Pre-order walk in sorted order: (rel dir, non-directory entries, subdir names).

    Directory entries carry their type, so no file is stat'ed twice; a symlink
    (to a file or a directory) is a non-directory entry and is never followed.
    """
    stack = [("", base)]
    while stack:
        rel, path = stack.pop()
        try:
            with os.scandir(path) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        files = [entry for entry in entries if not entry.is_dir(follow_symlinks=False)]
        dirs = [e.name for e in entries if e.is_dir(follow_symlinks=False) and e.name not in skip]
        yield rel, files, dirs
        for name in reversed(dirs):
            stack.append((f"{rel}/{name}" if rel else name, os.path.join(path, name)))


def _walk_files(base: Path, skip: tuple[str, ...] = ()) -> Iterator[Path]:
    for _rel, files, _dirs in _tree(str(base), skip):
        for entry in files:
            yield Path(entry.path)


def _owner_surfaces(root: Path) -> list[str]:
    """MCP config files outside the project, named by their adapters (W3d R4)."""
    from apm_cli.factory import ClientFactory

    inside = os.path.realpath(root)
    found: set[str] = set()
    for runtime in sorted(ClientFactory.supported_clients()):
        try:
            adapter = ClientFactory.create_client(runtime, project_root=root, user_scope=False)
            paths = [adapter.get_config_path()]
            legacy = getattr(adapter, "get_legacy_config_path", None)
            if callable(legacy):
                paths.append(legacy())
        except Exception:  # an adapter that cannot name its file cannot write it either
            found.add(f"\x00unnamed:{runtime}")
            continue
        for path in paths:
            if not path:
                continue
            real = os.path.realpath(os.path.expanduser(str(path)))
            if real != inside and not real.startswith(inside + os.sep):
                found.add(real)
    return sorted(found)


def _inputs(
    digest: _Digest,
    root: Path,
    argv: list[str],
    lock_text: bytes,
    run_env: list[str],
    surfaces: list[str],
    git_system: str,
    deployed: frozenset[str],
    notice: str,
) -> None:
    """Every verdict input, read live (QL D3-D6, O3); a writer reads the run's entry state."""
    if os.path.lexists(os.path.join(root, ".apm")):
        raise ValueError("root .apm sources")  # not verdict inputs: never recorded, never a hit
    digest.put("version", str(RECORD_VERSION).encode())
    digest.put("build", str(getattr(sys.modules.get("apm_cli"), "__version__", "")).encode())
    digest.put("build-id", _build_identity())
    digest.put("argv", "\x00".join(argv).encode())
    digest.put("root", os.path.realpath(root).encode("utf-8", "surrogateescape"))
    digest.put("platform", f"{sys.platform}|{os.sep}|{sys.version_info[:2]}".encode())
    digest.put("presentation", _ENTRY_PRESENTATION or _presentation())
    for name in ENV_ALLOWLIST:
        value = _env(name)
        digest.put(f"env:{name}", b"\x00unset" if value is None else b"\x01" + value.encode())
    digest.put("run-env", "\x00".join(run_env).encode("utf-8", "surrogateescape"))
    for name in run_env:
        value = _env(name, read_by_run=True)
        digest.put(f"env:{name}", b"\x00unset" if value is None else b"\x01" + value.encode())
    for name, value in _env_prefixed("GIT_"):
        digest.put(f"git-env:{name}", value.encode("utf-8", "surrogateescape"))
    from apm_cli.install import verdict_git

    if verdict_git._git_env_include():
        raise ValueError("git config include")
    digest.put("git-binary", verdict_git._git_identity().encode("utf-8", "surrogateescape"))
    digest.put("git-system", git_system.encode("utf-8", "surrogateescape"))
    verdict_git._git_config_bytes(digest, root, git_system)
    digest.put("surfaces", "\x00".join(surfaces).encode("utf-8", "surrogateescape"))
    for surface in surfaces:
        if not surface.startswith("\x00"):
            digest.file(f"surface:{surface}", Path(surface))
    digest.put("lock", lock_text)
    for rel in _PROJECT_SURFACES:
        digest.file(f"project:{rel}", root / rel)
    user = Path(os.path.expanduser("~"))
    for rel in _USER_SURFACES:
        digest.file(f"user:{rel}", user / rel)
    from apm_cli.install.locking import _LIFECYCLE_LOCK_NAME

    # Not verdict state: the MAC's key, and the held lifecycle lock (Windows locks it).
    unkeyed = (KEY_FILE, _LIFECYCLE_LOCK_NAME)
    for rel, files, _dirs in _tree(str(_apm_home()), skip=(RECORDS_DIR, "cache")):
        for entry in files:
            if entry.name not in unkeyed:
                digest.file(f"home:{rel}/{entry.name}" if rel else f"home:{entry.name}", entry.path)
    cache = _cache_root()
    if cache is not None:
        for rel, files, _dirs in _tree(str(cache / "policy_v1")):
            for entry in files:
                name = f"{rel}/{entry.name}" if rel else entry.name
                digest.put(f"policy-mtime:{name}", b"%d" % entry.stat().st_mtime_ns)
                digest.file(f"policy:{name}", entry.path)
    from apm_cli.install import verdict_inputs

    verdict_inputs.key_startup(digest, root, notice, surfaces)
    for name in _GOVERNED_DIRS:
        base = os.path.join(root, name)
        digest.put(f"target:{name}", verdict_inputs._kind(base))
        for rel, files, dirs in verdict_inputs.walk_governed(base):
            here = f"{name}/{rel}" if rel else name
            listing = [entry.name for entry in files] + ["/", *dirs]
            digest.put(f"listing:{here}", "\x00".join(listing).encode("utf-8", "surrogateescape"))
            for entry in files:
                if f"{here}/{entry.name}" in deployed and not entry.is_symlink():
                    continue  # its bytes are judged against the MAC'd lock (_deployed_intact)
                if entry.is_symlink():
                    target = os.readlink(entry.path)
                    digest.put(
                        f"link:{here}/{entry.name}", target.encode("utf-8", "surrogateescape")
                    )
                else:
                    digest.file(f"governed:{here}/{entry.name}", entry.path)


# deps/lockfile.FRAMED_CONTENT_HASH_VERSION (asserted equal in the cells): importing
# the lockfile owner costs a hit more than the whole replay.
_FRAMED_LOCKFILE_VERSION = "4"
_CLOCK_SKEW = 300


def _policy_identity(root: Path) -> Any:
    """The org, host and port policy discovery derives from the project's remote."""
    from apm_cli.policy._remote import _extract_org_host_port_from_git_remote, reuse_remote_reads

    with reuse_remote_reads():  # this run already read the remote
        return _extract_org_host_port_from_git_remote(root)


def _build_identity() -> bytes:
    """The running APM implementation (W3d A3): changes with any APM code change.

    A frozen binary's own stat; an installed (non-editable) distribution's RECORD,
    which holds every installed file's hash; otherwise (an editable install or a
    source tree) the content of every file of the package. Never the version alone:
    when none can be read, this raises and neither a record nor a replay proceeds.
    """
    if getattr(sys, "frozen", False):
        path = os.path.realpath(sys.executable)
        info = os.stat(path)
        return f"frozen|{path}|{info.st_size}|{info.st_mtime_ns}".encode("utf-8", "surrogateescape")
    package = os.path.dirname(os.path.realpath(sys.modules["apm_cli"].__file__ or ""))
    record = _installed_record(package)
    if record is not None:
        return b"installed|" + hashlib.sha256(record).hexdigest().encode()
    digest = hashlib.sha256()

    def refuse(error: OSError) -> None:
        raise error

    for here, dirs, files in os.walk(package, onerror=refuse):
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in sorted(files):
            data = _read_nofollow(os.path.join(here, name))
            if data is None:
                raise ValueError("the APM source tree cannot be read")
            rel = os.path.relpath(os.path.join(here, name), package)
            digest.update(f"{rel}\x00{len(data)}\x00".encode("utf-8", "surrogateescape"))
            digest.update(data)
    return f"source|{package}|{digest.hexdigest()}".encode("utf-8", "surrogateescape")


def _installed_record(package: str) -> bytes | None:
    """The RECORD of the non-editable distribution that installed *package*, if any."""
    site = os.path.dirname(package)
    try:
        names = [
            n for n in os.listdir(site) if n.startswith("apm_cli-") and n.endswith(".dist-info")
        ]
    except OSError:
        return None
    if len(names) != 1:
        return None
    dist = os.path.join(site, names[0])
    direct = _read_nofollow(os.path.join(dist, "direct_url.json")) or b""
    record = _read_nofollow(os.path.join(dist, "RECORD"))
    if b'"editable":true' in direct.replace(b" ", b"") or record is None:
        return None  # an editable install's files are the source tree: stat them
    return record if b"apm_cli/__init__.py," in record else None


def _names(value: Any) -> bool:
    """A record's name list: a list of strings (an empty list is a list)."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _body(digest: _Digest, record: dict) -> None:
    """The replayed bytes and their validity window are authenticated too (W3d R1)."""
    for field in ("stdout", "stderr", "exit", "issued_at", "expires_at", "kind", "provenance"):
        digest.put(f"body:{field}", repr(record.get(field)).encode("utf-8", "surrogateescape"))


def _provable_row(field: Any) -> bool:
    """A package copy with a locked content hash at a plain path (W3d R3).

    Local, virtual and unhashed packages cannot be proven by a hit, so a run
    over them records nothing instead of a record that could never hit.
    *field* reads one lock-row field (a mapping row or a LockedDependency).
    """
    repo = str(field("repo_url") or "")
    if not repo or ".." in repo.split("/") or not field("content_hash"):
        return False
    return not (field("virtual_path") or field("source"))


def _provable(lock: dict) -> bool:
    if lock.get("local_deployed_files") or lock.get("local_staged_files"):
        return False  # root-local content: its sources are not verdict inputs
    rows = lock.get("dependencies") or []
    return all(isinstance(row, dict) and _provable_row(row.get) for row in rows)


def _deployed_rels(lock: dict) -> frozenset[str]:
    """Deployed files the lock records a content hash for (mapping rows)."""
    return frozenset(
        rel
        for row in lock.get("dependencies") or []
        if isinstance(row, dict)
        for rel in row.get("deployed_file_hashes") or {}
    )


def _deployed_intact(root: Path, lock: dict) -> bool:
    """Every deployed file and package copy, re-hashed no-follow (QL D5)."""
    from apm_cli.install.verdict_inputs import has_linked_parent, hash_blind
    from apm_cli.utils.content_hash import canonical_digest

    if not _provable(lock):
        return False
    proven: set[str] = set()
    for dep in lock.get("dependencies") or []:
        for rel, expected in (dep.get("deployed_file_hashes") or {}).items():
            if has_linked_parent(root, rel, proven):
                return False  # the engine never deploys through a link (#5438)
            data = _read_nofollow(root / rel)
            # The engine's own judgment of a present deployed file (diff_apply._plan_files).
            if data is None or f"sha256:{canonical_digest(data)}" != expected:
                return False
        repo = str(dep.get("repo_url", ""))
        expected = dep.get("content_hash")
        package = root / "apm_modules" / Path(*repo.split("/"))
        if package.is_symlink() or not package.is_dir():
            return False
        from apm_cli.deps.carried_license import verify_locked_package

        # The lock owner's hash-stream selection: framed only at lockfile version 4.
        framed = lock.get("lockfile_version") == _FRAMED_LOCKFILE_VERSION
        records = [_Row(item) for item in dep.get("carried_licenses") or ()]
        if not verify_locked_package(package, str(expected), records, framed=framed):
            return False  # the package bytes and its carried licence copies (W3d R3)
        from apm_cli.install.cache_pin import marker_is_current

        if not marker_is_current(package, str(dep.get("resolved_commit") or "")):
            return False  # hashing skips .apm-pin; the full run's marker sync repairs it
        if hash_blind(package):
            return False
    return True


class _Row:
    """Attribute view of a lock mapping, for owners that read lock rows by attribute."""

    def __init__(self, data: Any) -> None:
        self._data = data if isinstance(data, dict) else {}

    def __getattr__(self, name: str) -> Any:
        return self._data.get(name)


def _fast_path_mode() -> str:
    return (_env("APM_INSTALL_FAST_PATH") or "").strip().lower()


def _trace(message: str) -> None:
    """Diagnostics only under APM_INSTALL_FAST_PATH=verdict (never in the trace contract)."""
    if _fast_path_mode() == "verdict":
        sys.stderr.write(f"[i] verdict record: {message}\n")


class _Miss(Exception):
    """The record does not prove this run; the message (if any) is the trace reason."""


def _verified_stdout(argv: list[str], root: Path, *, attested: bool = False) -> tuple[dict, str]:
    """The record and its stdout, only when it proves this run; raises _Miss otherwise.

    *attested* selects an attested record (no output, W3d A5), which only the
    install engine's fast path consumes; a replay never reads one.
    """
    raw = _read_nofollow(_record_path(root))
    if raw is None or not _location_trusted(root):
        raise _Miss("")
    from apm_cli.core.agents_md_compile_record import may_carry

    if not attested and may_carry(root):
        # The full run decides Claude rules from it; a replay never does (#5434).
        raise _Miss("a compiled AGENTS.md may carry a Claude rule")
    key = _key(create=False)
    if key is None:
        raise _Miss("no key")
    record = json.loads(raw.decode("utf-8"))
    lock_text = _read_nofollow(root / "apm.lock.yaml")
    run_env, surfaces = record.get("run_env"), record.get("surfaces")
    git_system, notice = record.get("git_system"), record.get("notice")
    if (record.get("kind") == ATTESTED) is not attested:
        raise _Miss("")  # an attested record has no output: only the engine consumes it
    if (
        lock_text is None
        or record.get("v") != RECORD_VERSION
        or int(record.get("exit", 1)) != 0
        or not (_names(run_env) and _names(surfaces) and _names([git_system, notice]))
    ):
        raise _Miss("")
    lock_tag = hmac.new(key, lock_text, hashlib.sha256).hexdigest()[:16]
    if dict(record.get("tags") or []).get("lock") != lock_tag:
        raise _Miss("inputs changed: lock")  # before any other input is read
    import yaml

    lock = yaml.load(lock_text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))  # noqa: S506
    if not isinstance(lock, dict):
        raise _Miss("lock")
    digest = _Digest(key)
    rels = _deployed_rels(lock)
    _inputs(digest, root, argv, lock_text, run_env, surfaces, git_system, rels, notice)
    _body(digest, record)
    now = time.time()
    issued, expires = float(record.get("issued_at", 0)), float(record.get("expires_at", 0))
    if now > expires or now < issued - _CLOCK_SKEW:
        raise _Miss("expired")
    if not hmac.compare_digest(digest.hexdigest(), str(record.get("mac", ""))):
        old = dict(record.get("tags") or [])
        changed = sorted(t for t, h in digest.tags if old.get(t) != h)
        raise _Miss(f"inputs changed: {', '.join(changed[:6])}")
    if not _deployed_intact(root, lock):
        raise _Miss("deployed bytes")
    from apm_cli.install import verdict_policy

    return record, verdict_policy._render_ages(str(record.get("stdout", "")))


def replay(argv: list[str]) -> bool:
    """Print the recorded no-op and return True, or return False to run the CLI."""
    started = time.perf_counter()
    if not _served(argv) or not _hits_allowed():
        return False
    try:
        if not os.path.lexists(_record_path(Path.cwd())):
            return False
    except (OSError, ValueError):
        return False
    lock = None
    try:
        from apm_cli.install.locking import acquire_lifecycle_lock

        lock = acquire_lifecycle_lock()  # never over a concurrent install's partial state
        record, stdout_text = _verified_stdout(argv, Path.cwd())
        stderr_text = str(record.get("stderr", ""))
        for stream, text in ((sys.stdout, stdout_text), (sys.stderr, stderr_text)):
            text.encode(getattr(stream, "encoding", None) or "utf-8")
    except Exception as exc:  # a miss, a busy lock, an unencodable record: run the CLI
        if lock is not None:
            lock.release()
        if not isinstance(exc, _Miss):
            _trace(f"miss ({type(exc).__name__})")
        elif str(exc):
            _trace(f"miss ({exc})")
        return False
    try:
        return _print_hit(started, stdout_text, stderr_text)
    finally:
        lock.release()


def _print_hit(started: float, stdout_text: str, stderr_text: str) -> bool:
    elapsed = f"{time.perf_counter() - started:.1f}"
    sys.stdout.write(stdout_text.replace(_ELAPSED, elapsed))
    sys.stderr.write(stderr_text)
    sys.stdout.flush()
    if _fast_path_mode() == "trace":  # the one decision line of the fast-path trace contract
        sys.stderr.write("[i] install fast path: no-op (verified-state record)\n")
    _trace("hit")
    return True


class _Tee:
    """The shown stream, plus what was shown: a verified no-op's exact output."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        self.parts.append(text)
        return self.stream.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


_ACTIVE: list[_Tee] = []
_UNKNOWN: list[str] = []


def unknown(reason: str) -> None:
    """An owner printed something a no-op replay must not repeat: record nothing."""
    if _ACTIVE:
        _UNKNOWN.append(reason)


@contextmanager
def recording(argv: list[str]) -> Iterator[None]:
    """Run the CLI; after a verified engine no-op with exit 0, record its output.

    Only a verified no-op records (VP ruling A): a full install never pays for a
    record, and the next unchanged run, a verified no-op, writes it.
    """
    if not _served(argv) or not _hits_allowed():
        yield
        return
    for state in (
        _VERIFIED_NOOP,
        _UNKNOWN,
        _PROVEN_LOCK,
        _ATTESTED_INSTALL,
        _RUN_ENTRY,
        _RUN_TARGETS,
    ):
        state.clear()
    _ENV_READS.clear()
    entry = (_env_snapshot(), _presentation())
    _RUN_ENTRY.append(entry)
    _ENTRY_FILES[:] = [_entry_bytes("apm.yml"), _entry_bytes("apm.lock.yaml")]
    from apm_cli.install.verdict_inputs import entry_stamp

    _ENTRY_TIME[:] = entry_stamp()
    if (console := sys.modules.get("apm_cli.utils.console")) is not None:
        console.EMITTED.clear()  # never imported here: the replay path stays light
    out, err = _Tee(sys.stdout), _Tee(sys.stderr)
    sys.stdout, sys.stderr = out, err  # type: ignore[assignment]
    _ACTIVE.append(out)
    code: object = 1
    try:
        with _env_log():
            yield
        code = 0
    except SystemExit as exc:
        code = 0 if exc.code is None else exc.code
        raise
    finally:
        _ACTIVE.remove(out)
        sys.stdout, sys.stderr = out.stream, err.stream
        _settle(argv, entry, out, err, code)


def _settle(argv: list[str], entry: Any, out: _Tee, err: _Tee, code: object) -> None:
    """Write what this run proved, attested or recorded (install/verdict_write.py)."""
    from apm_cli.install.verdict_write import _record_locked, _unsettled_line, unsettled_message

    if code == 0 and _ATTESTED_INSTALL and not _VERIFIED_NOOP:
        # No output is recorded, so an unknown no-op form does not matter:
        # every attested hit renders its output live.
        text = "".join(out.parts) + "".join(err.parts)
        unsettled = _unsettled_line(text) or unsettled_message()
        if unsettled is None:
            _record_locked(argv, entry, out, err, attested=True)
        else:
            _trace(f"no attestation (printed: {unsettled[:80]})")
    elif _UNKNOWN:
        _trace(f"no record (no-op form unknown: {', '.join(sorted(set(_UNKNOWN)))})")
    elif code == 0 and _VERIFIED_NOOP:
        _record_locked(argv, entry, out, err)
    else:
        _trace("no record (not a verified no-op)")
