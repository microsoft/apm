"""Unit tests for apm_cli.compilation.constitution module.

Covers:
- find_constitution returns correct path
- read_constitution when file exists: returns content
- read_constitution when file does not exist: returns None
- read_constitution when path is a directory (not a file): returns None
- read_constitution when OSError during read: returns None
- read_constitution is cached (second call doesn't re-read the file)
- clear_constitution_cache clears the cache (second call re-reads)
- Cache isolation between different base directories
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from apm_cli.compilation.constants import CONSTITUTION_RELATIVE_PATH
from apm_cli.compilation.constitution import (
    _constitution_cache,
    clear_constitution_cache,
    find_constitution,
    read_constitution,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_constitution(base_dir: Path, content: str) -> Path:
    """Create the constitution file under base_dir and return its path."""
    constitution_path = base_dir / CONSTITUTION_RELATIVE_PATH
    constitution_path.parent.mkdir(parents=True, exist_ok=True)
    constitution_path.write_text(content, encoding="utf-8")
    return constitution_path


# ---------------------------------------------------------------------------
# find_constitution
# ---------------------------------------------------------------------------


class TestFindConstitution:
    """Tests for find_constitution()."""

    def test_returns_correct_path(self, tmp_path: Path) -> None:
        expected = tmp_path / CONSTITUTION_RELATIVE_PATH
        result = find_constitution(tmp_path)
        assert result == expected

    def test_uses_constitution_relative_path_constant(self, tmp_path: Path) -> None:
        result = find_constitution(tmp_path)
        assert str(result).endswith(CONSTITUTION_RELATIVE_PATH.replace("/", str(Path("/"))))


# ---------------------------------------------------------------------------
# read_constitution
# ---------------------------------------------------------------------------


class TestReadConstitution:
    """Tests for read_constitution()."""

    def setup_method(self) -> None:
        """Clear cache before each test to avoid inter-test contamination."""
        clear_constitution_cache()

    def teardown_method(self) -> None:
        """Always clear cache after each test."""
        clear_constitution_cache()

    # ------------------------------------------------------------------
    # File exists
    # ------------------------------------------------------------------

    def test_returns_content_when_file_exists(self, tmp_path: Path) -> None:
        expected = "# My Constitution\nDo the right thing.\n"
        _write_constitution(tmp_path, expected)
        result = read_constitution(tmp_path)
        assert result == expected

    # ------------------------------------------------------------------
    # File does not exist
    # ------------------------------------------------------------------

    def test_returns_none_when_file_does_not_exist(self, tmp_path: Path) -> None:
        result = read_constitution(tmp_path)
        assert result is None

    # ------------------------------------------------------------------
    # Path is a directory
    # ------------------------------------------------------------------

    def test_returns_none_when_path_is_directory(self, tmp_path: Path) -> None:
        constitution_path = tmp_path / CONSTITUTION_RELATIVE_PATH
        constitution_path.mkdir(parents=True, exist_ok=True)
        result = read_constitution(tmp_path)
        assert result is None

    # ------------------------------------------------------------------
    # OSError
    # ------------------------------------------------------------------

    def test_returns_none_when_oserror_on_read(self, tmp_path: Path) -> None:
        _write_constitution(tmp_path, "irrelevant")

        with patch.object(
            Path,
            "read_text",
            side_effect=OSError("permission denied"),
        ):
            result = read_constitution(tmp_path)

        assert result is None

    # ------------------------------------------------------------------
    # Caching behaviour
    # ------------------------------------------------------------------

    def test_result_is_cached_second_call_does_not_re_read(self, tmp_path: Path) -> None:
        """Second call returns cached value without touching the filesystem.

        Verified by deleting the file between calls: if caching works, the
        second call must still return the original content, not None.
        """
        content = "# Cached constitution\n"
        constitution_path = _write_constitution(tmp_path, content)

        first = read_constitution(tmp_path)
        assert first == content

        # Remove the file — a non-cached call would now return None
        constitution_path.unlink()

        second = read_constitution(tmp_path)
        assert second == content  # still returns cached content

    def test_none_result_is_cached(self, tmp_path: Path) -> None:
        """A None result (file missing) is cached and returned on second call."""
        import apm_cli.compilation.constitution as const_mod

        with patch.object(const_mod, "find_constitution", wraps=find_constitution) as mock_find:
            first = read_constitution(tmp_path)
            second = read_constitution(tmp_path)

        assert first is None
        assert second is None
        # find_constitution still called once (cache keyed on resolved path)
        assert mock_find.call_count == 1

    # ------------------------------------------------------------------
    # clear_constitution_cache
    # ------------------------------------------------------------------

    def test_clear_cache_allows_re_read(self, tmp_path: Path) -> None:
        """After clearing the cache, the file is read again on next call."""
        content_v1 = "# Version 1\n"
        content_v2 = "# Version 2\n"
        _write_constitution(tmp_path, content_v1)

        first = read_constitution(tmp_path)
        assert first == content_v1

        # Overwrite the file and clear the cache
        _write_constitution(tmp_path, content_v2)
        clear_constitution_cache()

        second = read_constitution(tmp_path)
        assert second == content_v2

    def test_clear_cache_empties_dict(self, tmp_path: Path) -> None:
        """_constitution_cache is empty after clear_constitution_cache()."""
        _write_constitution(tmp_path, "content")
        read_constitution(tmp_path)
        assert len(_constitution_cache) >= 1
        clear_constitution_cache()
        assert len(_constitution_cache) == 0

    # ------------------------------------------------------------------
    # Cache isolation between base directories
    # ------------------------------------------------------------------

    def test_cache_isolated_between_base_dirs(self, tmp_path: Path) -> None:
        """Different base directories have independent cache entries."""
        dir_a = tmp_path / "project_a"
        dir_b = tmp_path / "project_b"
        dir_a.mkdir()
        dir_b.mkdir()

        content_a = "# Constitution A\n"
        content_b = "# Constitution B\n"
        _write_constitution(dir_a, content_a)
        _write_constitution(dir_b, content_b)

        result_a = read_constitution(dir_a)
        result_b = read_constitution(dir_b)

        assert result_a == content_a
        assert result_b == content_b

    def test_clearing_cache_affects_all_entries(self, tmp_path: Path) -> None:
        """clear_constitution_cache removes entries for all base directories."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        _write_constitution(dir_a, "A")
        _write_constitution(dir_b, "B")

        read_constitution(dir_a)
        read_constitution(dir_b)
        assert len(_constitution_cache) == 2

        clear_constitution_cache()
        assert len(_constitution_cache) == 0
