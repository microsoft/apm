"""U6: the in-process attribute engine agrees with ``git check-attr`` (W3d w3d:ruling:lever-s-t1prime).

Seeded trees of ``.gitattributes`` files (anchored and basename patterns, ``**``,
character classes, deeper overrides, last-line-wins, set/unset/unspecified/value,
the ``binary`` macro, ``dir/`` patterns git skips, case variants, CRLF lines) are
evaluated by ``checkout_verify``'s engine, as the sparse fetch and the verifier use it,
and by ``git check-attr -a`` in the same commit: case-sensitively against
``core.ignorecase=false`` and case-folded against ``core.ignorecase=true``.
A disagreement fixes the engine, and with it the verifier.
"""

from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache import inprocess_fetch as fetch
from apm_cli.cache.checkout_verify import _Attributes, _FoldedAttributes, _Tree

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-git-transport-selection")]

_DIRS = ["", "a", "a/b", "a/b/c", "docs", "Docs", "x/y"]
_NAMES = [
    "run.bat",
    "RUN.BAT",
    "read.md",
    "README.MD",
    "x.txt",
    "y.c",
    "z.h",
    "data.bin",
    "noext",
    "a.Md",
]
_PATTERNS = [
    "*",
    "*.md",
    "*.MD",
    "*.bat",
    "*.[ch]",
    "*.txt",
    "x.txt",
    "/x.txt",
    "a/*.md",
    "a/**/*.txt",
    "**/c/*",
    "docs/**",
    "b/*.bat",
    "run.*",
    "?.c",
    "noext",
    "data.*",
    "a/",
    "*.Md",
]
_TOKENS = [
    "text",
    "-text",
    "!text",
    "text=auto",
    "eol=lf",
    "eol=crlf",
    "binary",
    "diff",
    "-diff",
    "merge=union",
    "linguist-generated=true",
    "linguist-vendored",
    "filter=lfs",
    "ident",
    "-ident",
    "delta",
    "export-ignore",
]
_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]


# No system or global config: a host's git-lfs filter would store a .gitattributes
# that names filter=lfs as an LFS pointer, so the commit would not hold the rules.
_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        [*_GIT, *args], cwd=cwd, input=stdin, capture_output=True, check=True, env=_ENV
    ).stdout


def _seeded_repo(tmp_path: Path, seed: int) -> tuple[Path, str, list[str]]:
    rng = random.Random(seed)  # noqa: S311 -- seeded test data, not security
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "core.autocrlf", "false")
    paths = sorted({f"{d}/{n}" if d else n for d in _DIRS for n in _NAMES if rng.random() < 0.35})
    for rel in paths:
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(b"x\n")
    for directory in rng.sample(_DIRS, k=rng.randint(1, 3)):
        lines = []
        for _ in range(rng.randint(1, 6)):
            pattern = rng.choice(_PATTERNS)
            tokens = rng.sample(_TOKENS, k=rng.randint(1, 3))
            lines.append(f"{pattern} {' '.join(tokens)}")
        ending = "\r\n" if rng.random() < 0.2 else "\n"
        folder = src / directory if directory else src
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".gitattributes").write_bytes((ending.join(lines) + ending).encode())
    _git(src, "-c", "core.ignorecase=false", "add", "-A")
    _git(src, "commit", "-q", "-m", "seed")
    files = [line.decode() for line in _git(src, "ls-files", "-z").split(b"\0") if line]
    return src, _git(src, "rev-parse", "HEAD").decode().strip(), files


def _git_state(src: Path, files: list[str], ignorecase: bool) -> dict[str, dict[str, object]]:
    out = _git(
        src,
        "-c",
        f"core.ignorecase={'true' if ignorecase else 'false'}",
        "check-attr",
        "-a",
        "--stdin",
        "-z",
        stdin=b"\0".join(f.encode() for f in files) + b"\0",
    )
    fields = out.split(b"\0")
    states: dict[str, dict[str, object]] = {f: {} for f in files}
    for i in range(0, len(fields) - 2, 3):
        path, name, value = (part.decode() for part in fields[i : i + 3])
        states[path][name] = {"set": True, "unset": False}.get(value, value)
    return states


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("ignorecase", [False, True])
def test_the_engine_agrees_with_git_check_attr(tmp_path: Path, seed: int, ignorecase: bool) -> None:
    src, sha, files = _seeded_repo(tmp_path, seed)
    pack = _git(src, "pack-objects", "--stdout", "--revs", stdin=f"{sha}\n".encode())
    objects = fetch.parse_pack(pack)
    source = fetch._PlannedObjects(objects)
    root = objects[sha][1].split(b"\n", 1)[0][5:].decode("ascii")
    engine_class = _FoldedAttributes if ignorecase else _Attributes
    engine = engine_class(_Tree(source, root, 20), source)
    theirs = _git_state(src, files, ignorecase)
    checked = 0
    for path in files:
        if path.rpartition("/")[2] == ".gitattributes":
            continue
        state = engine.state(path)
        assert state is not None, path
        ours = {name: value for name, value in state.items() if value is not None}
        assert ours == theirs[path], (seed, ignorecase, path)
        checked += 1
    assert checked >= 5
