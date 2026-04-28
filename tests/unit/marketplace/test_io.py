"""Tests for _io.py -- shared atomic write helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.marketplace._io import atomic_write


class TestAtomicWrite:
    """Tests for the shared ``atomic_write()`` function."""

    def test_creates_file_with_correct_content(self, tmp_path: Path) -> None:
        """A new file is created with the expected content."""
        path = tmp_path / "output.txt"
        atomic_write(path, "hello world\n")
        assert path.read_text(encoding="utf-8") == "hello world\n"

    def test_no_tmp_file_remains(self, tmp_path: Path) -> None:
        """The temporary file is cleaned up after a successful write."""
        path = tmp_path / "output.txt"
        atomic_write(path, "data")
        tmp_file = path.with_suffix(path.suffix + ".tmp")
        assert not tmp_file.exists()

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """An existing file is replaced with the new content."""
        path = tmp_path / "output.txt"
        path.write_text("old content", encoding="utf-8")
        atomic_write(path, "new content")
        assert path.read_text(encoding="utf-8") == "new content"

    def test_preserves_unicode(self, tmp_path: Path) -> None:
        """Non-ASCII content round-trips correctly."""
        path = tmp_path / "output.txt"
        content = '{"name": "caf\\u00e9"}\n'
        atomic_write(path, content)
        assert path.read_text(encoding="utf-8") == content
