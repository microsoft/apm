"""Tests for _io.py -- shared atomic write helper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.marketplace._io import __all__, atomic_write


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

    # ------------------------------------------------------------------
    # Tmp file path
    # ------------------------------------------------------------------

    def test_tmp_path_has_correct_suffix(self, tmp_path: Path) -> None:
        """Temp file path uses path.with_suffix(path.suffix + '.tmp')."""
        path = tmp_path / "output.json"
        expected_tmp = path.with_suffix(path.suffix + ".tmp")
        # After success, tmp must not exist; confirm target matches expectation
        atomic_write(path, "{}")
        assert not expected_tmp.exists()
        assert path.exists()

    def test_tmp_path_for_no_suffix_file(self, tmp_path: Path) -> None:
        """Files with no extension get a '.tmp' suffix."""
        path = tmp_path / "Makefile"
        atomic_write(path, "# rules\n")
        tmp_path_expected = path.with_suffix(".tmp")
        assert not tmp_path_expected.exists()
        assert path.read_text(encoding="utf-8") == "# rules\n"

    # ------------------------------------------------------------------
    # Exception during write: tmp file is cleaned up
    # ------------------------------------------------------------------

    def test_tmp_file_cleaned_up_on_write_exception(self, tmp_path: Path) -> None:
        """When writing fails, the tmp file is removed and exception re-raised."""
        path = tmp_path / "fail.txt"
        tmp_file = path.with_suffix(path.suffix + ".tmp")

        with patch("apm_cli.marketplace._io.os.fsync", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                atomic_write(path, "data")

        assert not tmp_file.exists()
        assert not path.exists()

    def test_original_exception_propagates_when_unlink_also_fails(self, tmp_path: Path) -> None:
        """If unlink raises OSError during cleanup, original exception propagates."""
        path = tmp_path / "fail_unlink.txt"

        with patch("apm_cli.marketplace._io.os.fsync", side_effect=RuntimeError("boom")):
            with patch("apm_cli.marketplace._io.Path.unlink", side_effect=OSError("locked")):
                with pytest.raises(RuntimeError, match="boom"):
                    atomic_write(path, "data")

    def test_base_exception_is_also_caught(self, tmp_path: Path) -> None:
        """BaseException (e.g. KeyboardInterrupt) triggers cleanup too."""
        path = tmp_path / "interrupt.txt"
        tmp_file = path.with_suffix(path.suffix + ".tmp")

        with patch(
            "apm_cli.marketplace._io.os.fsync",
            side_effect=KeyboardInterrupt,
        ):
            with pytest.raises(KeyboardInterrupt):
                atomic_write(path, "data")

        assert not tmp_file.exists()

    # ------------------------------------------------------------------
    # __all__
    # ------------------------------------------------------------------

    def test_dunder_all_contains_atomic_write(self) -> None:
        assert "atomic_write" in __all__

    def test_dunder_all_does_not_expose_os(self) -> None:
        assert "os" not in __all__
