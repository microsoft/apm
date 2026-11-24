"""W3d R12 cells for ``apm_cli.utils.realpath_win`` (exact Windows ``Path.resolve``).

Stock ``Path.resolve()`` is the oracle: every Windows cell compares the fast
forms with it, or asserts what holding a directory must (and must not) do to
other processes and to APM's own renames and deletes. Holds exist only inside
one ``invocation_scope()``; each cell opens one with the project root as cwd
and a registered package root, as activation does. The pure string helpers
(``plain_win32``, ``_volatile``) run on every OS.

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
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.utils import realpath_win
from apm_cli.utils.file_ops import robust_replace, robust_rmtree
from apm_cli.utils.realpath import resolve_path
from apm_cli.utils.realpath_win import (
    _volatile,
    held_count,
    plain_win32,
    register_held_root,
    release_held,
    resolve_windows,
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


def _short_path(path: Path) -> str | None:
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value if 0 < length < 1024 and "~" in buffer.value else None


# --------------------------------------------------------------------------
# Pure helpers (every OS)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "C:\\",
        "C:\\Users\\dev\\proj\\apm_modules\\o\\r\\SKILL.md",
        "d:\\a b\\x.y.z",
    ],
)
def test_plain_win32_accepts_plain_drive_paths(path: str) -> None:
    assert plain_win32(path)


@pytest.mark.parametrize(
    "path",
    [
        "C:\\PROGRA~1\\x",  # 8.3 alias
        "C:\\x\\name.",  # trailing dot
        "C:\\x\\name ",  # trailing space
        "C:\\x\\f.txt:stream",  # alternate data stream
        "C:\\x\\CON",
        "C:\\x\\nul.txt",
        "C:\\x\\lpt9.log",
        "C:\\x\\COM\u00b9",
        "C:\\x\\caf\u00e9",  # non-ASCII
        "C:\\" + "a" * 257,  # MAX_PATH
        "\\\\server\\share\\x",  # UNC
        "\\\\?\\C:\\x",  # already extended
        "x\\y",  # relative
        "C:x",  # drive-relative
        "C:/x",  # forward slash
        "C:\\x\\\\y",  # empty component
    ],
)
def test_plain_win32_refuses_every_non_plain_form(path: str) -> None:
    assert not plain_win32(path)


@pytest.mark.parametrize(
    ("path", "volatile"),
    [
        ("C:\\p\\.git\\HEAD", True),
        ("C:\\p\\apm_modules\\.apm-resolution-staging\\abc", True),
        ("C:\\p\\pkg.inc.1", True),
        ("C:\\p\\x.tmp3", True),
        ("C:\\p\\Backup-1", True),
        ("C:\\p\\migration", True),
        ("C:\\p\\refresh-x", True),
        ("C:\\p\\apm_modules\\o\\r\\skills", False),
        ("C:\\p\\.github\\gitlab", False),
    ],
)
def test_volatile_matches_only_marked_components(path: str, volatile: bool) -> None:
    assert _volatile(path) is volatile


# --------------------------------------------------------------------------
# Windows cells
# --------------------------------------------------------------------------


@_WINDOWS
def test_differential_matches_stock_resolve(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realpath_win, "_DIFFERENTIAL", True)
    with _held_scope(project) as package:
        (package / "longdirectoryname").mkdir()
        inputs = [
            package / "f.txt",
            package / "sub",
            package / "missing.txt",
            package / "m1" / "m2.txt",
            package / "f.txt" / "under-file",
            project / "APM_MODULES" / "o" / "r" / "SUB",
            package / "f.txt.",
            package / "nul.txt",
            project / ("d" * 70) / ("e" * 70) / ("f" * 70) / ("g" * 70) / "x.txt",
            Path("apm_modules") / "o" / "r" / "f.txt",
            project / "missing-root-child",
        ]
        short = _short_path(package / "longdirectoryname")
        if short is not None:
            inputs.append(Path(short))
        for path in inputs:
            assert str(resolve_path(path)) == str(path.resolve()), path
        assert resolve_windows(package / "f.txt") is not None, "the fast form must engage"
        assert held_count() >= 2


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
def test_robust_rmtree_and_replace_of_held_dirs_succeed(project: Path) -> None:
    with _held_scope(project) as package:
        resolve_path(package / "sub" / "b.txt")
        assert str(package / "sub").lower() in _held_keys()
        robust_replace(package / "sub", package / "sub-renamed")
        assert (package / "sub-renamed" / "b.txt").is_file()

        # Releasing package/sub unregistered its package: activation registers it again.
        register_held_root(package, project / "apm_modules")
        resolve_path(package / "other" / "c.txt")
        assert {str(package).lower(), str(package / "other").lower()} <= _held_keys()
        robust_rmtree(package)
        assert not package.exists()
        assert held_count() == 1  # only the project root


@_WINDOWS
def test_resolution_session_prepare_and_rollback_of_held_package(project: Path) -> None:
    modules = project / "apm_modules"
    with _held_scope(project) as package:
        resolve_path(package / "sub" / "b.txt")
        session = ResolutionStagingSession(modules)
        session.prepare_path(package)
        assert not package.exists()

        (package / "sub").mkdir(parents=True)
        (package / "f.txt").write_text("new", encoding="ascii")
        register_held_root(package, modules)
        resolve_path(package / "sub" / "x.txt")
        assert str(package).lower() in _held_keys()

        assert session.rollback() == []
        assert (package / "f.txt").read_text(encoding="ascii") == "f"
        assert (package / "sub" / "b.txt").is_file()


@_WINDOWS
def test_case_variant_returns_on_disk_case(project: Path) -> None:
    with _held_scope(project) as package:
        (package / "Skill.md").write_text("s", encoding="ascii")
        for text in (package / "skill.MD", project / "Apm_Modules" / "O" / "r" / "SKILL.md"):
            resolved = resolve_path(text)
            assert str(resolved) == str(text.resolve())
            assert str(resolved) == str(package / "Skill.md")


@_WINDOWS
def test_held_count_returns_to_zero_after_scope(project: Path) -> None:
    with _held_scope(project) as package:
        resolve_path(package / "sub" / "b.txt")
        assert held_count() >= 3
    assert held_count() == 0

    def failing_body() -> None:
        with _held_scope(project) as package:
            resolve_path(package / "sub" / "b.txt")
            assert held_count() >= 3
            raise RuntimeError("body failed")

    with pytest.raises(RuntimeError, match="body failed"):
        failing_body()
    assert held_count() == 0


@_WINDOWS
def test_r1_middle_junction_takes_the_followed_path(project: Path) -> None:
    target = project / "elsewhere"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "x.txt").write_text("x", encoding="ascii")
    with _held_scope(project) as package:
        resolve_path(package / "f.txt")
        link = package / "mid"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
        through = link / "sub" / "x.txt"
        resolved = resolve_path(through)
        assert str(resolved) == str(through.resolve())
        assert str(resolved) == str(target / "sub" / "x.txt")
        assert str(link).lower() not in _held_keys()


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


@_WINDOWS
def test_volatile_component_takes_stock_resolve(project: Path) -> None:
    with _held_scope(project):
        volatile = project / "apm_modules" / ".apm-resolution-staging" / "abc"
        volatile.mkdir(parents=True)
        for path in (volatile, volatile / "x.txt"):
            assert resolve_windows(path) is None
            assert str(resolve_path(path)) == str(path.resolve())
