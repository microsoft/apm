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


# -- containment once per directory (W3d containment-once K1-K9) ------------------


@pytest.mark.parametrize("relative", [*_PLAIN, *(pytest.param(r, marks=_LINKS) for r in _LINKED)])
def test_a_proven_directory_answers_exactly_as_pathlib(tree: Path, relative: str) -> None:
    from apm_cli.core.invocation_memo import invocation_scope

    path = tree / relative
    with invocation_scope():
        for _ in range(3):  # the first call proves, the rest take the one-lstat form
            for probe in (tree / "dir" / "sub", tree / "dir" / "file.md", path):
                assert resolve_path(probe) == probe.resolve(), probe


@_LINKS
def test_seeded_paths_match_realpath_inside_one_command(tree: Path) -> None:
    from apm_cli.core.invocation_memo import invocation_scope

    rng = random.Random(5377)  # noqa: S311 - a seeded test sequence
    with invocation_scope():
        for _ in range(800):
            parts = [rng.choice(_FUZZ_PARTS) for _ in range(rng.randint(1, 6))]
            text = str(tree) + "/" + "/".join(parts)
            assert str(resolve_path(Path(text))) == os.path.realpath(text), text


@_LINKS
def test_a_link_planted_by_user_code_is_seen_after_the_memo_drops(tree: Path) -> None:
    """K1/K9: user code mid-command clears every proof (forget_invocation_memos)."""
    from apm_cli.core.invocation_memo import forget_invocation_memos, invocation_scope

    target = tree / "dir" / "sub" / "deployed.md"
    with invocation_scope():
        assert resolve_path(target) == target.resolve()
        assert realpath.proven_link_free(tree / "dir" / "sub")
        (tree / "dir" / "sub").rename(tree / "dir" / "sub-moved")
        (tree / "dir" / "sub").symlink_to(tree / "outside", target_is_directory=True)
        forget_invocation_memos()
        assert not realpath.proven_link_free(tree / "dir" / "sub")
        assert resolve_path(target) == target.resolve() == tree / "outside" / "deployed.md"
        with pytest.raises(PathTraversalError):
            ensure_path_within(target, tree / "dir")


@_LINKS
def test_a_link_planted_before_the_command_is_never_proven(tree: Path) -> None:
    """K2/K9: a tree from an earlier run is proven by resolution, which sees its links."""
    from apm_cli.core.invocation_memo import invocation_scope

    with invocation_scope():
        for _ in range(2):
            assert resolve_path(tree / "dir" / "escape" / "x.md") == tree / "outside" / "x.md"
            assert not realpath.proven_link_free(tree / "dir" / "escape")
            with pytest.raises(PathTraversalError):
                ensure_path_within(tree / "dir" / "escape" / "x.md", tree / "dir")


@_LINKS
def test_a_leaf_link_under_a_proven_directory_is_still_found(tree: Path) -> None:
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.path_security import first_symlink_component

    with invocation_scope():
        assert resolve_path(tree / "dir" / "file.md") == tree / "dir" / "file.md"
        assert realpath.proven_link_free(tree / "dir")
        assert (
            first_symlink_component(tree, tree / "dir" / "link_file") == tree / "dir" / "link_file"
        )
        assert first_symlink_component(tree, tree / "dir" / "file.md") is None
        assert first_symlink_component(tree, tree / "dir" / "missing.md") is None


def test_outside_a_command_nothing_is_proven(tree: Path) -> None:
    assert resolve_path(tree / "dir" / "file.md") == (tree / "dir" / "file.md").resolve()
    assert not realpath.proven_link_free(tree / "dir")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reports the on-disk case")
def test_a_case_variant_leaf_resolves_to_the_disk_case_on_windows(tree: Path) -> None:
    from apm_cli.core.invocation_memo import invocation_scope

    with invocation_scope():
        for _ in range(2):
            probe = tree / "dir" / "FILE.MD"
            assert resolve_path(probe) == probe.resolve()


@_LINKS
def test_a_missing_directory_is_not_proven_before_it_exists(tree: Path) -> None:
    """R1: a later link at the not-yet-existing directory is still followed and found."""
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.path_security import first_symlink_component

    later = tree / "dir" / "later"
    leaf = later / "x.md"
    with invocation_scope():
        assert resolve_path(leaf) == leaf.resolve()
        assert not realpath.proven_link_free(later)
        later.symlink_to(tree / "outside", target_is_directory=True)
        for _ in range(2):
            assert resolve_path(leaf) == leaf.resolve() == tree / "outside" / "x.md"
            assert first_symlink_component(tree, leaf) == later
        with pytest.raises(PathTraversalError):
            ensure_path_within(leaf, tree / "dir")


@_LINKS
def test_apm_replacing_a_tree_drops_its_proofs(tree: Path) -> None:
    """R2: release_held (called before APM renames or deletes) forgets proofs below it."""
    import shutil

    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.realpath_win import release_held

    leaf = tree / "dir" / "sub" / "x.md"
    with invocation_scope():
        assert resolve_path(leaf) == leaf
        assert realpath.proven_link_free(tree / "dir" / "sub")
        release_held(tree / "dir" / "sub")
        assert not realpath.proven_link_free(tree / "dir" / "sub")
        assert realpath.proven_link_free(tree / "dir")
        shutil.rmtree(tree / "dir" / "sub")
        (tree / "dir" / "sub").symlink_to(tree / "outside", target_is_directory=True)
        assert resolve_path(leaf) == leaf.resolve() == tree / "outside" / "x.md"


@pytest.mark.parametrize("volatile", [".git", "pkg.inc.0a1b2c3d", "x.tmp", "staging"])
def test_nothing_under_a_volatile_component_is_proven(tree: Path, volatile: str) -> None:
    """R3: APM renames and deletes there."""
    from apm_cli.core.invocation_memo import invocation_scope

    directory = tree / "dir" / volatile
    directory.mkdir()
    with invocation_scope():
        for _ in range(2):
            assert resolve_path(directory / "x.md") == (directory / "x.md").resolve()
        assert not realpath.proven_link_free(directory)
