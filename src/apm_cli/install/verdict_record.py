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
# these (their values, or their absence) plus every name the recorded run read
# (_env_log); the static guard in tests/unit/install/test_verdict_record.py fails
# when ``src/apm_cli`` reads an environment variable that is neither here nor in
# ENV_NOT_VERDICT. Owner-read roots (the APM home, harness user roots) are
# named only by their owners and keyed through the run's own reads.
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
    "APM_E2E_TESTS",
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
    "APM_POLICY_DISABLE",
)

# Project target roots: every file in them (listing and bytes) is a verdict input,
# which covers unmanaged files (D3) and the merged configs kept there (D4). The
# coverage cell in tests/unit/install/test_verdict_record.py fails when a target
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
    ".git/config",
    ".gitignore",
    "apm.yml",
    "opencode.json",
)
_USER_SURFACES = (
    ".claude.json",
    ".codex/config.toml",
    ".codeium/windsurf/mcp_config.json",
    ".hermes/config.yaml",
)

_VERIFIED_NOOP: list[bool] = []
# Same values as install/noop_diff.disabled() (APM_INSTALL_FAST_PATH kill switch).
_KILL_VALUES = frozenset({"0", "false", "no", "off"})
# O1: the summary's elapsed seconds are templated; any other clock-dependent
# token (a cache age, a countdown) means the output is not recorded at all.
_ELAPSED = "\x00elapsed\x00"
_ELAPSED_RE = re.compile(r"(up to date in )\d+\.\d(s\.)")
# O1: a cached policy's age (CommandLogger.policy_resolved) is templated to its
# cache entry and rendered from that entry's live cached_at at replay.
_AGE_RE = re.compile(r"(fetched )(\d+[smh])( ago)")
_AGE_TOKEN_RE = re.compile("\x00age:([^\x00]+)\x00")
_AGE_LAG = 900  # the policy line prints at discovery; the writer runs up to this much later
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


def note_verified_noop() -> None:
    """The install engine proved this run a no-op: its output may be recorded."""
    _VERIFIED_NOOP.append(True)


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


_GIT_TRUE = frozenset({"1", "true", "yes", "on"})


def _git_files(root: Path, system: str) -> list[Path]:
    """The config files a child ``git`` reads for this checkout, at every scope.

    The no-op runs ``git remote`` for policy discovery and it honors
    ``url.insteadOf``; a hit runs no git, so it keys what git would read: the
    repository config (``GIT_DIR``/``GIT_COMMON_DIR``, else found upward with a
    ``.git`` file followed to its gitdir and common dir), the global config
    (``GIT_CONFIG_GLOBAL``, else ``~/.gitconfig`` and the XDG file) and *system*,
    the path git itself reported (``git var GIT_CONFIG_SYSTEM``) for the binary
    keyed beside it. ``GIT_*`` and PATH are keyed as values too.
    """
    env = dict(_env_prefixed("GIT_"))
    cwd = Path.cwd()
    files: list[Path] = []
    if env.get("GIT_DIR"):
        gitdir = cwd / env["GIT_DIR"]
        common = cwd / env["GIT_COMMON_DIR"] if env.get("GIT_COMMON_DIR") else gitdir
        files += [gitdir / "config", gitdir / "config.worktree", common / "config"]
    else:
        for base in (root, *root.parents):
            dot = base / ".git"
            if dot.is_dir():
                files += [dot / "config", dot / "config.worktree"]
                break
            if dot.is_file():
                text = dot.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    break
                gitdir = (base / text[len("gitdir:") :].strip()).resolve()
                files += [dot, gitdir / "config", gitdir / "config.worktree"]
                common = gitdir / "commondir"
                if common.is_file():
                    files.append(common)
                    files.append((gitdir / common.read_text(encoding="utf-8").strip()) / "config")
                break
    if env.get("GIT_CONFIG_GLOBAL"):
        files.append(cwd / env["GIT_CONFIG_GLOBAL"])
    else:
        home = Path(os.path.expanduser("~"))
        xdg = _env("XDG_CONFIG_HOME") or str(home / ".config")
        files += [home / ".gitconfig", Path(xdg) / "git" / "config"]
    if system:
        files.append(Path(system))
    if os.name == "nt":
        files.append(Path(_env("PROGRAMDATA") or "C:/ProgramData") / "Git" / "config")
    return files


def _git() -> str | None:
    """The git the no-op runs: APM's trusted resolver (never the working tree)."""
    from apm_cli.utils.git_env import get_git_executable

    try:
        return get_git_executable()
    except FileNotFoundError:
        return None


def _git_identity() -> str:
    """That git as path, size and mtime: an upgrade or a swapped binary misses."""
    git = _git()
    if git is None:
        return "none"
    exe = Path(git).resolve()
    info = exe.stat()
    return f"{exe}|{info.st_size}|{info.st_mtime_ns}"


def _git_system() -> str:
    """The system config path git reads, asked of git once per record (W3d R7)."""
    env = dict(_env_prefixed("GIT_"))
    if (env.get("GIT_CONFIG_NOSYSTEM") or "").strip().lower() in _GIT_TRUE:
        return ""
    if env.get("GIT_CONFIG_SYSTEM"):
        return str(Path.cwd() / env["GIT_CONFIG_SYSTEM"])
    import subprocess

    git = _git()
    if git is None:
        return ""
    try:
        result = subprocess.run(
            [git, "var", "GIT_CONFIG_SYSTEM"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("git cannot name its system config") from exc
    path = result.stdout.strip()
    if result.returncode != 0 or not path:
        raise ValueError("git cannot name its system config")
    return path


def _git_env_include() -> bool:
    """An include injected through the environment (``GIT_CONFIG_KEY_n``,
    ``GIT_CONFIG_PARAMETERS``) is refused like an ``[include]`` section."""
    for name, value in _env_prefixed("GIT_CONFIG_"):
        if (name.startswith("GIT_CONFIG_KEY_") or name == "GIT_CONFIG_PARAMETERS") and (
            "include" in value.lower()
        ):
            return True
    return False


_INCLUDE_RE = re.compile(rb"^\s*\[\s*include", re.IGNORECASE | re.MULTILINE)


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
) -> None:
    """Every verdict input, read live (QL D3-D6, O3); a writer reads the run's entry state."""
    digest.put("version", str(RECORD_VERSION).encode())
    digest.put("build", str(getattr(sys.modules.get("apm_cli"), "__version__", "")).encode())
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
    if _git_env_include():
        raise ValueError("git config include")
    digest.put("git-binary", _git_identity().encode("utf-8", "surrogateescape"))
    digest.put("git-system", git_system.encode("utf-8", "surrogateescape"))
    for path in _git_files(root, git_system):
        # Keyed bytes only, never trusted content: a dotfile symlink is followed.
        try:
            data: bytes | None = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            data = None
        if data is not None and _INCLUDE_RE.search(data):
            raise ValueError("git config include")
        digest.put(f"git:{path}", b"\x00absent" if data is None else b"\x01" + data)
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
            for name in sorted(filenames):
                path = Path(directory) / name
                if path.is_symlink():
                    digest.put(
                        f"link:{rel}/{name}", os.readlink(path).encode("utf-8", "surrogateescape")
                    )
                else:
                    digest.file(f"governed:{rel}/{name}", path)


# A record expires within a day, and when its earliest cached policy goes stale
# for discovery (cached_at + capped_ttl(cache.ttl)), so a refetch is never
# replayed past (W3d R2).
_RECORD_MAX_AGE = 24 * 3600
_CLOCK_SKEW = 300


def _policy_fresh_until(meta_path: Path) -> float:
    """When discovery stops trusting one cached policy (policy/discovery.py)."""
    from apm_cli.policy._cache_verify import capped_ttl
    from apm_cli.policy.parser import load_policy

    meta = json.loads(_read_nofollow(meta_path) or b"{}")
    document = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".yml")
    try:
        policy, _warnings = load_policy(document.read_text(encoding="utf-8"))
        ttl = capped_ttl(int(policy.cache.ttl))
    except Exception:  # an unreadable policy is never fresh: the record is born expired
        ttl = 0
    return float(meta.get("cached_at", 0)) + ttl


def _policy_metas() -> list[tuple[str, float]]:
    cache = _cache_root()
    if cache is None:
        return []
    policy = cache / "policy_v1"
    metas = []
    for path in _walk_files(policy):
        if path.name.endswith(".meta.json"):
            meta = json.loads(_read_nofollow(path) or b"{}")
            metas.append((path.relative_to(policy).as_posix(), float(meta.get("cached_at", 0))))
    return metas


def _template_ages(text: str) -> str | None:
    """Each printed policy age becomes a token naming its cache entry; None if unsure."""
    if not _AGE_RE.search(text):
        return text
    from apm_cli.core.command_logger import format_policy_age

    now, metas = time.time(), _policy_metas()

    def token(match: re.Match[str]) -> str:
        found = {
            rel
            for rel, cached_at in metas
            for lag in range(_AGE_LAG)
            if format_policy_age(int(now - cached_at) - lag) == match.group(2)
        }
        if len(found) != 1:
            raise ValueError("policy age")
        return f"{match.group(1)}\x00age:{found.pop()}\x00{match.group(3)}"

    try:
        return _AGE_RE.sub(token, text)
    except ValueError:
        return None


def _render_ages(text: str) -> str:
    """The policy line's age now, from the live cache entry the record names."""
    if "\x00age:" not in text:
        return text
    from apm_cli.core.command_logger import format_policy_age

    cache = _cache_root()
    if cache is None:
        raise ValueError("no cache")

    def age(match: re.Match[str]) -> str:
        rel = match.group(1)
        if ".." in rel.split("/"):
            raise ValueError("policy age path")
        meta = json.loads(_read_nofollow(cache / "policy_v1" / rel) or b"{}")
        seconds = int(time.time() - float(meta["cached_at"]))
        if seconds < 0:
            raise ValueError("policy age")
        return format_policy_age(seconds)

    return _AGE_TOKEN_RE.sub(age, text)


def _expiry(issued: float) -> float:
    expiry = issued + _RECORD_MAX_AGE
    cache = _cache_root()
    if cache is not None:
        for path in _walk_files(cache / "policy_v1"):
            if path.name.endswith(".meta.json"):
                expiry = min(expiry, _policy_fresh_until(path))
    return expiry


def _names(value: Any) -> bool:
    """A record's name list: a list of strings (an empty list is a list)."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _body(digest: _Digest, record: dict) -> None:
    """The replayed bytes and their validity window are authenticated too (W3d R1)."""
    for field in ("stdout", "stderr", "exit", "issued_at", "expires_at"):
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
    rows = lock.get("dependencies") or []
    return all(isinstance(row, dict) and _provable_row(row.get) for row in rows)


def _deployed_intact(root: Path, lock: dict) -> bool:
    """Every deployed file and package copy, re-hashed no-follow (QL D5)."""
    if not _provable(lock):
        return False
    for dep in lock.get("dependencies") or []:
        for rel, expected in (dep.get("deployed_file_hashes") or {}).items():
            data = _read_nofollow(root / rel)
            if data is None:
                return False
            if "sha256:" + hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() != expected:
                return False
        repo = str(dep.get("repo_url", ""))
        expected = dep.get("content_hash")
        package = root / "apm_modules" / Path(*repo.split("/"))
        if package.is_symlink() or not package.is_dir():
            return False
        from apm_cli.deps.carried_license import carried_license_paths
        from apm_cli.utils.content_hash import verify_package_hash

        exclude = carried_license_paths([_Row(item) for item in dep.get("carried_licenses") or ()])
        framed = bool(dep.get("content_hash_framed", True))
        if not verify_package_hash(package, str(expected), exclude=exclude, framed=framed):
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


def replay(argv: list[str]) -> bool:
    """Print the recorded no-op and return True, or return False to run the CLI."""
    started = time.perf_counter()
    if not _served(argv) or not _hits_allowed():
        return False
    root = Path.cwd()
    if not _location_trusted(root):
        return False
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
        run_env, surfaces = record.get("run_env"), record.get("surfaces")
        git_system = record.get("git_system")
        if not _names(run_env) or not _names(surfaces) or not isinstance(git_system, str):
            return False
        digest = _Digest(key)
        _inputs(digest, root, argv, lock_text, run_env, surfaces, git_system)
        _body(digest, record)
        now = time.time()
        issued, expires = float(record.get("issued_at", 0)), float(record.get("expires_at", 0))
        if now > expires or now < issued - _CLOCK_SKEW:
            _trace("miss (expired)")
            return False
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
        stdout_text = _render_ages(str(record.get("stdout", "")))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        _trace(f"miss ({type(exc).__name__})")
        return False
    if int(record.get("exit", 1)) != 0:
        return False
    elapsed = f"{time.perf_counter() - started:.1f}"
    sys.stdout.write(stdout_text.replace(_ELAPSED, elapsed))
    sys.stderr.write(str(record.get("stderr", "")))
    sys.stdout.flush()
    if _fast_path_mode() == "trace":  # the one decision line of the fast-path trace contract
        sys.stderr.write("[i] install fast path: no-op (verified-state record)\n")
    _trace("hit")
    return True


class _Tee:
    """Shown stream plus the no-op view: what this run would print had it been a no-op.

    Owners whose output differs between a change and a no-op print their change-only
    lines inside :func:`change_only` (shown, not recorded) and the no-op form inside
    :func:`noop_only` (recorded, not shown); everything else is both.
    """

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.parts: list[str] = []
        self.mode = "both"

    def write(self, text: str) -> int:
        if self.mode != "shown":
            self.parts.append(text)
        if self.mode == "recorded":
            return len(text)
        return self.stream.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class _Capture(_Tee):
    """Records only (the write-time render); isatty and encoding stay the real stream's."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self.mode = "recorded"


_ACTIVE: list[_Tee] = []
_SEGMENTS: dict[str, tuple[int, int]] = {}
_UNKNOWN: list[str] = []
_FULL_SUCCESS: list[bool] = []


def recording_active() -> bool:
    return bool(_ACTIVE)


@contextmanager
def _mode(mode: str) -> Iterator[None]:
    tee = _ACTIVE[-1] if _ACTIVE else None
    if tee is None:
        yield
        return
    previous, tee.mode = tee.mode, mode
    try:
        yield
    finally:
        tee.mode = previous


def change_only() -> Any:
    """Lines printed only because something changed: shown, never in the no-op view."""
    return _mode("shown")


def noop_only() -> Any:
    """The no-op form of a changed line: in the no-op view, never shown."""
    return _mode("recorded")


@contextmanager
def segment(name: str) -> Iterator[None]:
    """Mark the no-op view of one owner phase (the services block)."""
    tee = _ACTIVE[-1] if _ACTIVE else None
    start = len(tee.parts) if tee is not None else 0
    try:
        yield
    finally:
        if tee is not None:
            _SEGMENTS[name] = (start, len(tee.parts))


def unknown(reason: str) -> None:
    """An owner printed something whose no-op form it does not know: record nothing."""
    if _ACTIVE:
        _UNKNOWN.append(reason)


def note_full_success() -> None:
    """A full (or applied) install finished: its no-op form may be recorded."""
    _FULL_SUCCESS.append(True)


@contextmanager
def recording(argv: list[str]) -> Iterator[None]:
    """Run the CLI; after a verified no-op or install with exit 0, record the no-op form."""
    if not _served(argv) or not _hits_allowed():
        yield
        return
    for state in (_VERIFIED_NOOP, _UNKNOWN, _FULL_SUCCESS):
        state.clear()
    _SEGMENTS.clear()
    _ENV_READS.clear()
    entry = (_env_snapshot(), _presentation())
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
        if code == 0 and not _UNKNOWN:
            _record_after(argv, entry, out, err)
        elif _UNKNOWN:
            _trace(f"no record (no-op form unknown: {', '.join(sorted(set(_UNKNOWN)))})")


def _record_after(argv: list[str], entry: Any, out: _Tee, err: _Tee) -> None:
    """After a successful run: record its no-op form when the run proved one."""
    noop = bool(_VERIFIED_NOOP) and not _FULL_SUCCESS
    if not noop and not (_FULL_SUCCESS and "services" in _SEGMENTS):
        _trace("no record (no verified no-op or full success)")
        return
    lock = _committed_lock()
    if lock is None:
        return
    if noop:
        _write_at_entry(entry, argv, "".join(out.parts), "".join(err.parts))
        return
    start, end = _SEGMENTS["services"]
    try:
        text = _noop_form(argv, "".join(out.parts[start:end]), lock)
    except Exception as exc:  # the install succeeded; only the record is lost
        _trace(f"no record (no-op form: {type(exc).__name__})")
        return
    if text is not None:
        _write_at_entry(entry, argv, text, "")


def _committed_lock() -> Any:
    """The lock this run committed (its owner's read), if a hit could prove it."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    try:
        lock = LockFile.read(get_lockfile_path(Path.cwd()))
    except Exception as exc:  # the install succeeded; only the record is lost
        _trace(f"no record (lockfile: {type(exc).__name__})")
        return None
    if lock is None:
        _trace("no record (no lockfile)")
        return None
    for key, dep in lock.dependencies.items():
        if key != "." and not _provable_row(lambda name, dep=dep: getattr(dep, name, None)):
            _trace("no record (a package copy a hit cannot prove)")
            return None
    return lock


def _write_at_entry(
    entry: tuple[dict[str, str], bytes], argv: list[str], out: str, err: str
) -> None:
    """Write the record keyed by the run's entry state (what a hit will see)."""
    global _ENTRY_ENV, _ENTRY_PRESENTATION
    _ENTRY_ENV, _ENTRY_PRESENTATION = entry
    try:
        _write(argv, out, err)
    finally:
        _ENTRY_ENV, _ENTRY_PRESENTATION = None, None


def _noop_form(argv: list[str], services_text: str, lock: Any) -> str | None:
    """Today's no-op text, rendered by the engine's own renderer with the services block's
    no-op view (fast_output.render), in this process after the install committed."""
    import time as _time

    from apm_cli.core.scope import manifest_file
    from apm_cli.install import fast_output
    from apm_cli.install.service import InstallService
    from apm_cli.models.apm_package import APMPackage

    root = Path.cwd()
    if lock is None:
        return None
    manifest = APMPackage.from_apm_yml(manifest_file(root))
    from types import SimpleNamespace

    disposition = InstallService.frozen_disposition(SimpleNamespace(frozen="--frozen" in argv))
    capture = _Capture(sys.stdout)
    real = sys.stdout
    sys.stdout = capture  # type: ignore[assignment]
    _ACTIVE.append(capture)
    try:
        fast_output.render(
            root,
            lock,
            manifest,
            started=_time.perf_counter(),
            bodies=None,
            installed=0,
            frozen_disposition=disposition,
            services_text=services_text,
        )
    finally:
        _ACTIVE.remove(capture)
        sys.stdout = real
    return "".join(capture.parts)


def _write(argv: list[str], stdout_text: str, stderr_text: str) -> None:
    root = Path.cwd()
    try:
        if not _location_trusted(root):
            _trace("no record (APM_HOME inside the project or cache)")
            return  # never create a key or record inside the project or cache
        lock_text = _read_nofollow(root / "apm.lock.yaml")
        if lock_text is None:
            _trace("no record (no lockfile)")
            return
        stdout_text, stamps = _ELAPSED_RE.subn(rf"\g<1>{_ELAPSED}\g<2>", stdout_text)
        templated = _template_ages(stdout_text)
        if templated is None:
            _trace("no record (a policy age without one cache entry)")
            return
        stdout_text = templated
        if stamps != 1 or _CLOCK_RE.search(stdout_text) or _CLOCK_RE.search(stderr_text):
            _trace("no record (clock-dependent output)")
            return  # O1: only the elapsed seconds may depend on the clock
        key = _key(create=True)
        if key is None:
            _trace("no record (no private key)")
            return
        run_env = sorted(
            name
            for name in _ENV_READS
            if isinstance(name, str) and name not in ENV_ALLOWLIST and name not in ENV_NOT_VERDICT
        )
        surfaces = _owner_surfaces(root)
        git_system = _git_system()
        digest = _Digest(key)
        _inputs(digest, root, argv, lock_text, run_env, surfaces, git_system)
        stderr_kept = "".join(
            line
            for line in stderr_text.splitlines(keepends=True)
            if "install fast path:" not in line
        )
        issued = time.time()
        record = {
            "v": RECORD_VERSION,
            "stdout": stdout_text,
            "stderr": stderr_kept,
            "exit": 0,
            "issued_at": issued,
            "expires_at": _expiry(issued),
            "run_env": run_env,
            "surfaces": surfaces,
            "git_system": git_system,
        }
        _body(digest, record)
        record["mac"] = digest.hexdigest()
        record["tags"] = digest.tags
        import tempfile

        path = _record_path(root)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".record-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        _trace("recorded")
    except (OSError, ValueError, TypeError) as exc:
        _trace(f"no record ({type(exc).__name__})")
        return
