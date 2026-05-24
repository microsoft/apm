"""Unit tests for runtime binary resolution utilities."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.runtime.utils import find_runtime_binary


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Override Path.home() to return a temporary directory."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


class TestFindRuntimeBinary:
    """Tests for find_runtime_binary utility function."""

    def test_apm_managed_binary_takes_priority(self, fake_home):
        """APM-managed binary should be returned when available and executable.

        When both ~/.apm/runtimes/<name> and a system PATH binary exist,
        the APM-managed one should take priority.
        """
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)
        apm_binary = runtime_dir / "codex"
        apm_binary.touch()
        apm_binary.chmod(0o755)

        with patch("apm_cli.runtime.utils.shutil.which", return_value="/usr/bin/codex"):
            result = find_runtime_binary("codex")

        assert result == str(apm_binary)

    def test_falls_back_to_path_when_no_apm_binary(self, fake_home):
        """System PATH should be consulted when no APM-managed binary exists."""
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)
        # No binary created in ~/.apm/runtimes/

        path_binary = "/usr/bin/codex"
        with patch("apm_cli.runtime.utils.shutil.which", return_value=path_binary):
            result = find_runtime_binary("codex")

        assert result == path_binary

    def test_returns_none_when_neither_exists(self, fake_home):
        """None should be returned when binary doesn't exist anywhere.

        When neither ~/.apm/runtimes/<name> nor system PATH contains the binary,
        the function should return None.
        """
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)
        # No binary in APM directory

        with patch("apm_cli.runtime.utils.shutil.which", return_value=None):
            result = find_runtime_binary("codex")

        assert result is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows does not honor POSIX execute bits; os.access(X_OK) returns True "
        "for any readable file, so the non-executable fallback path is unreachable.",
    )
    def test_skips_non_executable_apm_binary(self, fake_home):
        """Non-executable APM binary should be skipped in favor of PATH.

        When ~/.apm/runtimes/<name> exists but is not executable, the function
        should fall back to checking system PATH.
        """
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)
        apm_binary = runtime_dir / "codex"
        apm_binary.touch()
        # Not executable (mode 0o644)
        apm_binary.chmod(0o644)

        path_binary = "/usr/bin/codex"
        with patch("apm_cli.runtime.utils.shutil.which", return_value=path_binary):
            result = find_runtime_binary("codex")

        assert result == path_binary

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    def test_windows_exe_suffix_priority(self, fake_home):
        """On Windows, .exe suffix should be checked first.

        The function should check ~/.apm/runtimes/<name>.exe before
        ~/.apm/runtimes/<name> on Windows systems.
        """
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)
        apm_binary_exe = runtime_dir / "codex.exe"
        apm_binary_exe.touch()
        apm_binary_exe.chmod(0o755)

        with patch("apm_cli.runtime.utils.shutil.which", return_value="/usr/bin/codex"):
            result = find_runtime_binary("codex")

        assert result == str(apm_binary_exe)

    def test_windows_exe_suffix_falls_back_to_non_exe(self, fake_home):
        """On Windows, if .exe doesn't exist, non-.exe version should be checked.

        When ~/.apm/runtimes/<name>.exe doesn't exist but
        ~/.apm/runtimes/<name> does, the non-.exe version should be used.
        """
        # Use monkeypatch to override sys.platform
        with patch("sys.platform", "win32"):
            runtime_dir = fake_home / ".apm" / "runtimes"
            runtime_dir.mkdir(parents=True)
            apm_binary = runtime_dir / "codex"
            apm_binary.touch()
            apm_binary.chmod(0o755)

            with patch("apm_cli.runtime.utils.shutil.which", return_value=None):
                result = find_runtime_binary("codex")

            assert result == str(apm_binary)

    def test_non_windows_ignores_exe_suffix(self, fake_home):
        """On non-Windows systems, .exe suffix should be ignored.

        The function should only check ~/.apm/runtimes/<name> (without suffix)
        on non-Windows systems.
        """
        with patch("sys.platform", "linux"):
            runtime_dir = fake_home / ".apm" / "runtimes"
            runtime_dir.mkdir(parents=True)
            # Only .exe version exists
            apm_binary_exe = runtime_dir / "codex.exe"
            apm_binary_exe.touch()
            apm_binary_exe.chmod(0o755)

            path_binary = "/usr/bin/codex"
            with patch("apm_cli.runtime.utils.shutil.which", return_value=path_binary):
                result = find_runtime_binary("codex")

            # Should fall back to PATH since .exe suffix is ignored on non-Windows
            assert result == path_binary

    def test_multiple_binary_names(self, fake_home):
        """Function should work correctly with different binary names.

        The priority order should work consistently across different binary names.
        """
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)

        # Test with "python"
        python_binary = runtime_dir / "python"
        python_binary.touch()
        python_binary.chmod(0o755)

        with patch("apm_cli.runtime.utils.shutil.which", return_value=None):
            result = find_runtime_binary("python")

        assert result == str(python_binary)

        # Test with "node"
        node_binary = runtime_dir / "node"
        node_binary.touch()
        node_binary.chmod(0o755)

        with patch("apm_cli.runtime.utils.shutil.which", return_value=None):
            result = find_runtime_binary("node")

        assert result == str(node_binary)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows does not honor POSIX execute bits; os.access(X_OK) returns True "
        "for any readable file, so the non-executable fallback path is unreachable.",
    )
    def test_apm_binary_without_execute_permission_falls_back(self, fake_home):
        """An APM binary without execute permission should trigger fallback.

        os.access(path, os.X_OK) should be checked to ensure executability.
        """
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)
        apm_binary = runtime_dir / "codex"
        apm_binary.touch()
        apm_binary.chmod(0o600)  # Read/write but not execute

        path_binary = "/opt/bin/codex"
        with patch("apm_cli.runtime.utils.shutil.which", return_value=path_binary):
            result = find_runtime_binary("codex")

        assert result == path_binary

    def test_apm_runtimes_dir_creation(self, fake_home):
        """Function should handle missing .apm/runtimes directory gracefully.

        If ~/.apm/runtimes doesn't exist, the function should not crash
        and should return the system PATH result.
        """
        # Don't create the directory - leave it as non-existent
        path_binary = "/usr/bin/codex"
        with patch("apm_cli.runtime.utils.shutil.which", return_value=path_binary):
            result = find_runtime_binary("codex")

        assert result == path_binary

    def test_returns_string_path_not_path_object(self, fake_home):
        """Function should return a string path, not a Path object.

        The return value should be a string for compatibility with subprocess calls.
        """
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)
        apm_binary = runtime_dir / "codex"
        apm_binary.touch()
        apm_binary.chmod(0o755)

        with patch("apm_cli.runtime.utils.shutil.which", return_value=None):
            result = find_runtime_binary("codex")

        assert isinstance(result, str)
        assert result == str(apm_binary)


class TestFindRuntimeBinaryPathSecurity:
    """Tests for path-traversal security in find_runtime_binary."""

    def test_rejects_dotdot_traversal(self, fake_home):
        """Names with ``..`` segments must raise PathTraversalError."""
        from apm_cli.utils.path_security import PathTraversalError

        with pytest.raises(PathTraversalError):
            find_runtime_binary("../../etc/passwd")

    def test_rejects_name_with_forward_slash(self, fake_home):
        """Names containing '/' must raise PathTraversalError."""
        from apm_cli.utils.path_security import PathTraversalError

        with pytest.raises(PathTraversalError):
            find_runtime_binary("some/path")

    def test_rejects_name_with_backslash(self, fake_home):
        """Names containing '\\' must raise PathTraversalError."""
        from apm_cli.utils.path_security import PathTraversalError

        with pytest.raises(PathTraversalError):
            find_runtime_binary("some\\path")

    def test_rejects_absolute_path_as_name(self, fake_home):
        """Absolute paths passed as a name must raise PathTraversalError."""
        from apm_cli.utils.path_security import PathTraversalError

        with pytest.raises(PathTraversalError):
            find_runtime_binary("/usr/bin/codex")

    def test_rejects_dotdot_url_encoded(self, fake_home):
        """URL-encoded traversal sequences must also be rejected."""
        from apm_cli.utils.path_security import PathTraversalError

        with pytest.raises(PathTraversalError):
            find_runtime_binary("%2e%2e/etc/passwd")

    def test_valid_simple_name_does_not_raise(self, fake_home):
        """A plain binary name (no separators) must not raise any exception."""
        with patch("apm_cli.runtime.utils.shutil.which", return_value=None):
            # Should not raise; result is None because binary is absent
            result = find_runtime_binary("codex")
        assert result is None

    def test_symlink_outside_runtimes_dir_is_rejected(self, fake_home):
        """An APM binary that is a symlink pointing outside runtimes must be skipped."""
        import os

        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True)

        # Create a real executable outside the runtimes dir
        outside_binary = fake_home / "evil_codex"
        outside_binary.write_text("#!/bin/sh\n")
        outside_binary.chmod(0o755)

        # Symlink inside runtimes pointing outside
        apm_link = runtime_dir / "codex"
        os.symlink(outside_binary, apm_link)

        path_binary = "/usr/bin/codex"
        with patch("apm_cli.runtime.utils.shutil.which", return_value=path_binary):
            result = find_runtime_binary("codex")

        # The symlink escapes runtimes dir, so must fall back to PATH
        assert result == path_binary


class TestFindRuntimeBinaryWindowsExe:
    """Tests for Windows .exe suffix handling in find_runtime_binary."""

    def test_finds_exe_binary_on_windows(self, fake_home) -> None:
        """On Windows, <name>.exe is found in ~/.apm/runtimes/ before falling back."""

        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        apm_exe = runtime_dir / "codex.exe"
        apm_exe.touch()
        apm_exe.chmod(0o755)

        with (
            patch("sys.platform", "win32"),
            patch("apm_cli.runtime.utils.shutil.which", return_value=None),
        ):
            result = find_runtime_binary("codex")

        assert result == str(apm_exe)

    def test_skips_exe_on_non_windows(self, fake_home) -> None:
        """On non-Windows, .exe binary is not checked."""
        runtime_dir = fake_home / ".apm" / "runtimes"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        apm_exe = runtime_dir / "codex.exe"
        apm_exe.touch()
        apm_exe.chmod(0o755)
        # No codex (without .exe) exists

        with (
            patch("sys.platform", "linux"),
            patch("apm_cli.runtime.utils.shutil.which", return_value=None),
        ):
            result = find_runtime_binary("codex")

        assert result is None
