"""Git repository fixture with tracked links, built without OS link privilege (#3457).

Links are committed as mode-120000 index entries (``update-index --cacheinfo``),
so the fixture builds identically on Linux, macOS and non-admin Windows. The
expected package tree is what a POSIX checkout yields when its links are
followed (``shutil.copytree`` without ``symlinks=True``); its content hash is
pinned as a golden value so every platform must reproduce the same bytes.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from apm_cli.utils.git_env import get_git_executable

FILES: dict[str, bytes] = {
    ".gitattributes": b"*.crlf text eol=crlf\n",
    "shared/ref.md": b"Shared reference\n",
    "shared/notes.crlf": b"line one\nline two\n",
    "shared/inner/deep.md": b"Deep file\n",
    "pkg/apm.yml": b"name: linked-pkg\nversion: 1.0.0\n",
    "pkg/CLAUDE.md": b"Package instructions\n",
    "pkg/tool.sh": b"#!/bin/sh\necho ok\n",
}
EXECUTABLE = {"pkg/tool.sh"}
LINKS: dict[str, str] = {
    "shared/inner/up.md": "../ref.md",
    "pkg/AGENTS.md": "CLAUDE.md",
    "pkg/chain.md": "AGENTS.md",
    "pkg/shared": "../shared",
    "pkg/run": "tool.sh",
    "pkg/notes.crlf": "../shared/notes.crlf",
}

# The package tree a POSIX install records: every contained link followed,
# ``eol=crlf`` targets converted exactly as Git checks them out.
EXPECTED_PKG: dict[str, bytes] = {
    "AGENTS.md": b"Package instructions\n",
    "CLAUDE.md": b"Package instructions\n",
    "apm.yml": b"name: linked-pkg\nversion: 1.0.0\n",
    "chain.md": b"Package instructions\n",
    "notes.crlf": b"line one\r\nline two\r\n",
    "run": b"#!/bin/sh\necho ok\n",
    "shared/inner/deep.md": b"Deep file\n",
    "shared/inner/up.md": b"Shared reference\n",
    "shared/notes.crlf": b"line one\r\nline two\r\n",
    "shared/ref.md": b"Shared reference\n",
    "tool.sh": b"#!/bin/sh\necho ok\n",
}
EXPECTED_PKG_EXECUTABLE = {"run", "tool.sh"}
GOLDEN_PKG_HASH = "sha256:b735a61a5d032a7a98add763d20db6d75706d0b56baf48ac557cf13dba587fb9"


def neutral_git_env() -> dict[str, str]:
    """Return an env that ignores the developer's and runner's Git config."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_CONFIG", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX"))
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="APM Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="APM Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    return env


def git(*args: str, env: dict[str, str] | None = None, input: str | None = None) -> str:
    return subprocess.run(
        [get_git_executable(), *args],
        env=env or neutral_git_env(),
        input=input,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def build_link_origin(root: Path, extra_links: dict[str, str] | None = None) -> tuple[Path, str]:
    """Commit :data:`FILES` and :data:`LINKS` (plus *extra_links*); return repo and SHA."""
    origin = root / "origin"
    git("init", "-q", "-b", "main", "--template=", str(origin))
    for relative, content in FILES.items():
        path = origin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    git("-C", str(origin), "add", *FILES)
    for relative in EXECUTABLE:
        git("-C", str(origin), "update-index", "--chmod=+x", relative)
    for relative, target in {**LINKS, **(extra_links or {})}.items():
        oid = git("-C", str(origin), "hash-object", "-w", "--stdin", input=target)
        git("-C", str(origin), "update-index", "--add", "--cacheinfo", f"120000,{oid},{relative}")
    git("-C", str(origin), "commit", "-q", "-m", "link fixture")
    return origin, git("-C", str(origin), "rev-parse", "HEAD")


def bare_clone(origin: Path, dest: Path) -> Path:
    git("clone", "-q", "--bare", "--template=", str(origin), str(dest))
    return dest


def tree_bytes(root: Path) -> dict[str, bytes]:
    """Map every regular file under *root* (no links, no ``.git``) to its bytes."""
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        assert not path.is_symlink(), f"{relative} is still an OS symlink"
        if path.is_file():
            found[relative] = path.read_bytes()
    return found


def executable_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.stat().st_mode & stat.S_IXUSR
    }


def host_creates_symlinks(scratch: Path) -> bool:
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "target.txt").write_bytes(b"probe")
    try:
        os.symlink(scratch / "target.txt", scratch / "link.txt")
    except OSError:
        return False
    return True
