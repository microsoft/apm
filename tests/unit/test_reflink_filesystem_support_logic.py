"""tests for apm_cli.utils.reflink (pure logic cases)."""

from __future__ import annotations

import errno
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Reset module-level cache state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_reflink_state():
    """Reset module-level capability cache + clonefile state before each test."""
    import apm_cli.utils.reflink as rl

    rl._reset_capability_cache()
    rl._clonefile_fn = None
    rl._clonefile_loaded = False
    yield
    rl._reset_capability_cache()
    rl._clonefile_fn = None
    rl._clonefile_loaded = False


# ---------------------------------------------------------------------------
# _load_macos_clonefile
# ---------------------------------------------------------------------------


class TestLoadMacosClonefile:
    def test_already_loaded_returns_cached_fn(self):
        import apm_cli.utils.reflink as rl

        sentinel = object()
        rl._clonefile_fn = sentinel
        rl._clonefile_loaded = True
        result = rl._load_macos_clonefile()
        assert result is sentinel

    def test_libc_path_none_returns_none(self):
        import apm_cli.utils.reflink as rl

        with patch("ctypes.util.find_library", return_value=None):
            result = rl._load_macos_clonefile()
        assert result is None
        assert rl._clonefile_loaded is True

    def test_attribute_error_returns_none(self):
        import apm_cli.utils.reflink as rl

        mock_libc = MagicMock()
        mock_libc.clonefile = property(
            lambda s: (_ for _ in ()).throw(AttributeError("no clonefile"))
        )

        with (
            patch("ctypes.util.find_library", return_value="/usr/lib/libc.dylib"),
            patch("ctypes.CDLL", side_effect=AttributeError("no attr")),
        ):
            result = rl._load_macos_clonefile()
        assert result is None
        assert rl._clonefile_loaded is True

    def test_os_error_on_cdll_returns_none(self):
        import apm_cli.utils.reflink as rl

        with (
            patch("ctypes.util.find_library", return_value="/usr/lib/libc.dylib"),
            patch("ctypes.CDLL", side_effect=OSError("cannot open")),
        ):
            result = rl._load_macos_clonefile()
        assert result is None
        assert rl._clonefile_loaded is True

    def test_double_check_lock_skips_reentry(self):
        """Second call returns cached result without re-entering the lock body."""
        import apm_cli.utils.reflink as rl

        mock_fn = MagicMock()
        rl._clonefile_fn = mock_fn
        rl._clonefile_loaded = True

        result = rl._load_macos_clonefile()
        assert result is mock_fn

    def test_success_sets_fn(self):
        import apm_cli.utils.reflink as rl

        mock_fn = MagicMock()
        mock_libc = MagicMock()
        mock_libc.clonefile = mock_fn

        with (
            patch("ctypes.util.find_library", return_value="/usr/lib/libc.dylib"),
            patch("ctypes.CDLL", return_value=mock_libc),
        ):
            result = rl._load_macos_clonefile()
        assert result is mock_fn
        assert rl._clonefile_loaded is True


# ---------------------------------------------------------------------------
# _clone_macos
# ---------------------------------------------------------------------------


class TestCloneMacos:
    def test_fn_none_returns_false(self):
        import apm_cli.utils.reflink as rl

        with patch.object(rl, "_load_macos_clonefile", return_value=None):
            assert rl._clone_macos("/src", "/dst") is False

    def test_success_returns_true(self):
        import apm_cli.utils.reflink as rl

        mock_fn = MagicMock(return_value=0)
        with patch.object(rl, "_load_macos_clonefile", return_value=mock_fn):
            assert rl._clone_macos("/src", "/dst") is True

    def test_unsupported_errno_marks_device(self):
        import apm_cli.utils.reflink as rl

        mock_fn = MagicMock(return_value=-1)
        with (
            patch.object(rl, "_load_macos_clonefile", return_value=mock_fn),
            patch("ctypes.get_errno", return_value=errno.ENOTSUP),
            patch.object(rl, "_mark_device_unsupported") as mock_mark,
        ):
            result = rl._clone_macos("/src", "/dst")
        assert result is False
        mock_mark.assert_called_once_with("/dst")

    def test_other_errno_does_not_mark_device(self):
        import apm_cli.utils.reflink as rl

        mock_fn = MagicMock(return_value=-1)
        with (
            patch.object(rl, "_load_macos_clonefile", return_value=mock_fn),
            patch("ctypes.get_errno", return_value=errno.EACCES),
            patch.object(rl, "_mark_device_unsupported") as mock_mark,
        ):
            result = rl._clone_macos("/src", "/dst")
        assert result is False
        mock_mark.assert_not_called()

    def test_eopnotsupp_marks_device(self):
        import apm_cli.utils.reflink as rl

        mock_fn = MagicMock(return_value=-1)
        with (
            patch.object(rl, "_load_macos_clonefile", return_value=mock_fn),
            patch("ctypes.get_errno", return_value=errno.EOPNOTSUPP),
            patch.object(rl, "_mark_device_unsupported") as mock_mark,
        ):
            rl._clone_macos("/src", "/dst")
        mock_mark.assert_called_once()


# ---------------------------------------------------------------------------
# _clone_linux
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _device_for
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Capability cache helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reflink_supported
# ---------------------------------------------------------------------------


class TestReflinkSupported:
    def test_apm_no_reflink_env_returns_false(self):
        import apm_cli.utils.reflink as rl

        with patch.dict(os.environ, {"APM_NO_REFLINK": "1"}):
            assert rl.reflink_supported() is False

    def test_darwin_with_clonefile_returns_true(self):
        import apm_cli.utils.reflink as rl

        mock_fn = MagicMock()
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(rl, "_load_macos_clonefile", return_value=mock_fn),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_NO_REFLINK", None)
            result = rl.reflink_supported()
        assert result is True

    def test_darwin_without_clonefile_returns_false(self):
        import apm_cli.utils.reflink as rl

        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(rl, "_load_macos_clonefile", return_value=None),
        ):
            os.environ.pop("APM_NO_REFLINK", None)
            result = rl.reflink_supported()
        assert result is False

    def test_linux_returns_true(self):
        import apm_cli.utils.reflink as rl

        with patch.object(sys, "platform", "linux"):
            os.environ.pop("APM_NO_REFLINK", None)
            result = rl.reflink_supported()
        assert result is True

    def test_windows_returns_false(self):
        import apm_cli.utils.reflink as rl

        with patch.object(sys, "platform", "win32"):
            os.environ.pop("APM_NO_REFLINK", None)
            result = rl.reflink_supported()
        assert result is False


# ---------------------------------------------------------------------------
# clone_file
# ---------------------------------------------------------------------------
