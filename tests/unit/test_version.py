"""Unit tests for apm_cli.version module.

Covers all branches of get_version() and get_build_sha():
- Module-level build constants (frozen distribution)
- importlib.metadata happy-path and PackageNotFoundError
- Frozen mode (PyInstaller) reading pyproject.toml from sys._MEIPASS
- Non-frozen mode reading pyproject.toml relative to __file__
- Invalid / missing pyproject.toml fallback to "unknown"
- get_build_sha() via git subprocess and frozen-mode short-circuit
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import apm_cli.version as version_mod
from apm_cli.version import get_build_sha, get_version

# ---------------------------------------------------------------------------
# get_version()
# ---------------------------------------------------------------------------


class TestGetVersionBuildConstant:
    """get_version() returns __BUILD_VERSION__ when it is set."""

    def test_returns_build_version_constant(self) -> None:
        with patch.object(version_mod, "__BUILD_VERSION__", "1.2.3"):
            assert get_version() == "1.2.3"

    def test_build_version_takes_priority_over_importlib(self) -> None:
        """Build constant must be checked before importlib.metadata."""
        with patch.object(version_mod, "__BUILD_VERSION__", "9.9.9"):
            # even if importlib would succeed, we never reach it
            with patch("importlib.metadata.version", return_value="0.0.1") as mock_meta:
                assert get_version() == "9.9.9"
                mock_meta.assert_not_called()


class TestGetVersionImportlibMetadata:
    """get_version() uses importlib.metadata when not frozen and no constant."""

    def test_importlib_metadata_success(self) -> None:
        # version is locally imported inside get_version(), so patch its source
        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch("importlib.metadata.version", return_value="3.1.4"):
                    assert get_version() == "3.1.4"

    def test_importlib_metadata_legacy_pre38_path(self) -> None:
        """Line 34: Python < 3.8 falls back to importlib_metadata backport."""
        import types

        fake_pkg_not_found = type("PackageNotFoundError", (Exception,), {})
        fake_importlib_metadata = types.ModuleType("importlib_metadata")
        fake_importlib_metadata.PackageNotFoundError = fake_pkg_not_found
        fake_importlib_metadata.version = MagicMock(return_value="7.8.9")

        mock_sys = MagicMock()
        mock_sys.frozen = False
        mock_sys.version_info = (3, 7, 0)  # triggers the else-branch

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("apm_cli.version.sys", mock_sys):
                with patch.dict("sys.modules", {"importlib_metadata": fake_importlib_metadata}):
                    result = get_version()
        assert result == "7.8.9"

    def test_importlib_metadata_package_not_found_falls_through(self, tmp_path: Path) -> None:
        """PackageNotFoundError triggers pyproject.toml fallback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.poetry]\nversion = "2.0.0"\n', encoding="utf-8")
        from importlib.metadata import PackageNotFoundError

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch(
                    "importlib.metadata.version",
                    side_effect=PackageNotFoundError("apm-cli"),
                ):
                    with patch.object(
                        version_mod,
                        "__file__",
                        str(tmp_path / "src" / "apm_cli" / "version.py"),
                    ):
                        result = get_version()
                        assert result == "2.0.0"


class TestGetVersionFrozenMode:
    """get_version() reads pyproject.toml from sys._MEIPASS in frozen mode."""

    def test_frozen_mode_reads_meipass_pyproject(self, tmp_path: Path) -> None:
        meipass_dir = tmp_path / "meipass"
        meipass_dir.mkdir()
        (meipass_dir / "pyproject.toml").write_text('version = "5.6.7"\n', encoding="utf-8")
        mock_sys = MagicMock()
        mock_sys.frozen = True
        mock_sys._MEIPASS = str(meipass_dir)
        mock_sys.version_info = sys.version_info
        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("apm_cli.version.sys", mock_sys):
                result = get_version()
        assert result == "5.6.7"

    def test_frozen_mode_meipass_pyproject_missing_returns_unknown(self, tmp_path: Path) -> None:
        meipass_dir = tmp_path / "meipass_empty"
        meipass_dir.mkdir()
        mock_sys = MagicMock()
        mock_sys.frozen = True
        mock_sys._MEIPASS = str(meipass_dir)
        mock_sys.version_info = sys.version_info
        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("apm_cli.version.sys", mock_sys):
                result = get_version()
        assert result == "unknown"


class TestGetVersionPyprojectToml:
    """get_version() pyproject.toml path in non-frozen mode."""

    def test_pyproject_valid_version(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('version = "1.0.0"\n', encoding="utf-8")
        from importlib.metadata import PackageNotFoundError

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch(
                    "importlib.metadata.version",
                    side_effect=PackageNotFoundError("apm-cli"),
                ):
                    fake_file = str(tmp_path / "src" / "apm_cli" / "version.py")
                    with patch.object(version_mod, "__file__", fake_file):
                        result = get_version()
        assert result == "1.0.0"

    def test_pyproject_does_not_exist_returns_unknown(self, tmp_path: Path) -> None:
        from importlib.metadata import PackageNotFoundError

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch(
                    "importlib.metadata.version",
                    side_effect=PackageNotFoundError("apm-cli"),
                ):
                    # Point __file__ to a directory where pyproject.toml
                    # does not exist three levels up
                    fake_file = str(tmp_path / "empty" / "src" / "apm_cli" / "version.py")
                    with patch.object(version_mod, "__file__", fake_file):
                        result = get_version()
        assert result == "unknown"

    def test_pyproject_invalid_version_format_returns_unknown(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        # Version string that fails the regex guard
        pyproject.write_text('version = "not-a-version"\n', encoding="utf-8")
        from importlib.metadata import PackageNotFoundError

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch(
                    "importlib.metadata.version",
                    side_effect=PackageNotFoundError("apm-cli"),
                ):
                    fake_file = str(tmp_path / "src" / "apm_cli" / "version.py")
                    with patch.object(version_mod, "__file__", fake_file):
                        result = get_version()
        assert result == "unknown"

    def test_pyproject_oserror_returns_unknown(self, tmp_path: Path) -> None:
        """OSError while reading pyproject.toml falls back to 'unknown'."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('version = "1.0.0"\n', encoding="utf-8")
        from importlib.metadata import PackageNotFoundError

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch(
                    "importlib.metadata.version",
                    side_effect=PackageNotFoundError("apm-cli"),
                ):
                    fake_file = str(tmp_path / "src" / "apm_cli" / "version.py")
                    with patch.object(version_mod, "__file__", fake_file):
                        with patch("builtins.open", side_effect=OSError("permission denied")):
                            result = get_version()
        assert result == "unknown"

    @pytest.mark.parametrize(
        "version_str",
        [
            "0.0.1",
            "1.2.3",
            "10.20.30",
            "1.0.0a1",
            "2.0.0b2",
            "3.1.4rc1",
        ],
    )
    def test_valid_version_formats_accepted(self, tmp_path: Path, version_str: str) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(f'version = "{version_str}"\n', encoding="utf-8")
        from importlib.metadata import PackageNotFoundError

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch(
                    "importlib.metadata.version",
                    side_effect=PackageNotFoundError("apm-cli"),
                ):
                    fake_file = str(tmp_path / "src" / "apm_cli" / "version.py")
                    with patch.object(version_mod, "__file__", fake_file):
                        result = get_version()
        assert result == version_str


# ---------------------------------------------------------------------------
# get_build_sha()
# ---------------------------------------------------------------------------


class TestGetBuildSha:
    """Tests for get_build_sha()."""

    def test_returns_build_sha_constant_when_set(self) -> None:
        with patch.object(version_mod, "__BUILD_SHA__", "abc1234"):
            assert get_build_sha() == "abc1234"

    def test_build_sha_constant_skips_git(self) -> None:
        with patch.object(version_mod, "__BUILD_SHA__", "deadbeef"):
            with patch("subprocess.run") as mock_run:
                result = get_build_sha()
                mock_run.assert_not_called()
        assert result == "deadbeef"

    def test_git_success_returns_sha(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "d1630d1\n"
        with patch.object(version_mod, "__BUILD_SHA__", None):
            with patch("sys.frozen", False, create=True):
                # subprocess is locally imported inside get_build_sha()
                with patch("subprocess.run", return_value=mock_result):
                    result = get_build_sha()
        assert result == "d1630d1"

    def test_git_failure_returns_empty_string(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        with patch.object(version_mod, "__BUILD_SHA__", None):
            with patch("sys.frozen", False, create=True):
                with patch("subprocess.run", return_value=mock_result):
                    result = get_build_sha()
        assert result == ""

    def test_subprocess_exception_returns_empty_string(self) -> None:
        with patch.object(version_mod, "__BUILD_SHA__", None):
            with patch("sys.frozen", False, create=True):
                with patch(
                    "subprocess.run",
                    side_effect=FileNotFoundError("git not found"),
                ):
                    result = get_build_sha()
        assert result == ""

    def test_frozen_mode_returns_empty_string(self) -> None:
        mock_sys = MagicMock()
        mock_sys.frozen = True
        mock_sys._MEIPASS = "/fake/meipass"
        mock_sys.version_info = sys.version_info
        with patch.object(version_mod, "__BUILD_SHA__", None):
            with patch("apm_cli.version.sys", mock_sys):
                with patch("subprocess.run") as mock_run:
                    result = get_build_sha()
                    mock_run.assert_not_called()
        assert result == ""

    def test_git_called_with_correct_args(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abcdef0\n"
        with patch.object(version_mod, "__BUILD_SHA__", None):
            with patch("sys.frozen", False, create=True):
                with patch("subprocess.run", return_value=mock_result) as mock_run:
                    get_build_sha()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["git", "rev-parse", "--short", "HEAD"]
        assert call_args[1].get("timeout") == 5

    def test_pyproject_no_version_field_returns_unknown(self, tmp_path: Path) -> None:
        """pyproject.toml that has no version = '...' pattern returns 'unknown'."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.poetry]\nname = 'apm-cli'\n", encoding="utf-8")
        from importlib.metadata import PackageNotFoundError

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch(
                    "importlib.metadata.version",
                    side_effect=PackageNotFoundError("apm-cli"),
                ):
                    fake_file = str(tmp_path / "src" / "apm_cli" / "version.py")
                    with patch.object(version_mod, "__file__", fake_file):
                        result = get_version()
        assert result == "unknown"
