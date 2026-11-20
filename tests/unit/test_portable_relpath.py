"""Unit tests for apm_cli.utils.paths.portable_relpath().

Covers:
- Basic relative path computation with forward slashes
- .resolve() on both sides (handles Windows 8.3 short-name mismatches)
- Fallback to POSIX path when path is not under base
- Edge cases: same directory, deeply nested, trailing slashes
"""

import pytest

from apm_cli.utils.paths import portable_relpath

pytestmark = pytest.mark.component


class TestPortableRelpath:
    """Tests for portable_relpath()."""

    @pytest.mark.parametrize(
        ("parts", "expected"),
        [
            (("sub", "file.md"), "sub/file.md"),
            (("a", "b", "c", "d.txt"), "a/b/c/d.txt"),
            ((), "."),
            (("README.md",), "README.md"),
        ],
        ids=["simple-relative", "deeply-nested", "same-directory", "file-in-base"],
    )
    def test_relative_to_base(self, tmp_path, parts, expected):
        """Paths under base return forward-slash relative strings ('.' for base)."""
        child = tmp_path.joinpath(*parts)
        if parts:
            child.parent.mkdir(parents=True, exist_ok=True)
            child.touch()
        assert portable_relpath(child, tmp_path) == expected

    def test_fallback_when_not_under_base(self, tmp_path):
        """Returns POSIX-style resolved path when path is not under base."""
        other = tmp_path / "other"
        other.mkdir()
        base = tmp_path / "base"
        base.mkdir()
        result = portable_relpath(other, base)
        # Should fall back to resolved absolute POSIX path
        assert result == other.resolve().as_posix()
        assert "\\" not in result

    def test_result_never_contains_backslash(self, tmp_path):
        """Returned string never contains backslashes (Windows safety)."""
        child = tmp_path / "src" / "apm_cli" / "utils" / "paths.py"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.touch()
        result = portable_relpath(child, tmp_path)
        assert "\\" not in result
        assert result == "src/apm_cli/utils/paths.py"

    def test_resolve_handles_symlinks(self, tmp_path):
        """Symlinked paths resolve to the same result."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        child = real_dir / "file.txt"
        child.touch()

        link_dir = tmp_path / "link"
        try:
            link_dir.symlink_to(real_dir)
        except OSError:
            pytest.skip("symlinks not supported on this platform")

        linked_child = link_dir / "file.txt"
        # Both should resolve to the same thing
        result_real = portable_relpath(child, tmp_path)
        result_link = portable_relpath(linked_child, tmp_path)
        assert result_real == result_link

    def test_nonexistent_path_still_works(self, tmp_path):
        """Works even if the path doesn't exist on disk (resolve still works)."""
        fake = tmp_path / "does" / "not" / "exist.md"
        result = portable_relpath(fake, tmp_path)
        assert result == "does/not/exist.md"

    def test_return_type_is_str(self, tmp_path):
        """Always returns a str, never a Path."""
        child = tmp_path / "file.txt"
        child.touch()
        result = portable_relpath(child, tmp_path)
        assert isinstance(result, str)
