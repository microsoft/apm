"""Unit tests for apm_cli.utils.paths.portable_relpath().

Covers:
- Normal case: path under base returns relative POSIX path
- Path not under base: returns absolute POSIX path
- Nested path under base: correct relative POSIX path
- ValueError from relative_to falls back to resolved absolute POSIX
- OSError on first resolve: falls back to unresolved as_posix()
- Forward slashes used on all paths (no OS-specific separators)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from apm_cli.utils.paths import portable_relpath


class TestPortableRelpath:
    """Tests for portable_relpath()."""

    # ------------------------------------------------------------------
    # Happy-path: path IS under base
    # ------------------------------------------------------------------

    def test_simple_relative(self, tmp_path: Path) -> None:
        """Direct child path returns a single-segment POSIX string."""
        child = tmp_path / "sub" / "file.md"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.touch()
        result = portable_relpath(child, tmp_path)
        assert result == "sub/file.md"

    def test_deeply_nested_relative(self, tmp_path: Path) -> None:
        """Deeply nested path uses forward slashes throughout."""
        child = tmp_path / "a" / "b" / "c" / "d.txt"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.touch()
        result = portable_relpath(child, tmp_path)
        assert result == "a/b/c/d.txt"

    def test_file_directly_in_base(self, tmp_path: Path) -> None:
        """File directly under base returns just the filename."""
        child = tmp_path / "readme.txt"
        child.touch()
        result = portable_relpath(child, tmp_path)
        assert result == "readme.txt"

    def test_same_directory_returns_dot(self, tmp_path: Path) -> None:
        """Path equal to base resolves to '.'."""
        result = portable_relpath(tmp_path, tmp_path)
        assert result == "."

    def test_no_backslashes_in_result(self, tmp_path: Path) -> None:
        """Result must never contain backslashes."""
        child = tmp_path / "x" / "y" / "z.txt"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.touch()
        result = portable_relpath(child, tmp_path)
        assert "\\" not in result

    # ------------------------------------------------------------------
    # Path NOT under base: falls back to absolute POSIX
    # ------------------------------------------------------------------

    def test_path_not_under_base_returns_absolute(self, tmp_path: Path) -> None:
        """If path is not relative to base, the resolved absolute POSIX path is returned."""
        base = tmp_path / "base_dir"
        base.mkdir()
        unrelated = tmp_path / "other" / "file.txt"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.touch()
        result = portable_relpath(unrelated, base)
        # Must be absolute (starts with /)
        assert result.startswith("/")
        # Must use forward slashes
        assert "\\" not in result
        # Must contain the filename
        assert "file.txt" in result

    # ------------------------------------------------------------------
    # ValueError fallback (relative_to raises)
    # ------------------------------------------------------------------

    def test_value_error_falls_back_to_resolved_absolute(self, tmp_path: Path) -> None:
        """ValueError from relative_to() triggers fallback to resolved absolute path."""
        child = tmp_path / "nested" / "f.txt"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.touch()

        resolved_child = child.resolve()

        # Patch relative_to on the resolved object to raise ValueError
        original_resolve = Path.resolve

        def patched_resolve(self, **kwargs):  # type: ignore[override]
            result = original_resolve(self, **kwargs)

            class _NoRelativeTo(type(result)):
                def relative_to(self, *args, **kw):  # type: ignore[override]
                    raise ValueError("not relative")

            # Re-bind as subclass instance only for child path
            if str(result) == str(resolved_child):
                obj = _NoRelativeTo(str(result))
                return obj
            return result

        with patch.object(Path, "resolve", patched_resolve):
            result = portable_relpath(child, tmp_path)

        # Falls back to resolved absolute posix
        assert result.startswith("/")
        assert "f.txt" in result

    # ------------------------------------------------------------------
    # OSError fallback (resolve raises)
    # ------------------------------------------------------------------

    def test_oserror_on_resolve_falls_back_to_as_posix(self, tmp_path: Path) -> None:
        """OSError from resolve() falls back to unresolved as_posix()."""
        child = tmp_path / "f.txt"
        child.touch()

        call_count = 0

        original_resolve = Path.resolve

        def patched_resolve(self, **kwargs):  # type: ignore[override]
            nonlocal call_count
            call_count += 1
            # Raise on first call (child.resolve()), succeed on base.resolve()
            if call_count == 1:
                raise OSError("resolve failed")
            return original_resolve(self, **kwargs)

        with patch.object(Path, "resolve", patched_resolve):
            result = portable_relpath(child, tmp_path)

        # Fell back to child.as_posix() (unresolved)
        assert "f.txt" in result

    # ------------------------------------------------------------------
    # RuntimeError fallback
    # ------------------------------------------------------------------

    def test_runtime_error_on_relative_to_falls_back(self, tmp_path: Path) -> None:
        """RuntimeError from relative_to triggers fallback."""
        child = tmp_path / "a.txt"
        child.touch()

        resolved_child = child.resolve()
        original_resolve = Path.resolve

        def patched_resolve(self, **kwargs):  # type: ignore[override]
            result = original_resolve(self, **kwargs)

            class _RuntimeRelativeTo(type(result)):
                def relative_to(self, *args, **kw):  # type: ignore[override]
                    raise RuntimeError("cross-device")

            if str(result) == str(resolved_child):
                return _RuntimeRelativeTo(str(result))
            return result

        with patch.object(Path, "resolve", patched_resolve):
            result = portable_relpath(child, tmp_path)

        assert "a.txt" in result

    def test_double_oserror_falls_back_to_as_posix(self, tmp_path: Path) -> None:
        """When both resolve() calls raise, returns path.as_posix() (deepest fallback)."""
        child = tmp_path / "f.txt"
        child.touch()

        original_resolve = Path.resolve  # noqa: F841

        def always_raise(self, **kwargs):  # type: ignore[override]
            raise OSError("resolve always fails")

        with patch.object(Path, "resolve", always_raise):
            result = portable_relpath(child, tmp_path)

        # Must fall back to unresolved as_posix
        assert "f.txt" in result
        assert "\\" not in result
