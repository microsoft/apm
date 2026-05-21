"""tests for apm_cli.utils.reflink.

Covers the missing branches/lines identified in coverage-unit.json:
- _load_macos_clonefile: double-checked lock, libc_path None, AttributeError
- _clone_macos: fn is None, unsupported errno path
- _clone_linux: all major branches (success, FICLONE ioctl failure, open failure)
- _device_for: OSError path
- _mark_device_supported / _mark_device_unsupported: dev is None
- _mark_device_supported: dev is None
- clone_file: APM_NO_REFLINK env, device-known-unsupported, linux path, darwin path
- reflink_supported: APM_NO_REFLINK, darwin, linux
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


class TestCloneLinux:
    def test_success(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"hello")
        dst = tmp_path / "dst.txt"

        mock_ioctl = MagicMock()
        import fcntl

        with patch.object(fcntl, "ioctl", mock_ioctl):
            result = rl._clone_linux(str(src), str(dst))

        assert result is True

    def test_ioctl_unsupported_errno_returns_false(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"

        err = OSError()
        err.errno = errno.EOPNOTSUPP

        import fcntl

        with (
            patch.object(fcntl, "ioctl", side_effect=err),
            patch.object(rl, "_mark_device_unsupported") as mock_mark,
        ):
            result = rl._clone_linux(str(src), str(dst))

        assert result is False
        mock_mark.assert_called_once()

    def test_ioctl_other_errno_returns_false(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"

        err = OSError()
        err.errno = errno.EIO

        import fcntl

        with (
            patch.object(fcntl, "ioctl", side_effect=err),
            patch.object(rl, "_mark_device_unsupported") as mock_mark,
        ):
            result = rl._clone_linux(str(src), str(dst))

        assert result is False
        mock_mark.assert_not_called()

    def test_open_src_fails_returns_false(self, tmp_path):
        """open() of source fails -> returns False."""
        import apm_cli.utils.reflink as rl

        result = rl._clone_linux("/nonexistent/src.txt", str(tmp_path / "dst.txt"))
        assert result is False

    def test_dst_exists_returns_false(self, tmp_path):
        """dst already exists -> O_EXCL on open fails -> returns False."""
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"
        dst.write_bytes(b"existing")

        result = rl._clone_linux(str(src), str(dst))
        # dst exists -> O_EXCL fails -> False
        assert result is False


# ---------------------------------------------------------------------------
# _device_for
# ---------------------------------------------------------------------------


class TestDeviceFor:
    def test_stat_failure_returns_none(self):
        import apm_cli.utils.reflink as rl

        with patch("os.stat", side_effect=OSError("stat failed")):
            result = rl._device_for("/nonexistent/path/file.txt")
        assert result is None

    def test_stat_success_returns_dev(self, tmp_path):
        import apm_cli.utils.reflink as rl

        result = rl._device_for(str(tmp_path / "file.txt"))
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Capability cache helpers
# ---------------------------------------------------------------------------


class TestCapabilityCache:
    def test_mark_device_unsupported_none_dev(self):
        import apm_cli.utils.reflink as rl

        with patch.object(rl, "_device_for", return_value=None):
            rl._mark_device_unsupported("/any/path")
        # Should not raise and cache should remain empty
        assert rl._device_capability == {}

    def test_mark_device_supported_none_dev(self):
        import apm_cli.utils.reflink as rl

        with patch.object(rl, "_device_for", return_value=None):
            rl._mark_device_supported("/any/path")
        assert rl._device_capability == {}

    def test_mark_device_supported_does_not_downgrade(self, tmp_path):
        import apm_cli.utils.reflink as rl

        p = str(tmp_path / "f.txt")
        rl._mark_device_unsupported(p)
        rl._mark_device_supported(p)
        # Should remain False (not upgraded to True)
        dev = rl._device_for(p)
        assert rl._device_capability.get(dev) is False

    def test_is_device_known_unsupported_none_dev(self):
        import apm_cli.utils.reflink as rl

        with patch.object(rl, "_device_for", return_value=None):
            result = rl._is_device_known_unsupported("/any/path")
        assert result is False

    def test_is_device_known_unsupported_true(self, tmp_path):
        import apm_cli.utils.reflink as rl

        p = str(tmp_path / "f.txt")
        rl._mark_device_unsupported(p)
        assert rl._is_device_known_unsupported(p) is True

    def test_is_device_known_unsupported_unknown_device(self, tmp_path):
        import apm_cli.utils.reflink as rl

        p = str(tmp_path / "f.txt")
        # Device not in cache -> not known unsupported
        assert rl._is_device_known_unsupported(p) is False


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


class TestCloneFile:
    def test_apm_no_reflink_returns_false(self, tmp_path):
        import apm_cli.utils.reflink as rl

        with patch.dict(os.environ, {"APM_NO_REFLINK": "1"}):
            result = rl.clone_file(tmp_path / "src", tmp_path / "dst")
        assert result is False

    def test_device_known_unsupported_returns_false(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"

        # Mark device as unsupported
        rl._mark_device_unsupported(str(dst))

        os.environ.pop("APM_NO_REFLINK", None)
        result = rl.clone_file(src, dst)
        assert result is False

    def test_darwin_success_marks_device_supported(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"

        os.environ.pop("APM_NO_REFLINK", None)

        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(rl, "_clone_macos", return_value=True),
            patch.object(rl, "_mark_device_supported") as mock_supported,
        ):
            result = rl.clone_file(src, dst)

        assert result is True
        mock_supported.assert_called_once_with(str(dst))

    def test_darwin_failure_returns_false(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"

        os.environ.pop("APM_NO_REFLINK", None)

        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(rl, "_clone_macos", return_value=False),
        ):
            result = rl.clone_file(src, dst)

        assert result is False

    def test_linux_success_marks_device_supported(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"

        os.environ.pop("APM_NO_REFLINK", None)

        with (
            patch.object(sys, "platform", "linux"),
            patch.object(rl, "_clone_linux", return_value=True),
            patch.object(rl, "_mark_device_supported") as mock_supported,
        ):
            result = rl.clone_file(src, dst)

        assert result is True
        mock_supported.assert_called_once_with(str(dst))

    def test_linux_failure_returns_false(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"

        os.environ.pop("APM_NO_REFLINK", None)

        with (
            patch.object(sys, "platform", "linux"),
            patch.object(rl, "_clone_linux", return_value=False),
        ):
            result = rl.clone_file(src, dst)

        assert result is False

    def test_unsupported_platform_returns_false(self, tmp_path):
        import apm_cli.utils.reflink as rl

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.txt"

        os.environ.pop("APM_NO_REFLINK", None)

        with patch.object(sys, "platform", "win32"):
            result = rl.clone_file(src, dst)

        assert result is False

    def test_path_objects_accepted(self, tmp_path):
        import apm_cli.utils.reflink as rl

        os.environ.pop("APM_NO_REFLINK", None)

        with patch.object(sys, "platform", "win32"):
            # Path objects should be accepted (os.fspath applied)
            result = rl.clone_file(Path("/src"), Path("/dst"))
        assert result is False
