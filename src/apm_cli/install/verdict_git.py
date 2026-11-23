"""Git config keying for the verified-state record (#4977).

A no-op's ``git remote`` reads git config at every scope; a hit runs no git, so
the record keys the bytes of every file git would read. Split out of
``install/verdict_record.py`` verbatim (install LOC budget).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from apm_cli.install.verdict_record import _Digest, _env, _env_prefixed

_GIT_TRUE = frozenset({"1", "true", "yes", "on"})


def _git_files(root: Path, system: str) -> list[Path]:
    """The config files a child ``git`` reads for this checkout, at every scope.

    The no-op runs ``git remote`` for policy discovery and it honors
    ``url.insteadOf``; a hit runs no git, so it keys what git would read: the
    repository config (``GIT_DIR``/``GIT_COMMON_DIR``, else found upward with a
    ``.git`` file followed to its gitdir and common dir), the global config
    (``GIT_CONFIG_GLOBAL``, else ``~/.gitconfig`` and the XDG file) and *system*,
    the path git itself reported (``git var GIT_CONFIG_SYSTEM``) for the binary
    keyed beside it, each followed through its includes. ``GIT_*`` and PATH are
    keyed as values too.
    """
    env = dict(_env_prefixed("GIT_"))
    cwd = Path.cwd()
    files: list[Path] = []
    if env.get("GIT_DIR"):
        gitdir = cwd / env["GIT_DIR"]
        common = cwd / env["GIT_COMMON_DIR"] if env.get("GIT_COMMON_DIR") else gitdir
        files += [gitdir / "config", gitdir / "config.worktree", common / "config"]
        files += [gitdir / "HEAD", gitdir / "commondir"]
    else:
        for base in (root, *root.parents):
            dot = base / ".git"
            if dot.is_dir():
                # HEAD names the branch an integration records (copilot_app_project).
                files += [dot / "config", dot / "config.worktree", dot / "HEAD", dot / "commondir"]
                break
            if dot.is_file():
                text = dot.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    break
                gitdir = (base / text[len("gitdir:") :].strip()).resolve()
                files += [dot, gitdir / "config", gitdir / "config.worktree", gitdir / "HEAD"]
                common = gitdir / "commondir"
                files.append(common)
                if common.is_file():
                    files.append((gitdir / common.read_text(encoding="utf-8").strip()) / "config")
                break
    if env.get("GIT_COMMON_DIR"):
        files.append(cwd / env["GIT_COMMON_DIR"] / "config")  # it overrides commondir too
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


_SECTION_RE = re.compile(rb"^\s*\[\s*([A-Za-z][A-Za-z0-9.-]*)", re.MULTILINE)
_INCLUDE_PATH_RE = re.compile(rb"^\s*path\s*=\s*(.*?)\s*$", re.IGNORECASE)
_GIT_INCLUDE_LIMIT = 64
_INCLUDE_UNPARSED = re.compile(rb"[;#\\]|%\(")


def _git_includes(config: Path, data: bytes) -> list[Path]:
    """Files an ``[include]`` or ``[includeIf]`` section of *config* names.

    Every condition counts as met, a superset of what git reads, so a change in
    any file git might include misses.
    """
    found: list[Path] = []
    section = b""
    for line in data.splitlines():
        header = _SECTION_RE.match(line)
        if header is not None:
            section = header.group(1).lower()
            if section.startswith(b"include") and (
                line.count(b"]") != 1 or line[line.index(b"]") + 1 :].strip()
            ):
                raise ValueError("git include: an ambiguous section line")
            continue
        if not section.startswith(b"include"):
            continue
        match = _INCLUDE_PATH_RE.match(line)
        if match is None:
            continue
        raw = match.group(1)
        inner = raw[1:-1] if len(raw) > 1 and raw[:1] == raw[-1:] == b'"' else raw
        if (
            b'"' in inner
            or _INCLUDE_UNPARSED.search(raw)
            or (os.name == "nt" and raw.lstrip(b'"').startswith(b"~"))
        ):
            # Comments, escapes, continuations, %(prefix)/ and Windows ~ are git syntax
            # this reader does not parse: fail closed rather than under-key (W3d R3).
            raise ValueError("git include: unparsed value syntax")
        value = raw.strip(b'"').decode("utf-8", "surrogateescape")
        target = Path(os.path.expanduser(value))
        found.append(target if target.is_absolute() else config.parent / target)
    return found


def _git_config_bytes(digest: _Digest, root: Path, git_system: str) -> None:
    """Every git config file a child git may read, followed through its includes."""
    from apm_cli.install.verdict_inputs import _kind

    for base in (root, *root.parents):
        # Discovery also takes a bare repository (its HEAD) in each directory it walks.
        digest.put(f"git-head:{base}", _kind(str(base / "HEAD")))
        if os.path.lexists(base / ".git"):
            for name in ("objects", "refs"):  # GitPython's repository test
                digest.put(f"git-dir:{base}/{name}", _kind(str(base / ".git" / name)))
            break
    queue, seen = list(_git_files(root, git_system)), set()
    while queue:
        path = queue.pop(0)
        if str(path) in seen:
            continue
        seen.add(str(path))
        if len(seen) > _GIT_INCLUDE_LIMIT:
            raise ValueError("git config includes")
        # Keyed bytes only, never trusted content: a dotfile symlink is followed.
        try:
            data: bytes | None = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            data = None
        digest.put(f"git:{path}", b"\x00absent" if data is None else b"\x01" + data)
        if data is not None:
            queue.extend(_git_includes(path, data))
