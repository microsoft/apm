"""A converged install leaves the cache-pin marker untouched (#4790).

``install/cache_pin.py::write_marker`` runs on every install. A second no-op
install must not rewrite an identical ``.apm-pin`` (bytes, mtime and inode
kept). A missing or different marker is written, and a symlinked marker is
replaced by a regular file without touching the link target.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.install.cache_pin import MARKER_FILENAME, SCHEMA_VERSION, write_marker

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I2")]

_COMMIT = "a" * 40


def _payload(commit: str) -> bytes:
    return json.dumps({"schema_version": SCHEMA_VERSION, "resolved_commit": commit}).encode()


def _age(path: Path) -> int:
    """Push *path*'s mtime 10 s into the past; return it."""
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns - 10_000_000_000))
    return path.stat().st_mtime_ns


def test_an_identical_marker_is_not_rewritten(tmp_path: Path) -> None:
    write_marker(tmp_path, _COMMIT)
    marker = tmp_path / MARKER_FILENAME
    stamped = _age(marker)
    inode = marker.stat().st_ino

    write_marker(tmp_path, _COMMIT)

    assert marker.read_bytes() == _payload(_COMMIT)
    assert marker.stat().st_mtime_ns == stamped, "a converged install must not rewrite the marker"
    assert marker.stat().st_ino == inode


def test_a_missing_marker_is_written(tmp_path: Path) -> None:
    write_marker(tmp_path, _COMMIT)
    assert (tmp_path / MARKER_FILENAME).read_bytes() == _payload(_COMMIT)


def test_a_different_marker_is_rewritten(tmp_path: Path) -> None:
    marker = tmp_path / MARKER_FILENAME
    marker.write_bytes(_payload("b" * 40))
    write_marker(tmp_path, _COMMIT)
    assert marker.read_bytes() == _payload(_COMMIT)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_marker_is_replaced_by_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(b"unrelated")
    stamped = _age(target)
    marker = tmp_path / MARKER_FILENAME
    marker.symlink_to(target)

    write_marker(tmp_path, _COMMIT)

    assert not marker.is_symlink(), "the marker must become a regular file"
    assert marker.is_file()
    assert marker.read_bytes() == _payload(_COMMIT)
    assert target.read_bytes() == b"unrelated", "the link target must stay unchanged"
    assert target.stat().st_mtime_ns == stamped


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_marker_with_identical_target_bytes_is_still_replaced(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(_payload(_COMMIT))
    marker = tmp_path / MARKER_FILENAME
    marker.symlink_to(target)

    write_marker(tmp_path, _COMMIT)

    assert not marker.is_symlink()
    assert marker.read_bytes() == _payload(_COMMIT)


def test_a_directory_in_the_way_is_left_and_install_continues(tmp_path: Path) -> None:
    (tmp_path / MARKER_FILENAME).mkdir()
    write_marker(tmp_path, _COMMIT)
    assert (tmp_path / MARKER_FILENAME).is_dir()
    assert [p.name for p in tmp_path.iterdir()] == [MARKER_FILENAME], "no temp file left behind"
