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


@pytest.fixture
def k_applies():
    """K runs where no kernel form does: refuse the kernel form for this process."""
    realpath._reset_proof()
    realpath._proc_state["refused"] = True
    yield
    realpath._reset_proof()


@pytest.mark.parametrize("relative", [*_PLAIN, *(pytest.param(r, marks=_LINKS) for r in _LINKED)])
@pytest.mark.usefixtures("k_applies")
def test_a_proven_directory_answers_exactly_as_pathlib(tree: Path, relative: str) -> None:
    from apm_cli.core.invocation_memo import invocation_scope

    path = tree / relative
    with invocation_scope():
        for _ in range(3):  # the first call proves, the rest take the one-lstat form
            for probe in (tree / "dir" / "sub", tree / "dir" / "file.md", path):
                assert resolve_path(probe) == probe.resolve(), probe


@_LINKS
@pytest.mark.usefixtures("k_applies")
def test_seeded_paths_match_realpath_inside_one_command(tree: Path) -> None:
    from apm_cli.core.invocation_memo import invocation_scope

    rng = random.Random(5377)  # noqa: S311 - a seeded test sequence
    with invocation_scope():
        for _ in range(800):
            parts = [rng.choice(_FUZZ_PARTS) for _ in range(rng.randint(1, 6))]
            text = str(tree) + "/" + "/".join(parts)
            assert str(resolve_path(Path(text))) == os.path.realpath(text), text


@_LINKS
@pytest.mark.usefixtures("k_applies")
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
@pytest.mark.usefixtures("k_applies")
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
@pytest.mark.usefixtures("k_applies")
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


@pytest.mark.usefixtures("k_applies")
def test_outside_a_command_nothing_is_proven(tree: Path) -> None:
    assert resolve_path(tree / "dir" / "file.md") == (tree / "dir" / "file.md").resolve()
    assert not realpath.proven_link_free(tree / "dir")


@pytest.mark.skipif(
    not (sys.platform == "win32" or realpath.kernel_resolves()),
    reason="only where a fast platform form applies",
)
def test_a_platform_with_a_fast_form_keeps_no_proofs(tree: Path) -> None:
    """Linux (kernel forms) and Windows (exact forms): K is not applied, nothing is proven."""
    from apm_cli.core.invocation_memo import invocation_scope

    leaf = tree / "dir" / "file.md"
    with invocation_scope():
        for _ in range(3):
            assert resolve_path(leaf) == leaf.resolve()
        assert not realpath.proven_link_free(tree / "dir")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reports the on-disk case")
@pytest.mark.usefixtures("k_applies")
def test_a_case_variant_leaf_resolves_to_the_disk_case_on_windows(tree: Path) -> None:
    from apm_cli.core.invocation_memo import invocation_scope

    with invocation_scope():
        for _ in range(2):
            probe = tree / "dir" / "FILE.MD"
            assert resolve_path(probe) == probe.resolve()


@_LINKS
@pytest.mark.usefixtures("k_applies")
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
@pytest.mark.usefixtures("k_applies")
def test_apm_replacing_a_tree_drops_its_proofs(tree: Path) -> None:
    """R2: release_held (called before APM renames or deletes) forgets proofs below it."""
    import shutil

    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.realpath_win import release_held

    leaf = tree / "dir" / "sub" / "x.md"
    with invocation_scope():
        assert resolve_path(tree / "dir" / "file.md") == tree / "dir" / "file.md"
        assert resolve_path(leaf) == leaf
        assert realpath.proven_link_free(tree / "dir" / "sub")
        release_held(tree / "dir" / "sub")
        assert not realpath.proven_link_free(tree / "dir" / "sub")
        assert not realpath.proven_link_free(tree / "dir")  # every proof goes (aliases)
        shutil.rmtree(tree / "dir" / "sub")
        (tree / "dir" / "sub").symlink_to(tree / "outside", target_is_directory=True)
        assert resolve_path(leaf) == leaf.resolve() == tree / "outside" / "x.md"


@_LINKS
@pytest.mark.usefixtures("k_applies")
def test_a_mutation_spelled_through_an_alias_drops_the_proof(tree: Path) -> None:
    """``/root/link_dir/sub`` names ``/root/dir/sub``: a prefix match would keep its proof."""
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.integration import write_seam

    sub, leaf = tree / "dir" / "sub", tree / "dir" / "sub" / "x.md"
    with invocation_scope():
        assert resolve_path(leaf) == leaf
        write_seam.rmtree(tree / "link_dir" / "sub")
        sub.symlink_to(tree / "outside", target_is_directory=True)
        _expect_link_refused(tree, leaf)


@_LINKS
@pytest.mark.usefixtures("k_applies")
def test_a_release_racing_a_resolve_is_never_undone_by_its_late_publish(tree: Path) -> None:
    """The resolve observed ``dir/sub`` as a real directory; before it publishes, APM
    releases and replaces it with a link. The stale proof must not be recorded."""
    import shutil

    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.realpath_win import release_held

    sub, leaf = tree / "dir" / "sub", tree / "dir" / "sub" / "x.md"
    fired: list[bool] = []

    def swap() -> None:
        if fired:
            return
        fired.append(True)
        release_held(sub)
        shutil.rmtree(sub)
        sub.symlink_to(tree / "outside", target_is_directory=True)

    with invocation_scope():
        realpath._before_publish.append(swap)
        try:
            resolve_path(leaf)
        finally:
            realpath._before_publish.remove(swap)
        assert fired and not realpath.proven_link_free(sub)
        _expect_link_refused(tree, leaf)


def _swap_once(sub: Path, target: Path, fired: list) -> None:
    import shutil

    from apm_cli.utils.realpath_win import release_held

    if fired:
        return
    fired.append(True)
    release_held(sub)
    shutil.rmtree(sub)
    sub.symlink_to(target, target_is_directory=True)


@_LINKS
@pytest.mark.usefixtures("k_applies")
def test_a_subdirectory_proof_racing_a_release_is_dropped(tree: Path) -> None:
    """_proven_answer's own publish site: a directory under a proven one."""
    from apm_cli.core.invocation_memo import invocation_scope

    sub = tree / "dir" / "sub"
    fired: list[bool] = []

    def swap() -> None:
        _swap_once(sub, tree / "outside", fired)

    with invocation_scope():
        assert resolve_path(tree / "dir" / "file.md") == tree / "dir" / "file.md"
        realpath._before_publish.append(swap)
        try:
            resolve_path(sub)
        finally:
            realpath._before_publish.remove(swap)
        assert fired and not realpath.proven_link_free(sub)
        _expect_link_refused(tree, sub / "x.md")


@_LINKS
@pytest.mark.usefixtures("k_applies")
def test_a_worker_resolve_parked_at_publish_loses_to_a_release_on_another_thread(
    tree: Path,
) -> None:
    """Cross-thread: APM's workers share the proof memo (W3d T1/T2), no sleeps."""
    import threading

    from apm_cli.core.invocation_memo import invocation_scope

    sub, leaf = tree / "dir" / "sub", tree / "dir" / "sub" / "x.md"
    observed, released = threading.Event(), threading.Event()

    def park() -> None:
        if not observed.is_set():
            observed.set()
            assert released.wait(10)

    with invocation_scope():
        realpath._before_publish.append(park)
        try:
            worker = threading.Thread(target=resolve_path, args=(leaf,))
            worker.start()
            assert observed.wait(10)
            _swap_once(sub, tree / "outside", [])
            released.set()
            worker.join(10)
        finally:
            realpath._before_publish.remove(park)
        assert not worker.is_alive() and not realpath.proven_link_free(sub)
        _expect_link_refused(tree, leaf)


@_LINKS
@pytest.mark.usefixtures("k_applies")
def test_a_dotdot_spelling_is_never_proven(tree: Path) -> None:
    """``link_dir/..`` is lexically ``root`` but resolves to ``root``'s parent's view."""
    from apm_cli.core.invocation_memo import invocation_scope

    with invocation_scope():
        assert resolve_path(tree / "dir" / "file.md") == tree / "dir" / "file.md"
        assert realpath.proven_link_free(tree / "dir")
        assert not realpath.proven_link_free(tree / "link_dir" / ".." / "dir")


def _expect_link_refused(tree: Path, leaf: Path) -> None:
    """The replaced directory is a link again: answers, link scan and containment agree."""
    from apm_cli.utils.path_security import first_symlink_component

    assert resolve_path(leaf) == leaf.resolve() == tree / "outside" / "x.md"
    assert first_symlink_component(tree, leaf) == tree / "dir" / "sub"
    with pytest.raises(PathTraversalError):
        ensure_path_within(leaf, tree / "dir")


def _replace_with_rmtree(sub: Path, target: Path) -> None:
    from apm_cli.integration import write_seam

    write_seam.rmtree(sub)
    sub.symlink_to(target, target_is_directory=True)


def _replace_with_rename(sub: Path, target: Path) -> None:
    from apm_cli.integration import write_seam

    write_seam.rename(sub, sub.with_name("moved"))
    sub.symlink_to(target, target_is_directory=True)


def _replace_with_copied_link(sub: Path, target: Path) -> None:
    from apm_cli.utils.file_ops import robust_copy_entry

    os.rmdir(sub)  # unreleased: only the link creator can drop the proof
    staged = sub.with_name("staged-link")
    staged.symlink_to(target, target_is_directory=True)
    robust_copy_entry(staged, sub)


@_LINKS
@pytest.mark.parametrize(
    "replace", [_replace_with_rmtree, _replace_with_rename, _replace_with_copied_link]
)
@pytest.mark.usefixtures("k_applies")
def test_a_proven_directory_apm_replaces_with_a_link_is_refused(tree: Path, replace) -> None:
    """R4: write_seam and every APM link creator drop proofs at or below the path."""
    from apm_cli.core.invocation_memo import invocation_scope

    sub, leaf = tree / "dir" / "sub", tree / "dir" / "sub" / "x.md"
    with invocation_scope():
        assert resolve_path(leaf) == leaf
        assert realpath.proven_link_free(sub)
        replace(sub, tree / "outside")
        assert not realpath.proven_link_free(sub)
        _expect_link_refused(tree, leaf)


@pytest.mark.parametrize("volatile", [".git", "pkg.inc.0a1b2c3d", "x.tmp", "staging"])
@pytest.mark.usefixtures("k_applies")
def test_nothing_under_a_volatile_component_is_proven(tree: Path, volatile: str) -> None:
    """R3: APM renames and deletes there."""
    from apm_cli.core.invocation_memo import invocation_scope

    directory = tree / "dir" / volatile
    directory.mkdir()
    with invocation_scope():
        for _ in range(2):
            assert resolve_path(directory / "x.md") == (directory / "x.md").resolve()
        assert not realpath.proven_link_free(directory)


@pytest.fixture
def fresh_proof():
    realpath._reset_proof()
    yield
    realpath._reset_proof()


@pytest.mark.skipif(not realpath._FAST, reason="kernel form is Linux-only")
def test_a_failed_proc_proof_leaves_every_later_call_to_pathlib(
    tree: Path, fresh_proof: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A path through a link takes the /proc form (openat2 refuses links).
    candidate = tree / "link_dir" / "file.md"
    expected = candidate.resolve()

    def diverged(*_args, **_kwargs):
        raise OSError("not reachable from this root")

    monkeypatch.setattr(os, "stat", diverged)
    assert resolve_path(candidate) == expected
    monkeypatch.undo()
    assert realpath._proc_state["refused"] is True

    def no_kernel(*_args, **_kwargs):
        raise AssertionError("the kernel form must stay off")

    monkeypatch.setattr(os, "open", no_kernel)
    assert [resolve_path(candidate), resolve_path(tree / "dir")] == [
        expected,
        (tree / "dir").resolve(),
    ]


@_LINKS
def test_a_skill_linked_out_of_the_bundle_is_still_refused(tmp_path: Path) -> None:
    from apm_cli.models.validation import validate_apm_package

    outside = tmp_path / "outside" / "evil"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text(
        "---\nname: evil\ndescription: x\n---\n# x\n", encoding="utf-8"
    )
    skills = tmp_path / "pkg" / "skills"
    (skills / "good").mkdir(parents=True)
    (skills / "good" / "SKILL.md").write_text(
        "---\nname: good\ndescription: x\n---\n# x\n", encoding="utf-8"
    )
    (skills / "evil").symlink_to(outside, target_is_directory=True)
    result = validate_apm_package(tmp_path / "pkg")
    assert [e for e in result.errors if "evil" in e and "outside" in e]


@pytest.mark.skipif(not realpath._openat2_state["usable"], reason="openat2 is Linux 5.6+")
def test_a_link_free_path_resolves_without_the_proc_name(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain, linked = tree / "dir" / "sub", tree / "link_dir" / "sub"
    missing = tree / "dir" / "missing" / "x.md"
    expected = [plain.resolve(), missing.resolve(), linked.resolve()]
    if realpath._link_free(os.fspath(plain)) is not True:
        pytest.skip("openat2 cannot prove a link-free path here (seccomp or kernel)")
    readlinks: list[str] = []
    readlink = os.readlink

    def counted(value, *args, **kwargs):
        readlinks.append(os.fspath(value))
        return readlink(value, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", counted)
    assert [resolve_path(plain), resolve_path(missing), resolve_path(linked)] == expected
    assert len(readlinks) == 1  # only the path through a link needs the /proc name


@pytest.mark.skipif(not realpath._openat2_state["usable"], reason="openat2 is Linux 5.6+")
def test_openat2_shapes_match_pathlib(tree: Path) -> None:
    (tree / "dir" / "final_link").symlink_to(tree / "dir" / "file.md")
    os.mkdir(tree / "locked")
    os.chmod(tree / "locked", 0)
    raw = os.fsdecode(b"/tmp/\xff-not-utf8")
    candidates = [
        tree / "link_dir" / "sub",
        tree / "dir" / "final_link",
        tree / "dangling",
        Path("/proc/self/cwd"),
        tree / "dir" / "missing" / "deeper",
        tree / "dir" / "file.md" / "under-file",
        tree / "locked" / "x",
        Path(raw),
    ]
    try:
        for candidate in candidates:
            assert resolve_path(candidate) == candidate.resolve(), candidate
        assert realpath._link_free("//" + str(tree)) is None
        assert realpath._link_free(str(tree) + "\0x") is None
    finally:
        os.chmod(tree / "locked", 0o700)


@pytest.mark.skipif(not realpath._openat2_state["usable"], reason="openat2 is Linux 5.6+")
def test_an_unsupported_openat2_turns_itself_off(tree: Path) -> None:
    import errno

    saved = list(realpath._openat2_call)
    realpath._openat2_call[:] = [lambda _raw: (-1, errno.ENOSYS)]
    try:
        candidate = tree / "dir" / "file.md"
        assert resolve_path(candidate) == candidate.resolve()
        assert realpath._openat2_state["usable"] is False
    finally:
        realpath._openat2_call[:] = saved
        realpath._openat2_state["usable"] = True
