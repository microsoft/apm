"""``resolve_path`` returns exactly what ``Path.resolve()`` returns.

Each cell builds a real tree (plain, missing, links, a loop, a file used as a
directory) and compares the kernel-backed result with pathlib's own. On Linux
the fast form is taken; elsewhere the helper is ``Path.resolve()`` itself.
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

from apm_cli.utils import realpath
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.realpath import resolve_path

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-portability.byte_deterministic")]

_LINKS = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "dir" / "sub").mkdir(parents=True)
    (root / "dir" / "file.md").write_text("x", encoding="utf-8")
    (root / "outside").mkdir()
    if sys.platform != "win32":
        (root / "link_dir").symlink_to(root / "dir", target_is_directory=True)
        (root / "dir" / "link_file").symlink_to(root / "dir" / "file.md")
        (root / "dangling").symlink_to(root / "nowhere" / "target")
        (root / "loop_a").symlink_to(root / "loop_b")
        (root / "loop_b").symlink_to(root / "loop_a")
        (root / "dir" / "escape").symlink_to(root / "outside", target_is_directory=True)
    return root


_PLAIN = [
    "dir",
    "dir/file.md",
    "dir/sub",
    "dir/missing.md",
    "dir/missing/deeper/x.md",
    "dir/file.md/under-file",
]
_LINKED = [
    "link_dir",
    "link_dir/file.md",
    "link_dir/sub/../file.md",
    "dir/link_file",
    "dangling",
    "dangling/child",
    "dir/escape/new.md",
    "link_dir/missing/x.md",
]


@pytest.mark.parametrize("relative", [*_PLAIN, *(pytest.param(r, marks=_LINKS) for r in _LINKED)])
def test_resolve_path_matches_pathlib(tree: Path, relative: str) -> None:
    candidate = tree / relative
    assert resolve_path(candidate) == candidate.resolve()


@_LINKS
def test_a_subclass_resolves_its_own_filesystem_path(tree: Path) -> None:
    class Elsewhere(type(tree)):
        def __fspath__(self) -> str:
            return str(tree / "dir")

    candidate = Elsewhere(tree / "link_dir")
    assert resolve_path(candidate) == candidate.resolve() == (tree / "dir").resolve()


@_LINKS
def test_a_symlink_loop_ends_as_pathlib_ends_it(tree: Path) -> None:
    """Python 3.12 raises RuntimeError here; 3.13+ returns a path. Match either."""

    def outcome(resolve):
        try:
            return resolve(tree / "loop_a")
        except (OSError, RuntimeError) as exc:
            return type(exc)

    assert outcome(resolve_path) == outcome(Path.resolve)


def test_a_relative_path_matches_pathlib(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tree)
    assert resolve_path(Path("dir/file.md")) == Path("dir/file.md").resolve()


@_LINKS
def test_a_link_out_of_the_base_is_still_refused(tree: Path) -> None:
    with pytest.raises(PathTraversalError):
        ensure_path_within(tree / "dir" / "escape" / "new.md", tree / "dir")


@pytest.mark.skipif(not realpath._FAST, reason="kernel form is Linux-only")
def test_linux_resolves_in_the_kernel(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    linked, missing = tree / "link_dir" / "file.md", tree / "link_dir" / "new" / "x.md"
    expected = (linked.resolve(), missing.resolve())

    def no_python_walk(*_args, **_kwargs):
        raise AssertionError("posixpath.realpath must not run")

    monkeypatch.setattr(os.path, "realpath", no_python_walk)
    assert (resolve_path(linked), resolve_path(missing)) == expected


_FUZZ_PARTS = [
    "dir",
    "sub",
    "file.md",
    "missing",
    "..",
    ".",
    "link_dir",
    "link_file",
    "dangling",
    "escape",
    "x",
]


@_LINKS
def test_seeded_paths_match_posix_realpath(tree: Path) -> None:
    rng = random.Random(4939)  # noqa: S311 - a seeded test sequence
    for _ in range(400):
        parts = [rng.choice(_FUZZ_PARTS) for _ in range(rng.randint(1, 6))]
        text = str(tree) + "/" + "/".join(parts)
        assert str(resolve_path(Path(text))) == os.path.realpath(text), text


@_LINKS
def test_without_proc_the_answer_is_pathlib(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_proc(*_args, **_kwargs):
        raise FileNotFoundError("/proc is not mounted")

    candidate = tree / "dir" / "sub" / "file.md"
    expected = candidate.resolve()
    monkeypatch.setattr(os, "readlink", no_proc)
    assert resolve_path(candidate) == expected


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_a_fifo_leaf_resolves_without_blocking(tree: Path) -> None:
    fifo = tree / "dir" / "pipe"
    os.mkfifo(fifo)
    assert resolve_path(fifo) == fifo.resolve()
