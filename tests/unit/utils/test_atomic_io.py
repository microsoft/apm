"""Unit tests for apm_cli.utils.atomic_io.atomic_write_text.

Covers:
- Basic write: file created with correct content
- Overwrites existing file atomically
- new_file_mode applied via fchmod when file is new
- new_file_mode NOT applied when file already exists
- new_file_mode=None never calls fchmod
- Write failure: tmp file is cleaned up and exception propagates
- Write failure + unlink failure: original exception still propagates
- Unicode content round-trips correctly
- Temp file uses correct prefix and parent directory
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.utils.atomic_io import atomic_write_text, write_text_lf


class TestAtomicWriteText:
    """Tests for atomic_write_text()."""

    # ------------------------------------------------------------------
    # Happy-path writes
    # ------------------------------------------------------------------

    def test_creates_file_with_correct_content(self, tmp_path: Path) -> None:
        """A new file is created and contains the expected content."""
        target = tmp_path / "output.txt"
        atomic_write_text(target, "hello world\n")
        assert target.read_text(encoding="utf-8") == "hello world\n"

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """An already-existing file is replaced atomically."""
        target = tmp_path / "existing.txt"
        target.write_text("old content", encoding="utf-8")
        atomic_write_text(target, "new content")
        assert target.read_text(encoding="utf-8") == "new content"

    def test_unicode_content_round_trips(self, tmp_path: Path) -> None:
        """Non-ASCII / emoji content is preserved through UTF-8 encoding."""
        target = tmp_path / "unicode.txt"
        content = "café ☕ 日本語\n"
        atomic_write_text(target, content)
        assert target.read_text(encoding="utf-8") == content

    # ------------------------------------------------------------------
    # Temp file properties
    # ------------------------------------------------------------------

    def test_tmp_file_is_in_path_parent(self, tmp_path: Path) -> None:
        """mkstemp is called with dir=path.parent."""
        target = tmp_path / "out.txt"
        created_tmp_names: list[str] = []

        original_mkstemp = tempfile.mkstemp

        def capturing_mkstemp(
            prefix: str = "",
            suffix: str = "",
            dir: str | None = None,
        ) -> tuple[int, str]:
            fd, name = original_mkstemp(prefix=prefix, suffix=suffix, dir=dir)
            created_tmp_names.append(name)
            return fd, name

        with patch("apm_cli.utils.atomic_io.tempfile.mkstemp", side_effect=capturing_mkstemp):
            atomic_write_text(target, "data")

        assert len(created_tmp_names) == 1
        assert Path(created_tmp_names[0]).parent == tmp_path

    def test_tmp_file_uses_apm_atomic_prefix(self, tmp_path: Path) -> None:
        """mkstemp is called with prefix='apm-atomic-'."""
        target = tmp_path / "out.txt"
        captured_kwargs: list[dict] = []

        # Save the original before patching to avoid recursion
        import tempfile as _tempfile

        _orig_mkstemp = _tempfile.mkstemp

        def mock_mkstemp(**kwargs):  # type: ignore[override]
            captured_kwargs.append(dict(kwargs))
            return _orig_mkstemp(**kwargs)

        with patch("apm_cli.utils.atomic_io.tempfile.mkstemp", side_effect=mock_mkstemp):
            atomic_write_text(target, "data")

        assert captured_kwargs[0]["prefix"] == "apm-atomic-"

    # ------------------------------------------------------------------
    # fchmod / new_file_mode
    # ------------------------------------------------------------------

    def test_fchmod_called_for_new_file_with_mode(self, tmp_path: Path) -> None:
        """fchmod is called when new_file_mode is set and file is new."""
        target = tmp_path / "new_file.txt"
        assert not target.exists()

        with patch("apm_cli.utils.atomic_io.os.fchmod", create=True) as mock_fchmod:
            with patch("apm_cli.utils.atomic_io.hasattr", return_value=True):
                atomic_write_text(target, "data", new_file_mode=0o600)

        mock_fchmod.assert_called_once()
        _fd, mode = mock_fchmod.call_args[0]
        assert mode == 0o600

    def test_fchmod_not_called_when_file_exists(self, tmp_path: Path) -> None:
        """fchmod is NOT called when the file already exists."""
        target = tmp_path / "existing.txt"
        target.write_text("existing", encoding="utf-8")

        with patch("apm_cli.utils.atomic_io.os.fchmod", create=True) as mock_fchmod:
            atomic_write_text(target, "data", new_file_mode=0o600)

        mock_fchmod.assert_not_called()

    def test_fchmod_not_called_when_mode_is_none(self, tmp_path: Path) -> None:
        """fchmod is NOT called when new_file_mode is None."""
        target = tmp_path / "out.txt"

        with patch("apm_cli.utils.atomic_io.os.fchmod", create=True) as mock_fchmod:
            atomic_write_text(target, "data", new_file_mode=None)

        mock_fchmod.assert_not_called()

    # ------------------------------------------------------------------
    # Error paths
    # ------------------------------------------------------------------

    def test_tmp_file_cleaned_up_on_write_failure(self, tmp_path: Path) -> None:
        """When fdopen/write raises, the tmp file is deleted."""
        target = tmp_path / "fail.txt"
        tmp_names: list[str] = []

        original_mkstemp = tempfile.mkstemp

        def capturing_mkstemp(**kwargs):
            fd, name = original_mkstemp(**kwargs)
            tmp_names.append(name)
            return fd, name

        with patch("apm_cli.utils.atomic_io.tempfile.mkstemp", side_effect=capturing_mkstemp):
            with patch(
                "apm_cli.utils.atomic_io.os.fdopen",
                side_effect=OSError("disk full"),
            ):
                with pytest.raises(OSError, match="disk full"):
                    atomic_write_text(target, "data")

        # Tmp file must not remain after failure
        if tmp_names:
            assert not Path(tmp_names[0]).exists()

    def test_exception_propagates_even_when_unlink_fails(self, tmp_path: Path) -> None:
        """If cleanup unlink fails, the original write exception still propagates."""
        target = tmp_path / "fail_unlink.txt"

        with patch("apm_cli.utils.atomic_io.os.fdopen", side_effect=RuntimeError("boom")):
            with patch("apm_cli.utils.atomic_io.os.unlink", side_effect=OSError("locked")):
                with pytest.raises(RuntimeError, match="boom"):
                    atomic_write_text(target, "data")

    def test_target_not_written_when_exception_occurs(self, tmp_path: Path) -> None:
        """If write fails, the target path must not be created."""
        target = tmp_path / "ghost.txt"

        with patch("apm_cli.utils.atomic_io.os.fdopen", side_effect=OSError("nope")):
            with pytest.raises(OSError):
                atomic_write_text(target, "data")

        assert not target.exists()

    # ------------------------------------------------------------------
    # Deterministic LF line endings (cross-platform hash stability)
    # ------------------------------------------------------------------

    def test_crlf_normalized_to_lf(self, tmp_path: Path) -> None:
        """CRLF input is written as LF so on-disk bytes are OS-independent."""
        target = tmp_path / "crlf.txt"
        atomic_write_text(target, "a\r\nb\r\nc")
        assert target.read_bytes() == b"a\nb\nc"

    def test_lf_input_unchanged(self, tmp_path: Path) -> None:
        """LF input round-trips without gaining a carriage return."""
        target = tmp_path / "lf.txt"
        atomic_write_text(target, "a\nb\n")
        assert target.read_bytes() == b"a\nb\n"

    def test_bare_cr_input_unchanged(self, tmp_path: Path) -> None:
        """Bare CR round-trips; only CRLF pairs are normalized."""
        target = tmp_path / "bare-cr.txt"
        atomic_write_text(target, "a\rb\n")
        assert target.read_bytes() == b"a\rb\n"


class TestWriteTextLf:
    """Tests for write_text_lf() (non-atomic, LF-normalizing writer)."""

    def test_creates_file_with_lf(self, tmp_path: Path) -> None:
        """Content is written and CRLF is normalized to LF."""
        target = tmp_path / "out.md"
        write_text_lf(target, "a\r\nb\r\n")
        assert target.read_bytes() == b"a\nb\n"

    def test_lf_input_unchanged(self, tmp_path: Path) -> None:
        """LF-only input is left byte-for-byte intact."""
        target = tmp_path / "out.md"
        write_text_lf(target, "# H\n\ntext\n")
        assert target.read_bytes() == b"# H\n\ntext\n"

    def test_bare_cr_input_unchanged(self, tmp_path: Path) -> None:
        """Bare CR is not normalized so drift-normalizer semantics match."""
        target = tmp_path / "bare-cr.md"
        write_text_lf(target, "a\rb\n")
        assert target.read_bytes() == b"a\rb\n"

    def test_unicode_round_trips(self, tmp_path: Path) -> None:
        """Non-ASCII content survives the UTF-8 + LF write."""
        target = tmp_path / "u.md"
        write_text_lf(target, "café ☕ 日本語\r\n")
        assert target.read_text(encoding="utf-8") == "café ☕ 日本語\n"
