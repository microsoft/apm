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
