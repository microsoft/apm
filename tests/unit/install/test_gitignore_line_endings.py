"""#5458: the apm_modules/ entry keeps the .gitignore's own line endings.

Since #4863 the update writes exact bytes (``newline=""``); before it, text
mode wrote the platform's ending. A CRLF file must stay CRLF, an LF file LF,
and a new file gets the platform default, as v0.38.1 wrote it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.install.helpers.gitignore import update_gitignore_for_apm_modules

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-portability.byte_deterministic")]

_ENTRY = "{nl}{nl}# APM dependencies{nl}apm_modules/{nl}"


def _update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bytes | None) -> None:
    monkeypatch.chdir(tmp_path)
    if existing is not None:
        (tmp_path / ".gitignore").write_bytes(existing)
    update_gitignore_for_apm_modules()


def _assert_gitignore(tmp_path: Path, expected: bytes) -> None:
    """The durable file the install leaves behind, byte for byte."""
    assert (tmp_path / ".gitignore").read_bytes() == expected


@pytest.mark.parametrize("nl", ["\r\n", "\n"])
def test_the_entry_uses_the_files_own_line_ending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nl: str
) -> None:
    existing = f"node_modules/{nl}.env{nl}".encode()

    _update(tmp_path, monkeypatch, existing)

    _assert_gitignore(tmp_path, existing + _ENTRY.format(nl=nl).encode())


def test_a_mixed_file_keeps_its_bytes_and_follows_its_first_line_ending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = b"a/\r\nb/\nc/\r\n"

    _update(tmp_path, monkeypatch, existing)

    _assert_gitignore(tmp_path, existing + _ENTRY.format(nl="\r\n").encode())


@pytest.mark.parametrize("platform_nl", ["\r\n", "\n"])
def test_a_new_or_unterminated_file_uses_the_platform_ending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_nl: str
) -> None:
    monkeypatch.setattr(os, "linesep", platform_nl)

    _update(tmp_path, monkeypatch, None)
    _assert_gitignore(
        tmp_path, f"{platform_nl}# APM dependencies{platform_nl}apm_modules/{platform_nl}".encode()
    )
    (tmp_path / ".gitignore").write_bytes(b"dist/")
    _update(tmp_path, monkeypatch, None)
    _assert_gitignore(tmp_path, f"dist/{_ENTRY.format(nl=platform_nl)}".encode())
