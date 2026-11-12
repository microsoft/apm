"""Shared, committed APM artifacts follow the process umask (#3442).

``atomic_write_text`` stages through ``tempfile.mkstemp`` (always 0600), so
callers writing shared artifacts must pass an explicit umask-derived
``new_file_mode``. Secret-bearing files keep their explicit 0600 and existing
files keep their mode.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from apm_cli.compilation.output_writer import CompiledOutputWriter
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils import atomic_io
from apm_cli.utils.atomic_io import atomic_write_text, umask_file_mode

pytestmark = pytest.mark.windows_compat

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _write_lockfile(path: Path) -> None:
    LockFile().write(path)


def _write_compiled(path: Path) -> None:
    CompiledOutputWriter().write(path, "# Instructions\n\nBe brief.\n")


SHARED_WRITERS = [
    pytest.param("apm.lock.yaml", _write_lockfile, id="lockfile"),
    pytest.param("AGENTS.md", _write_compiled, id="compiled-agents-md"),
]


@posix_only
@pytest.mark.parametrize(("name", "writer"), SHARED_WRITERS)
def test_new_shared_artifact_follows_umask_022(umask_022, tmp_path: Path, name, writer) -> None:
    target = tmp_path / name
    writer(target)
    assert _mode(target) == 0o644


@posix_only
@pytest.mark.parametrize(("name", "writer"), SHARED_WRITERS)
def test_new_shared_artifact_tracks_a_stricter_umask(tmp_path: Path, name, writer) -> None:
    previous = os.umask(0o027)
    try:
        target = tmp_path / name
        writer(target)
    finally:
        os.umask(previous)
    assert _mode(target) == 0o640


@posix_only
@pytest.mark.parametrize(("name", "writer"), SHARED_WRITERS)
def test_existing_shared_artifact_keeps_its_mode(umask_022, tmp_path: Path, name, writer) -> None:
    target = tmp_path / name
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o600)
    writer(target)
    assert _mode(target) == 0o600


@posix_only
def test_explicit_secret_mode_is_not_widened_by_umask(umask_022, tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    atomic_write_text(target, "{}", new_file_mode=0o600)
    assert _mode(target) == 0o600


@posix_only
def test_umask_file_mode_never_changes_the_process_umask(
    umask_022, monkeypatch, tmp_path: Path
) -> None:
    """Concurrent creations elsewhere must never observe a transient umask."""

    def _forbidden(_mask: int) -> int:
        raise AssertionError("umask_file_mode must not change the process umask")

    monkeypatch.setattr(atomic_io.os, "umask", _forbidden)
    assert umask_file_mode(tmp_path / "apm.lock.yaml") == 0o644


@posix_only
def test_concurrent_directory_creation_keeps_umask_mode(umask_022, tmp_path: Path) -> None:
    stop = threading.Event()

    def _probe() -> None:
        while not stop.is_set():
            umask_file_mode(tmp_path / "AGENTS.md")

    worker = threading.Thread(target=_probe)
    worker.start()
    try:
        for index in range(200):
            created = tmp_path / f"dir-{index}"
            created.mkdir()
            assert _mode(created) == 0o755
    finally:
        stop.set()
        worker.join()


def test_umask_file_mode_leaves_no_probe_behind(tmp_path: Path) -> None:
    umask_file_mode(tmp_path / "AGENTS.md")
    assert list(tmp_path.iterdir()) == []


@posix_only
def test_umask_file_mode_skips_existing_targets(tmp_path: Path) -> None:
    target = tmp_path / "apm.lock.yaml"
    target.write_text("old\n", encoding="utf-8")
    assert umask_file_mode(target) is None


@pytest.mark.parametrize(("name", "writer"), SHARED_WRITERS)
def test_shared_artifact_write_succeeds_without_fchmod(
    monkeypatch, tmp_path: Path, name, writer
) -> None:
    """Platforms without ``os.fchmod`` (Windows) keep writing without a mode."""
    monkeypatch.delattr(atomic_io.os, "fchmod", raising=False)
    target = tmp_path / name
    writer(target)
    assert target.is_file()
    assert umask_file_mode(tmp_path / "new.md") is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows behaviour")
@pytest.mark.parametrize(("name", "writer"), SHARED_WRITERS)
def test_shared_artifact_write_on_native_windows(tmp_path: Path, name, writer) -> None:
    target = tmp_path / name
    writer(target)
    assert target.is_file()
    assert umask_file_mode(tmp_path / "new.md") is None
