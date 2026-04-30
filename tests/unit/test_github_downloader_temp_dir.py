"""Tests for PermissionError / OSError handling in GitHubPackageDownloader.

Covers the temp-dir configuration feature requirement that PermissionError
(and OSError errno=13 / winerror=5) raised during download_subdirectory_package
are converted to RuntimeError with an actionable 'apm config set temp-dir'
suggestion, while other OSError variants propagate unchanged.
"""

import errno
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_downloader():
    """Create a GitHubPackageDownloader with stubbed auth (no network needed)."""
    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            return_value=None,
        ),
    ):
        return GitHubPackageDownloader()


def _make_virtual_dep_ref(subdir="src/agent", ref="main"):
    """Return a MagicMock dep_ref that passes is_virtual subdirectory checks."""
    dep_ref = MagicMock()
    dep_ref.is_virtual = True
    dep_ref.virtual_path = subdir
    dep_ref.is_virtual_subdirectory.return_value = True
    dep_ref.reference = ref
    return dep_ref


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDownloadSubdirectoryPermissionError:
    """PermissionError / OSError handling in download_subdirectory_package."""

    def _invoke_with_mkdtemp_error(self, downloader, dep_ref, side_effect, tmp_path):
        """Invoke download_subdirectory_package with a mocked mkdtemp that
        raises *side_effect*, returning whatever exception propagates."""
        with (
            patch(
                "apm_cli.deps.github_downloader.tempfile.mkdtemp",
                side_effect=side_effect,
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            return downloader.download_subdirectory_package(dep_ref, tmp_path / "target")

    def test_permission_error_raises_runtime_error_with_suggestion(self, tmp_path):
        """PermissionError from mkdtemp is converted to RuntimeError with temp-dir hint."""
        downloader = _make_downloader()
        dep_ref = _make_virtual_dep_ref()
        with pytest.raises(RuntimeError) as exc_info:
            self._invoke_with_mkdtemp_error(
                downloader, dep_ref, PermissionError("denied"), tmp_path
            )
        msg = str(exc_info.value)
        assert "apm config set temp-dir" in msg

    def test_permission_error_message_mentions_access_denied(self, tmp_path):
        """RuntimeError message explains that access was denied."""
        downloader = _make_downloader()
        dep_ref = _make_virtual_dep_ref()
        with pytest.raises(RuntimeError) as exc_info:
            self._invoke_with_mkdtemp_error(
                downloader, dep_ref, PermissionError("denied"), tmp_path
            )
        assert "Access denied" in str(exc_info.value)

    def test_oserror_errno13_raises_runtime_error_with_suggestion(self, tmp_path):
        """OSError with errno=13 (EACCES) from mkdtemp is converted to RuntimeError."""
        downloader = _make_downloader()
        dep_ref = _make_virtual_dep_ref()
        exc = OSError("Permission denied")
        exc.errno = 13
        with pytest.raises(RuntimeError) as exc_info:
            self._invoke_with_mkdtemp_error(downloader, dep_ref, exc, tmp_path)
        assert "apm config set temp-dir" in str(exc_info.value)

    def test_oserror_winerror5_raises_runtime_error_with_suggestion(self, tmp_path):
        """OSError with winerror=5 (ERROR_ACCESS_DENIED) from mkdtemp is converted to RuntimeError."""
        downloader = _make_downloader()
        dep_ref = _make_virtual_dep_ref()
        exc = OSError("Access is denied")
        exc.winerror = 5
        with pytest.raises(RuntimeError) as exc_info:
            self._invoke_with_mkdtemp_error(downloader, dep_ref, exc, tmp_path)
        assert "apm config set temp-dir" in str(exc_info.value)

    def test_other_oserror_is_reraised(self, tmp_path):
        """OSError with unrelated errno is NOT caught and propagates unchanged."""
        downloader = _make_downloader()
        dep_ref = _make_virtual_dep_ref()
        exc = OSError("Disk full")
        exc.errno = errno.ENOSPC
        with pytest.raises(OSError) as exc_info:
            self._invoke_with_mkdtemp_error(downloader, dep_ref, exc, tmp_path)
        assert exc_info.value is exc

    def test_permission_error_chain_is_suppressed(self, tmp_path):
        """RuntimeError suppress_context is True (raised with 'from None')."""
        downloader = _make_downloader()
        dep_ref = _make_virtual_dep_ref()
        with pytest.raises(RuntimeError) as exc_info:
            self._invoke_with_mkdtemp_error(
                downloader, dep_ref, PermissionError("denied"), tmp_path
            )
        # 'raise ... from None' sets __suppress_context__ so the original
        # PermissionError is hidden in tracebacks even though __context__ is set.
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True

    def test_permission_error_from_mkdtemp_omits_path_in_message(self, tmp_path):
        """When mkdtemp itself fails, temp_dir is None so message omits a path."""
        downloader = _make_downloader()
        dep_ref = _make_virtual_dep_ref()
        with pytest.raises(RuntimeError) as exc_info:
            self._invoke_with_mkdtemp_error(
                downloader, dep_ref, PermissionError("denied"), tmp_path
            )
        msg = str(exc_info.value)
        assert "Access denied in temporary directory." in msg
        # No quoted path when mkdtemp never created the directory
        assert "'" not in msg

    def test_permission_error_on_target_path_is_reraised(self, tmp_path):
        """PermissionError whose filename is outside temp_dir re-raises."""
        downloader = _make_downloader()
        dep_ref = _make_virtual_dep_ref()
        fake_temp = str(tmp_path / "faketemp")
        target_exc = PermissionError("denied")
        target_exc.filename = "/other/path/outside"

        with (
            patch(
                "apm_cli.deps.github_downloader.tempfile.mkdtemp",
                return_value=fake_temp,
            ),
            patch.object(
                downloader,
                "_try_sparse_checkout",
                side_effect=target_exc,
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            with pytest.raises(PermissionError) as exc_info:
                downloader.download_subdirectory_package(dep_ref, tmp_path / "target")
            assert exc_info.value is target_exc

    def test_invalid_dep_ref_not_virtual_raises_value_error(self):
        """Passing a non-virtual dep_ref raises ValueError before any git ops."""
        downloader = _make_downloader()
        dep_ref = MagicMock()
        dep_ref.is_virtual = False
        dep_ref.virtual_path = None
        with pytest.raises(ValueError, match="virtual subdirectory package"):
            downloader.download_subdirectory_package(dep_ref, Path("/tmp/target"))
