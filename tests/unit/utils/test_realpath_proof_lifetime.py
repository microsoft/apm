"""Red-team cells (RT6): how long a #5382 link-free proof may live.

A proof says "no link on this directory's path" for one command. These cells try
to keep one alive past the moment it stops being true: a nested link recreated by
an APM copy helper, user code racing a parked publish, and a directory swapped
for a new inode at the same path. Each cell runs the proof form (``k_applies``)
on any POSIX runner and checks the answers against pathlib and containment.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from apm_cli.utils import realpath
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    first_symlink_component,
)
from apm_cli.utils.realpath import resolve_path

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-LAW-portability.byte_deterministic"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks; Windows keeps no proofs"),
]


@pytest.fixture
def k_applies():
    """K runs where no kernel form does: refuse the kernel form for this process."""
    realpath._reset_proof()
    realpath._proc_state["refused"] = True
    yield
    realpath._reset_proof()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "dir" / "sub" / "inner").mkdir(parents=True)
    (root / "outside").mkdir()
    return root


def _expect_refused(tree: Path, leaf: Path, link: Path) -> None:
    assert resolve_path(leaf) == leaf.resolve() == tree / "outside" / leaf.name
    assert first_symlink_component(tree, leaf) == link
    with pytest.raises(PathTraversalError):
        ensure_path_within(leaf, tree / "dir")


@pytest.mark.usefixtures("k_applies")
def test_a_directory_copied_with_a_nested_link_drops_the_nested_proof(tree: Path) -> None:
    """R4 for a nested link: robust_copy_entry(dir) recreates links through copytree.

    The removal is unreleased, as in test_realpath's copied-link cell: the link
    creator alone must drop the proof. Here the link is one level down.
    """
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.file_ops import robust_copy_entry

    sub, inner = tree / "dir" / "sub", tree / "dir" / "sub" / "inner"
    leaf = inner / "x.md"
    staged = tree / "dir" / "package-copy"
    staged.mkdir()
    (staged / "inner").symlink_to(tree / "outside", target_is_directory=True)
    with invocation_scope():
        assert resolve_path(leaf) == leaf
        assert realpath.proven_link_free(inner)
        shutil.rmtree(sub)  # unreleased: only the link creator can drop the proof
        robust_copy_entry(staged, sub)
        assert inner.is_symlink()
        assert not realpath.proven_link_free(inner)
        _expect_refused(tree, leaf, inner)


@pytest.mark.usefixtures("k_applies")
def test_a_nested_link_copied_over_a_proven_directory_is_still_contained(tree: Path) -> None:
    """The consequence of the cell above: answers, link scan and containment (no proof asserts)."""
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.file_ops import robust_copy_entry

    sub, inner = tree / "dir" / "sub", tree / "dir" / "sub" / "inner"
    leaf = inner / "x.md"
    staged = tree / "dir" / "package-copy"
    staged.mkdir()
    (staged / "inner").symlink_to(tree / "outside", target_is_directory=True)
    with invocation_scope():
        assert resolve_path(leaf) == leaf
        shutil.rmtree(sub)  # unreleased: only the link creator can drop the proof
        robust_copy_entry(staged, sub)
        assert inner.is_symlink()
        _expect_refused(tree, leaf, inner)


@pytest.mark.parametrize("forget", ["forget_invocation_memos", "release_held", "forget_proofs"])
@pytest.mark.usefixtures("k_applies")
def test_a_publish_parked_across_a_forget_and_a_swap_never_lands(tree: Path, forget: str) -> None:
    """G3: the resolve observed ``sub`` before user code swapped it for a link.

    Every forget entry point must void a publish captured before it, including
    the one that orphans the memo dict (forget_invocation_memos).
    """
    from apm_cli.core import invocation_memo
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils import realpath_win

    forgetters = {
        "forget_invocation_memos": invocation_memo.forget_invocation_memos,
        "release_held": realpath_win.release_held,
        "forget_proofs": realpath.forget_proofs,
    }
    sub, leaf = tree / "dir" / "sub", tree / "dir" / "sub" / "x.md"
    observed, swapped = threading.Event(), threading.Event()

    def park() -> None:
        if not observed.is_set():
            observed.set()
            assert swapped.wait(10)

    with invocation_scope():
        assert resolve_path(tree / "dir" / "file.md") == tree / "dir" / "file.md"
        realpath._before_publish.append(park)
        try:
            worker = threading.Thread(target=resolve_path, args=(leaf,))
            worker.start()
            assert observed.wait(10)
            sub.rename(tree / "dir" / "sub-moved")  # user code: no release
            sub.symlink_to(tree / "outside", target_is_directory=True)
            forgetters[forget]()
            swapped.set()
            worker.join(10)
        finally:
            realpath._before_publish.remove(park)
        assert not worker.is_alive()
        assert not realpath.proven_link_free(sub)
        _expect_refused(tree, leaf, sub)


@pytest.mark.usefixtures("k_applies")
def test_a_swapped_in_directory_answers_as_pathlib_at_every_depth(tree: Path) -> None:
    """Same path, new inode: native plugin deploy renames a staged tree over a proven one.

    The swap is a raw rename (no release). The proof survives only if the new tree
    is link-free wherever the old one was proven; any link must still be seen.
    """
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.native_plugins.layout import NativeLayoutError, deploy_plugin

    root = tree / "dir"
    name = "sub"
    deployed = root / name
    (deployed / "inner" / "old.md").write_text("old", encoding="utf-8")
    source = tree / "source"
    (source / "inner").mkdir(parents=True)
    (source / "inner" / "new.md").write_text("new", encoding="utf-8")
    (tree / "outside" / "skill.md").write_text("out", encoding="utf-8")
    (source / "linked").symlink_to(tree / "outside", target_is_directory=True)
    probes = [
        deployed / "inner" / "new.md",
        deployed / "inner" / "x.md",
        deployed / "linked" / "skill.md",
        deployed / "linked" / "x.md",
    ]
    with invocation_scope():
        for probe in (deployed / "inner" / "old.md", deployed / "x.md"):
            assert resolve_path(probe) == probe
        assert realpath.proven_link_free(deployed / "inner")
        try:
            deploy_plugin(root, name, source, "fp", lambda _p: "fp")
        except (NativeLayoutError, PathTraversalError) as exc:  # refusing the link is safe
            pytest.skip(f"deploy refused the source: {type(exc).__name__}")
        for _ in range(2):
            for probe in probes:
                assert str(resolve_path(probe)) == os.path.realpath(probe), probe
                assert (first_symlink_component(tree, probe) is None) == (
                    not any(p.is_symlink() for p in [probe, *probe.parents] if tree in p.parents)
                ), probe
