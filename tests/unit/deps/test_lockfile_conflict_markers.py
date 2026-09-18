"""Git merge conflict markers in ``apm.lock.yaml`` (#2979)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import (
    LockFile,
    LockfileConflictError,
    LockfileFormatError,
    discard_conflicted_lockfile,
)

pytestmark = pytest.mark.component

_VALID = "lockfile_version: '1'\ndependencies: []\n"

_CONFLICTED = (
    "lockfile_version: '1'\n"
    "<<<<<<< HEAD\n"
    "dependencies: []\n"
    "=======\n"
    "dependencies:\n"
    "- repo_url: example/x\n"
    ">>>>>>> feature\n"
)

_CONFLICTED_DIFF3 = (
    "lockfile_version: '1'\n"
    "<<<<<<< HEAD\n"
    "dependencies: []\n"
    "||||||| merged common ancestors\n"
    "dependencies:\n"
    "- repo_url: example/base\n"
    "=======\n"
    "dependencies:\n"
    "- repo_url: example/x\n"
    ">>>>>>> feature\n"
)


@pytest.mark.parametrize("text", [_CONFLICTED, _CONFLICTED_DIFF3])
def test_read_names_conflict_markers_and_next_action(tmp_path: Path, text: str) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(LockfileConflictError) as exc_info:
        LockFile.read(path)

    message = str(exc_info.value)
    assert str(path) in message
    assert "conflict markers" in message
    assert "apm install" in message
    assert exc_info.value.path == path


def test_conflict_error_is_a_format_error() -> None:
    assert issubclass(LockfileConflictError, LockfileFormatError)


def test_read_corrupt_lockfile_without_markers_is_a_plain_format_error(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text("lockfile_version: '1'\ndependencies: [\n", encoding="utf-8")

    with pytest.raises(LockfileFormatError) as exc_info:
        LockFile.read(path)

    assert not isinstance(exc_info.value, LockfileConflictError)


def test_read_accepts_marker_text_that_is_not_at_line_start(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text(
        "lockfile_version: '1'\n"
        "dependencies:\n"
        "- repo_url: example/x\n"
        "  resolved_ref: 'tag <<<<<<< HEAD >>>>>>> end'\n",
        encoding="utf-8",
    )

    lock = LockFile.read(path)

    assert lock is not None
    assert lock.dependencies["example/x"].resolved_ref == "tag <<<<<<< HEAD >>>>>>> end"


def test_read_treats_separator_only_line_as_ordinary_format_error(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text("lockfile_version: '1'\n=======\ndependencies: []\n", encoding="utf-8")

    with pytest.raises(LockfileFormatError) as exc_info:
        LockFile.read(path)

    assert not isinstance(exc_info.value, LockfileConflictError)


def test_read_undecodable_lockfile_is_a_format_error(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_bytes(b"lockfile_version: '1'\n\xff\xfe<<<<<<< HEAD\n")

    with pytest.raises(LockfileFormatError) as exc_info:
        LockFile.read(path)

    assert not isinstance(exc_info.value, LockfileConflictError)
    assert str(path) in str(exc_info.value)


def test_discard_leaves_an_undecodable_lockfile_in_place(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    raw = b"\xff\xfe<<<<<<< HEAD\n"
    path.write_bytes(raw)

    assert discard_conflicted_lockfile(path) is False
    assert path.read_bytes() == raw


def test_discard_removes_only_a_conflicted_lockfile(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"

    assert discard_conflicted_lockfile(path) is False

    path.write_text(_VALID, encoding="utf-8")
    assert discard_conflicted_lockfile(path) is False
    assert path.read_text(encoding="utf-8") == _VALID

    path.write_text(_CONFLICTED, encoding="utf-8")
    assert discard_conflicted_lockfile(path) is True
    assert not path.exists()
