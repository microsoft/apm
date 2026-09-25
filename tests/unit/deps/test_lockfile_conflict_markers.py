"""Git merge conflict markers in ``apm.lock.yaml`` (#2979, diagnostic slice)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile, LockfileConflictError, LockfileFormatError
from tests.utils.diagnostic_recipe import shell_commands_in

pytestmark = pytest.mark.component

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
def test_read_names_the_file_and_a_manual_next_step(tmp_path: Path, text: str) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(LockfileConflictError) as exc_info:
        LockFile.read(path)

    message = str(exc_info.value)
    assert str(path) in message
    assert "conflict markers" in message
    assert shell_commands_in(message), "a runnable next step MUST be offered"
    assert exc_info.value.path == path


def test_conflict_diagnostic_never_offers_a_bare_apm_command_as_the_repair(
    tmp_path: Path,
) -> None:
    """An apm command may only appear sequenced after a step that resolves the file."""
    path = tmp_path / "apm.lock.yaml"
    path.write_text(_CONFLICTED, encoding="utf-8")

    with pytest.raises(LockfileConflictError) as exc_info:
        LockFile.read(path)

    message = str(exc_info.value)
    assert "apm outdated" not in message
    assert "apm update" not in message
    lines = message.splitlines()
    resolving = next(i for i, line in enumerate(lines) if "git checkout" in line)
    for i, line in enumerate(lines):
        if "apm install" in line:
            assert i > resolving, f"{line!r} offers an apm command before the lockfile is resolved"


def test_conflict_diagnostic_carries_no_raw_parser_content(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text(_CONFLICTED, encoding="utf-8")

    with pytest.raises(LockfileConflictError) as exc_info:
        LockFile.read(path)

    message = str(exc_info.value)
    assert "<<<<<<<" not in message
    assert "could not find expected" not in message
    assert 'in "<unicode string>"' not in message


def test_conflict_error_is_a_format_error() -> None:
    """Callers that already fail closed on a bad lockfile keep doing so."""
    assert issubclass(LockfileConflictError, LockfileFormatError)


def test_read_corrupt_lockfile_without_markers_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text("lockfile_version: '1'\ndependencies: [\n", encoding="utf-8")

    with pytest.raises(LockfileFormatError) as exc_info:
        LockFile.read(path)

    assert not isinstance(exc_info.value, LockfileConflictError)


def test_read_undecodable_lockfile_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_bytes(b"lockfile_version: '1'\n\xff\xfe<<<<<<< HEAD\n")

    with pytest.raises(LockfileFormatError) as exc_info:
        LockFile.read(path)

    assert not isinstance(exc_info.value, LockfileConflictError)
    assert str(path) in str(exc_info.value)


def test_marker_text_inside_a_value_is_not_a_conflict(tmp_path: Path) -> None:
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


def test_separator_only_line_is_not_a_conflict(tmp_path: Path) -> None:
    """A bare ``=======`` is not evidence of a conflict, so it stays a parse error."""
    path = tmp_path / "apm.lock.yaml"
    path.write_text("lockfile_version: '1'\n=======\ndependencies: []\n", encoding="utf-8")

    with pytest.raises(LockfileFormatError) as exc_info:
        LockFile.read(path)

    assert not isinstance(exc_info.value, LockfileConflictError)


def test_valid_lockfile_is_read_normally(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text("lockfile_version: '1'\ndependencies: []\n", encoding="utf-8")

    assert LockFile.read(path) is not None
