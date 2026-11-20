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
- ConstitutionInjector reading and injecting the constitution on disk
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.compilation.constants import (
    CONSTITUTION_MARKER_BEGIN,
    CONSTITUTION_MARKER_END,
    CONSTITUTION_RELATIVE_PATH,
)
from apm_cli.compilation.constitution import (
    _constitution_cache,
    clear_constitution_cache,
    find_constitution,
    read_constitution,
)
from apm_cli.compilation.constitution_block import compute_constitution_hash, render_block
from apm_cli.compilation.injector import ConstitutionInjector

pytestmark = pytest.mark.component

_BEGIN = CONSTITUTION_MARKER_BEGIN
_END = CONSTITUTION_MARKER_END

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


class TestConstitutionInjector:
    def setup_method(self):
        clear_constitution_cache()

    def _make_injector(self, base_dir: Path) -> ConstitutionInjector:
        return ConstitutionInjector(str(base_dir))

    def test_skipped_no_existing_block(self, tmp_path):
        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"
        compiled = "# Title\n\nBody text.\n"
        final, status, hash_val = injector.inject(compiled, False, output_path)
        assert status == "SKIPPED"
        assert hash_val is None
        assert final == compiled
        assert not output_path.exists()

    def test_skipped_preserves_existing_block(self, tmp_path):
        constitution = "Constitution rule\n"
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text(constitution)

        injector = self._make_injector(tmp_path)
        block = render_block(constitution)
        existing = f"# Title\n\n{block.rstrip()}\n\nBody.\n"
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text(existing)

        final, status, hash_val = injector.inject("# Title\n\nBody.\n", False, output_path)  # noqa: RUF059
        assert status == "SKIPPED"
        assert _BEGIN in final
        assert output_path.read_text() == existing

    def test_missing_when_no_constitution_file(self, tmp_path):
        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"
        final, status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)  # noqa: RUF059
        assert status == "MISSING"
        assert hash_val is None
        assert not output_path.exists()

    def test_missing_preserves_existing_block(self, tmp_path):
        # AGENTS.md already has a block, but no constitution file present
        existing_block = f"{_BEGIN}\nhash: old123\nOld rule\n{_END}\n"
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text(f"# Title\n\n{existing_block}\nBody.\n")

        injector = self._make_injector(tmp_path)
        final, status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)  # noqa: RUF059
        assert status == "MISSING"
        assert _BEGIN in final
        assert output_path.read_text() == f"# Title\n\n{existing_block}\nBody.\n"

    def test_created_with_constitution_file(self, tmp_path):
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text("New rule.\n")

        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"
        final, status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)
        assert status == "CREATED"
        assert hash_val is not None
        assert _BEGIN in final
        assert "New rule." in final
        assert not output_path.exists()

    def test_unchanged_when_same_constitution(self, tmp_path):
        constitution = "Same rule.\n"
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text(constitution)

        injector = self._make_injector(tmp_path)
        # Pre-render exact block
        block = render_block(constitution)
        existing = f"# Title\n\n{block.rstrip()}\n\nBody.\n"
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text(existing)

        final, status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)  # noqa: RUF059
        assert status == "UNCHANGED"
        assert output_path.read_text() == existing

    def test_updated_when_constitution_changes(self, tmp_path):
        old_constitution = "Old rule.\n"
        new_constitution = "New rule.\n"
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text(new_constitution)

        injector = self._make_injector(tmp_path)
        old_block = render_block(old_constitution)
        existing = f"# Title\n\n{old_block.rstrip()}\n\nBody.\n"
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text(existing)

        final, status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)  # noqa: RUF059
        assert status == "UPDATED"
        assert "New rule." in final
        assert "Old rule." not in final
        assert output_path.read_text() == existing

    def test_output_path_not_exists(self, tmp_path):
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text("Rule.\n")

        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "nonexistent.md"
        final, status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)  # noqa: RUF059
        assert status == "CREATED"
        assert not output_path.exists()

    def test_trailing_newline_in_output(self, tmp_path):
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text("Rule.\n")

        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"
        final, status, hash_val = injector.inject("# Title\n\nBody.", True, output_path)  # noqa: RUF059
        assert final.endswith("\n")
        assert not output_path.exists()

    def test_hash_value_extracted_correctly(self, tmp_path):
        constitution = "My rules.\n"
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text(constitution)

        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"
        _, _, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)
        expected = compute_constitution_hash(constitution)
        assert hash_val == expected
        assert not output_path.exists()

    def test_oserror_reading_existing_file_treated_as_empty(self, tmp_path):
        """OSError when reading AGENTS.md is caught; existing_content defaults to '' (lines 37-38)."""
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text("Rule.\n")

        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"
        output_path.write_text("existing content")

        # Patch only this specific file's read_text to raise OSError,
        # while leaving the constitution file readable.
        _orig = Path.read_text

        def _raise_for_output(self, *args, **kwargs):
            if self == output_path:
                raise OSError("permission denied")
            return _orig(self, *args, **kwargs)

        with patch.object(Path, "read_text", _raise_for_output):
            _final, status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)

        # OSError causes existing_content = "" → no existing block → block CREATED
        assert status == "CREATED"
        assert hash_val is not None
        assert output_path.read_text() == "existing content"

    def test_compiled_content_without_double_newline_uses_full_as_header(self, tmp_path):
        """Compiled content lacking \\n\\n causes _split_header to return full content as header (line 48)."""
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text("Rule.\n")

        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"
        # No double-newline: the entire string is treated as the "header"
        compiled = "# Title with no blank line\n"
        final, status, hash_val = injector.inject(compiled, True, output_path)

        assert status == "CREATED"
        assert hash_val is not None
        assert _BEGIN in final
        assert not output_path.exists()

    def test_hash_value_none_when_block_has_single_line(self, tmp_path):
        """When render_block returns a single-line block, hash_value is None (branch 85->90)."""
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text("Rule.\n")

        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"

        with patch("apm_cli.compilation.injector.render_block", return_value="SINGLE_LINE\n"):
            _final, status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)

        assert hash_val is None
        assert status == "CREATED"
        assert not output_path.exists()

    def test_hash_value_none_when_hash_line_has_no_value(self, tmp_path):
        """When hash line has no value after 'hash:', hash_value is None (branch 87->90)."""
        spec_path = tmp_path / ".specify" / "memory"
        spec_path.mkdir(parents=True)
        (spec_path / "constitution.md").write_text("Rule.\n")

        injector = self._make_injector(tmp_path)
        output_path = tmp_path / "AGENTS.md"

        block = f"{_BEGIN}\nhash:\nRule.\n{_END}\n"
        with patch("apm_cli.compilation.injector.render_block", return_value=block):
            _final, _status, hash_val = injector.inject("# Title\n\nBody.\n", True, output_path)

        assert hash_val is None
        assert not output_path.exists()
