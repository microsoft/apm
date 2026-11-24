"""Read the effective git config once per command for clone environments.

A clone's network environment used to come from a throwaway probe repository
(``git init`` + ``git remote add origin``), and each extraHeader lookup asked
``git config --get-urlmatch``. Both are exact but cost spawns per dependency.

* :func:`clone_env` returns the clone environment without the probe repository
  when it cannot differ: a fresh ``git init --no-template`` plus ``remote add
  origin`` writes only ``core.*`` and ``remote.origin.*`` locally, and the
  network env keeps only ``http.*`` and ``url.*.insteadOf`` from local scope.
  Any include or includeIf (which may select files by repository) or any
  local/worktree entry (the command runs inside a repository) keeps the probe.
* :func:`header_group` evaluates Git's ``http.<url>.extraHeader`` urlmatch in
  process for plain http(s) URLs; any other shape returns ``None`` and the
  caller spawns as before.

Both read the command's memoized config snapshots (``git_config_memo``) and
create no cache. Under ``APM_DEBUG`` each answer is compared with the spawned
one and a mismatch raises (the differential).
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from apm_cli.utils.git_env import GitConfigEntry, _GitConfigSnapshot

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNSUPPORTED_URL_CHARS = frozenset("%*?#@" + chr(92))
# Set while the differential asks the spawned path for its answer.
_SPAWN_ONLY: list[bool] = []


def urlmatch_first(spawned: Callable) -> Callable:
    """Decorate Git's urlmatch spawn: answer plain URLs in process first."""

    @functools.wraps(spawned)
    def urlmatched(remote_url: str, headers: Sequence[GitConfigEntry], env: dict[str, str]):
        if headers and (fast := header_group(remote_url, headers, env)) is not None:
            return fast
        return spawned(remote_url, headers, env)

    return urlmatched


def config_once_first(probed: Callable) -> Callable:
    """Decorate the probe-repository clone env: skip the probe when it cannot differ."""

    @functools.wraps(probed)
    def clone(
        remote_url: str, overrides: dict[str, object] | None, target: Path, *, bare: bool = False
    ) -> dict[str, str]:
        fresh = not ((target if bare else target / ".git") / "config").exists()
        if fresh and (once := clone_env(remote_url, overrides, target, bare)) is not None:
            return once
        return probed(remote_url, overrides, target, bare=bare)

    return clone


def _differential() -> bool:
    return bool(os.environ.get("APM_DEBUG"))


def _simple_http_url(url: str) -> tuple[str, str, int, str] | None:
    """``(scheme, host, port, path)`` for a plain http(s) URL, else ``None``."""
    if any(ch in _UNSUPPORTED_URL_CHARS or ord(ch) < 0x21 or ord(ch) > 0x7E for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if port == 0:
        return None  # Git rejects port 0: let it decide
    if scheme not in _DEFAULT_PORTS or not parts.hostname or parts.username or parts.password:
        return None
    path = parts.path or "/"
    if any(seg in {".", ".."} for seg in path.split("/")) or "//" in path:
        return None
    return scheme, parts.hostname.lower(), port or _DEFAULT_PORTS[scheme], path


def inprocess_header_group(
    remote_url: str, headers: Sequence[GitConfigEntry]
) -> tuple[GitConfigEntry, ...] | None:
    """Git's ``http.<url>.extraHeader`` urlmatch for plain URLs, else ``None``.

    The generic ``http.extraheader`` scores lowest; a URL key matches when its
    scheme, host and port are equal (default ports normalized) and its path is
    a prefix at a ``/`` boundary, and the longest path wins. User info,
    wildcards, percent-encoding, dot segments, ties between two spellings and
    case-folded spellings of one key return ``None`` (Git decides).
    """
    target = _simple_http_url(remote_url)
    if target is None:
        return None
    best: tuple[int, int] | None = None
    best_keys: set[str] = set()
    for entry in headers:
        lowered = entry.key.lower()
        if lowered == "http.extraheader":
            score = (0, 0)
        else:
            if not (lowered.startswith("http.") and lowered.endswith(".extraheader")):
                return None
            config = _simple_http_url(entry.key[5 : -len(".extraheader")])
            if config is None:
                return None
            if config[:3] != target[:3]:
                continue
            base = config[3].rstrip("/")
            url_path = target[3]
            if not (url_path.rstrip("/") == base or url_path.startswith(base + "/")):
                continue
            score = (1, len(base))
        if best is None or score > best:
            best, best_keys = score, {lowered}
        elif score == best:
            best_keys.add(lowered)
    if best is None:
        return ()
    if len(best_keys) != 1:
        return None  # two spellings of one URL: let Git decide
    (selected,) = best_keys
    group = tuple(entry for entry in headers if entry.key.lower() == selected)
    if len({entry.key for entry in group}) != 1:
        return None  # case-folded spellings of one key (path case): let Git decide
    return group


def header_group(
    remote_url: str, headers: Sequence[GitConfigEntry], env: dict[str, str]
) -> tuple[GitConfigEntry, ...] | None:
    """The extraHeader group without a spawn, or ``None`` when Git must decide."""
    if _SPAWN_ONLY:
        return None
    fast = inprocess_header_group(remote_url, headers)
    if fast is not None and _differential():
        from apm_cli.utils import git_env

        if _spawned(git_env._urlmatched_header_group, remote_url, headers, env) != fast:
            raise git_env.GitUrlRewriteProbeError("git config-once: urlmatch differs from git")
    return fast


def needs_probe_repository(snapshot: _GitConfigSnapshot) -> bool:
    """Whether the repository-less snapshot may differ from a probe repository's."""
    for entry in snapshot.entries:
        if entry.scope in {"local", "worktree"}:
            return True
        key = entry.key.lower()
        if key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
            return True
    return False


def clone_env(
    remote_url: str, overrides: dict[str, object] | None, target: Path, bare: bool
) -> dict[str, str] | None:
    """The clone env without a throwaway probe repository, or ``None`` to probe."""
    if _SPAWN_ONLY or (target.exists() and any(target.iterdir())):
        return None
    from apm_cli.utils import git_env

    envs = (git_env.git_subprocess_env(), git_env.git_subprocess_env(overrides))
    for env in envs:
        for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
            if env.get(name) and not Path(env[name]).is_absolute():
                return None  # Git resolves it from the probe's directory, not ours
    try:
        parent = git_env._read_effective_git_config(envs[0])
        child = git_env._read_effective_git_config(envs[1])
    except (git_env.GitUrlRewriteProbeError, ValueError):
        return None
    if needs_probe_repository(parent) or needs_probe_repository(child):
        return None
    once = git_env.git_network_env(remote_url, overrides, git_dir=None)
    if _differential():
        full = _spawned(git_env.git_clone_env, remote_url, overrides, target, bare=bare)
        if full != once:
            raise git_env.GitUrlRewriteProbeError("git config-once: clone env differs from probe")
    return once


def _spawned(function, *args, **kwargs):
    """Run *function* with the config-once shortcuts off (the differential's reference)."""
    _SPAWN_ONLY.append(True)
    try:
        return function(*args, **kwargs)
    finally:
        _SPAWN_ONLY.pop()
