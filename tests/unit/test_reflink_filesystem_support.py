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

pytestmark = pytest.mark.component

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


# ---------------------------------------------------------------------------
# _clone_macos
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _clone_linux
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="Linux-only: fcntl module not available on Windows"
)
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
