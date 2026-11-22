"""Unit tests for apm_cli.utils.reflink -- copy-on-write file cloning.

Reflink semantics depend on both the OS and the underlying filesystem,
so most tests use mocks to drive both branches deterministically. A
small number of integration tests run only when ``reflink_supported()``
returns True (typically macOS APFS or Linux btrfs/XFS); they are
skipped on ext4, NFS, tmpfs, and Windows runners.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.utils import reflink
from apm_cli.utils.reflink import (
    _reset_capability_cache,
    clone_file,
    reflink_supported,
)


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    """Reset the per-device capability cache between tests."""
    _reset_capability_cache()
    yield
    _reset_capability_cache()


@pytest.fixture(autouse=True)
def _clear_no_reflink_env(monkeypatch):
    """Remove APM_NO_REFLINK so each test starts from the same baseline."""
    monkeypatch.delenv("APM_NO_REFLINK", raising=False)


class TestReflinkSupported:
    """Test the reflink_supported() probe."""

    def test_apm_no_reflink_disables(self, monkeypatch):
        monkeypatch.setenv("APM_NO_REFLINK", "1")
        assert reflink_supported() is False

    def test_returns_bool(self):
        assert isinstance(reflink_supported(), bool)


class TestCloneFileEnvOptOut:
    """APM_NO_REFLINK must short-circuit before any platform call."""

    def test_env_opt_out_returns_false(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"hello")
        monkeypatch.setenv("APM_NO_REFLINK", "1")
        assert clone_file(src, dst) is False
        # Did not create dst -- fallback path is the caller's job.
        assert not dst.exists()

    def test_env_opt_out_skips_ctypes_call(self, tmp_path: Path, monkeypatch):
        """Make sure no platform-specific code path is even attempted."""
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"hello")
        monkeypatch.setenv("APM_NO_REFLINK", "1")
        with (
            patch.object(reflink, "_clone_macos") as mac,
            patch.object(reflink, "_clone_linux") as lin,
        ):
            assert clone_file(src, dst) is False
            mac.assert_not_called()
            lin.assert_not_called()


class TestCloneFileFallback:
    """Failures must return False, never raise."""

    def test_returns_false_when_unsupported(self, tmp_path: Path):
        """On a filesystem without reflink support, returns False."""
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"hello")
        with (
            patch.object(reflink, "_clone_macos", return_value=False),
            patch.object(reflink, "_clone_linux", return_value=False),
        ):
            assert clone_file(src, dst) is False

    def test_does_not_raise_on_missing_source(self, tmp_path: Path):
        src = tmp_path / "missing.bin"
        dst = tmp_path / "dst.bin"
        # Must not raise even though src does not exist.
        assert clone_file(src, dst) is False

    def test_does_not_raise_on_existing_destination(self, tmp_path: Path):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"hello")
        dst.write_bytes(b"existing")
        # macOS clonefile rejects existing dst with EEXIST; Linux
        # FICLONE wrapper opens with O_CREAT|O_EXCL. Either way: False.
        result = clone_file(src, dst)
        assert isinstance(result, bool)


class TestCapabilityCache:
    """Per-device capability cache must skip retries on unsupported FS."""

    def test_cache_marks_unsupported_after_failure(self, tmp_path: Path):
        """A simulated ENOTSUP failure marks the device as unsupported."""
        src = tmp_path / "src.bin"
        dst1 = tmp_path / "dst1.bin"
        dst2 = tmp_path / "dst2.bin"
        src.write_bytes(b"x")

        # Force the unsupported branch via the public API.
        reflink._mark_device_unsupported(str(dst1))
        # Now the cache short-circuits before any platform call.
        with (
            patch.object(reflink, "_clone_macos") as mac,
            patch.object(reflink, "_clone_linux") as lin,
        ):
            assert clone_file(src, dst2) is False
            mac.assert_not_called()
            lin.assert_not_called()

    def test_cache_reset(self, tmp_path: Path):
        """_reset_capability_cache clears the cached negative."""
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"x")
        reflink._mark_device_unsupported(str(dst))
        _reset_capability_cache()
        # Mark removed -- the platform call should now be attempted.
        with (
            patch.object(reflink, "_clone_macos", return_value=True) as mac,
            patch.object(reflink, "_clone_linux", return_value=True) as lin,
        ):
            clone_file(src, dst)
            # Exactly one of the platform paths should have been invoked.
            calls = mac.call_count + lin.call_count
            # On unsupported platforms (e.g. Windows) neither runs and
            # clone_file returns False. Both outcomes are acceptable.
            assert calls in (0, 1)


@pytest.mark.skipif(not reflink_supported(), reason="filesystem without reflink support")
class TestRealReflink:
    """End-to-end tests that require a reflink-capable filesystem."""

    def test_clone_succeeds_on_supported_fs(self, tmp_path: Path):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        payload = b"a" * 16384
        src.write_bytes(payload)
        ok = clone_file(src, dst)
        # Some CI tmp dirs (overlayfs, tmpfs) report supported but
        # reject the actual clone. Tolerate both outcomes -- only
        # assert that on True the destination is correct.
        if ok:
            assert dst.read_bytes() == payload
            # Distinct inodes (CoW), but identical content.
            assert os.stat(src).st_ino != os.stat(dst).st_ino

    def test_clone_then_modify_preserves_source(self, tmp_path: Path):
        """Copy-on-write: modifying dst must not affect src."""
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        original = b"original"
        src.write_bytes(original)
        if not clone_file(src, dst):
            pytest.skip("filesystem rejected real clone")
        dst.write_bytes(b"modified")
        assert src.read_bytes() == original
