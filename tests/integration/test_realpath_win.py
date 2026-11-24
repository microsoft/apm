"""W3d R12 cells for ``apm_cli.utils.realpath_win`` (exact Windows ``Path.resolve``).

Stock ``Path.resolve()`` is the oracle: every Windows cell compares the fast
forms with it, or asserts what holding a directory must (and must not) do to
other processes and to APM's own renames and deletes. Holds exist only inside
one ``invocation_scope()``; each cell opens one with the project root as cwd
and a registered package root, as activation does. These cells need a
second process; the in-process cells are in tests/unit/utils/test_realpath_win.py.

Test names avoid the volatile markers (staging, backup, ...): ``tmp_path`` is
derived from the test name, and a volatile project root is never held.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apm_cli.core.invocation_memo import forget_invocation_memos, invocation_scope
from apm_cli.utils import realpath_win
from apm_cli.utils.realpath import resolve_path
from apm_cli.utils.realpath_win import (
    held_count,
    register_held_root,
    release_held,
)

# Spawns processes, so it lives under tests/integration; windows_compat puts it
# in the PR-time Windows Compatibility Gate (`-m windows_compat tests/integration`).
pytestmark = [
    pytest.mark.component,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-LAW-portability.byte_deterministic"),
]

_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows holds and Win32 paths")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "apm_modules" / "o" / "r"
    (package / "sub").mkdir(parents=True)
    (package / "other").mkdir()
    (package / "f.txt").write_text("f", encoding="ascii")
    (package / "sub" / "b.txt").write_text("b", encoding="ascii")
    (package / "other" / "c.txt").write_text("c", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@contextmanager
def _held_scope(root: Path) -> Iterator[Path]:
    """One invocation with the project root held and ``apm_modules/o/r`` registered."""
    with invocation_scope():
        package = root / "apm_modules" / "o" / "r"
        register_held_root(package, root / "apm_modules")
        assert realpath_win._root_text() == str(root), "the project root must be held"
        yield package


def _held_keys() -> set[str]:
    with realpath_win._lock:
        return {key.lower() for key in realpath_win._held}


def _other_process(code: str, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


_RENAME = "import os, sys; os.rename(sys.argv[1], sys.argv[2])"


@_WINDOWS
def test_held_package_cannot_be_renamed_by_another_process(project: Path) -> None:
    with _held_scope(project) as package:
        resolve_path(package / "f.txt")
        assert str(package).lower() in _held_keys()
        moved = package.with_name("r-moved")

        blocked = _other_process(_RENAME, package, moved)
        assert blocked.returncode != 0, "a held package dir was renamed"
        assert "PermissionError" in blocked.stderr, blocked.stderr
        assert (package / "f.txt").is_file()

        release_held()
        assert _other_process(_RENAME, package, moved).returncode == 0
        assert (moved / "f.txt").is_file()


@_WINDOWS
def test_held_package_pins_its_ancestors_against_another_process(project: Path) -> None:
    """Form A's anchor answers rely on it: an ancestor of a held dir cannot be renamed."""
    with _held_scope(project) as package:
        resolve_path(package / "f.txt")
        assert str(package).lower() in _held_keys()
        owner = package.parent
        blocked = _other_process(_RENAME, owner, owner.with_name("o-moved"))
        assert blocked.returncode != 0, "the parent of a held package dir was renamed"
        assert (package / "f.txt").is_file()

        release_held()
        assert _other_process(_RENAME, owner, owner.with_name("o-moved")).returncode == 0
        assert (owner.with_name("o-moved") / "r" / "f.txt").is_file()


@_WINDOWS
def test_r4_forget_memos_releases_holds_for_user_code(project: Path) -> None:
    with _held_scope(project) as package:
        resolve_path(package / "sub" / "b.txt")
        assert held_count() >= 3
        forget_invocation_memos()
        assert held_count() == 0
        renamed = _other_process(_RENAME, package / "sub", package / "sub2")
        assert renamed.returncode == 0, renamed.stderr
        assert (package / "sub2" / "b.txt").is_file()


_FOREIGN_RMTREE = (
    "import sys\n"
    "from apm_cli.utils.file_ops import robust_rmtree\n"
    "try:\n"
    "    robust_rmtree(sys.argv[1], max_retries=1)\n"
    "except OSError as exc:\n"
    "    print(type(exc).__name__, getattr(exc, 'winerror', None))\n"
    "    sys.exit(3)\n"
)


@_WINDOWS
def test_r7_foreign_rmtree_of_held_package_fails_after_removing_unheld_entries(
    project: Path,
) -> None:
    """robust_rmtree finishes independent removals before it raises.

    So a second process deleting a held package fails with an OSError while
    the held directories (package, sub) survive, but the files and the
    never-walked ``other`` directory under them are already gone.
    """
    with _held_scope(project) as package:
        resolve_path(package / "sub" / "b.txt")
        assert {str(package).lower(), str(package / "sub").lower()} <= _held_keys()

        result = _other_process(_FOREIGN_RMTREE, package)

        assert result.returncode == 3, result.stdout + result.stderr
        assert result.stdout.split()[0] in {"PermissionError", "OSError"}
        assert package.is_dir() and (package / "sub").is_dir()
        assert not (package / "f.txt").exists()
        assert not (package / "sub" / "b.txt").exists()
        assert not (package / "other").exists()
