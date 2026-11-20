"""Unit tests for apm_cli.utils.file_ops -- retry-aware file operations."""

import errno
import os
import shutil
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.utils.file_ops import (
    _on_readonly_retry,
    robust_copy2,
    robust_copytree,
    robust_replace,
    robust_rmtree,
)

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# _is_transient_lock_error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _retry_on_lock
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# robust_rmtree
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows does not honor POSIX execute bits; st_mode & 0o111 is not preserved",
)
def test_robust_copy2_preserves_executable_bits_on_reflink_fast_path(tmp_path):
    src = tmp_path / "source.sh"
    dst = tmp_path / "dest.sh"
    src.write_text("#!/bin/sh\n")
    src.chmod(0o755)

    def fake_clone(src_path: str, dst_path: str) -> bool:
        Path(dst_path).write_bytes(Path(src_path).read_bytes())
        return True

    with patch("apm_cli.utils.reflink.clone_file", side_effect=fake_clone):
        robust_copy2(src, dst)

    assert dst.read_text() == "#!/bin/sh\n"
    assert os.stat(dst).st_mode & 0o111 == 0o111


class TestRobustRmtree:
    """Test the retry-aware rmtree wrapper."""

    def test_removes_normal_directory(self, tmp_path):
        d = tmp_path / "to_remove"
        d.mkdir()
        (d / "file.txt").write_text("hello")
        robust_rmtree(d)
        assert not d.exists()

    def test_handles_readonly_files(self, tmp_path):
        d = tmp_path / "readonly_dir"
        d.mkdir()
        f = d / "readonly.txt"
        f.write_text("locked")
        os.chmod(str(f), stat.S_IREAD)
        robust_rmtree(d)
        assert not d.exists()

    def test_ignore_errors_suppresses_final_failure(self, tmp_path):
        """When ignore_errors=True, don't raise even after all retries."""
        with patch("shutil.rmtree", side_effect=PermissionError("denied")):
            # Should not raise
            robust_rmtree(tmp_path / "nonexistent", ignore_errors=True)

    def test_raises_without_ignore_errors(self, tmp_path):
        exc = OSError(errno.ENOENT, "not found")
        with patch("shutil.rmtree", side_effect=exc), pytest.raises(OSError):
            robust_rmtree(tmp_path / "nonexistent-dir-for-test")

    def test_retries_on_transient_error(self, tmp_path):
        d = tmp_path / "locked_dir"
        d.mkdir()
        (d / "file.txt").write_text("content")

        rmtree_calls = 0
        original_rmtree = shutil.rmtree

        def flaky_rmtree(*args, **kwargs):
            nonlocal rmtree_calls
            rmtree_calls += 1
            if rmtree_calls == 1:
                exc = OSError(errno.EBUSY, "busy")
                raise exc
            return original_rmtree(*args, **kwargs)

        with (
            patch("apm_cli.utils.file_ops.shutil.rmtree", side_effect=flaky_rmtree),
            patch("apm_cli.utils.file_ops.time.sleep"),
        ):
            robust_rmtree(d)

        assert rmtree_calls == 2

    def test_nonexistent_directory_onerror_handles_silently(self, tmp_path):
        """rmtree with onerror callback handles missing dir silently."""
        # shutil.rmtree with an onerror callback suppresses ENOENT,
        # so robust_rmtree also does not raise for non-existent paths.
        robust_rmtree(tmp_path / "definitely-does-not-exist-apm-test")


# ---------------------------------------------------------------------------
# robust_copytree
# ---------------------------------------------------------------------------


class TestRobustCopytree:
    """Test the retry-aware copytree wrapper."""

    def test_copies_normal_directory(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")
        (src / "sub").mkdir()
        (src / "sub" / "nested.txt").write_text("nested")

        dst = tmp_path / "dst"
        result = robust_copytree(src, dst)

        assert result == dst
        assert (dst / "file.txt").read_text() == "hello"
        assert (dst / "sub" / "nested.txt").read_text() == "nested"

    def test_retries_with_partial_cleanup(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")
        dst = tmp_path / "dst"

        copytree_calls = 0
        original_copytree = shutil.copytree

        def flaky_copytree(*args, **kwargs):
            nonlocal copytree_calls
            copytree_calls += 1
            if copytree_calls == 1:
                # Simulate partial copy then failure
                dst.mkdir(exist_ok=True)
                exc = OSError(errno.EBUSY, "busy")
                raise exc
            return original_copytree(*args, **kwargs)

        with (
            patch("apm_cli.utils.file_ops.shutil.copytree", side_effect=flaky_copytree),
            patch("apm_cli.utils.file_ops.time.sleep"),
        ):
            result = robust_copytree(src, dst)

        assert copytree_calls == 2
        assert (result / "file.txt").read_text() == "hello"

    def test_dirs_exist_ok_skips_cleanup(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")
        dst = tmp_path / "dst"
        dst.mkdir()

        cleanup_called = False
        original_copytree = shutil.copytree
        copytree_calls = 0

        def flaky_copytree(*args, **kwargs):
            nonlocal copytree_calls
            copytree_calls += 1
            if copytree_calls == 1:
                exc = OSError(errno.EBUSY, "busy")
                raise exc
            return original_copytree(*args, **kwargs)

        original_rmtree = shutil.rmtree

        def spy_rmtree(*args, **kwargs):
            nonlocal cleanup_called
            cleanup_called = True
            return original_rmtree(*args, **kwargs)

        with (
            patch("apm_cli.utils.file_ops.shutil.copytree", side_effect=flaky_copytree),
            patch("apm_cli.utils.file_ops.shutil.rmtree", side_effect=spy_rmtree),
            patch("apm_cli.utils.file_ops.time.sleep"),
        ):
            robust_copytree(src, dst, dirs_exist_ok=True)

        # Cleanup should NOT have been called because dirs_exist_ok=True
        assert not cleanup_called


# ---------------------------------------------------------------------------
# robust_copy2
# ---------------------------------------------------------------------------


class TestRobustCopy2:
    """Test the retry-aware copy2 wrapper."""

    def test_copies_normal_file(self, tmp_path):
        src = tmp_path / "source.txt"
        src.write_text("content")
        dst = tmp_path / "dest.txt"

        result = robust_copy2(src, dst)
        assert result == dst
        assert dst.read_text() == "content"

    def test_retries_on_transient_error(self, tmp_path):
        src = tmp_path / "source.txt"
        src.write_text("content")
        dst = tmp_path / "dest.txt"

        copy_calls = 0
        original_copy2 = shutil.copy2

        def flaky_copy2(*args, **kwargs):
            nonlocal copy_calls
            copy_calls += 1
            if copy_calls == 1:
                exc = OSError(errno.EBUSY, "busy")
                raise exc
            return original_copy2(*args, **kwargs)

        # Patch the reflink-aware wrapper so the test exercises the
        # retry loop regardless of whether the host filesystem
        # supports clones.
        def flaky_reflink_copy(*args, **kwargs):
            return flaky_copy2(*args, **kwargs)

        with (
            patch("apm_cli.utils.file_ops._reflink_copy_file", side_effect=flaky_reflink_copy),
            patch("apm_cli.utils.file_ops.time.sleep"),
        ):
            result = robust_copy2(src, dst)  # noqa: F841

        assert copy_calls == 2
        assert dst.read_text() == "content"

    def test_non_transient_error_raises_immediately(self, tmp_path):
        src = tmp_path / "nonexistent.txt"
        dst = tmp_path / "dest.txt"

        with pytest.raises(FileNotFoundError):
            robust_copy2(src, dst)


# ---------------------------------------------------------------------------
# robust_replace
# ---------------------------------------------------------------------------


class TestRobustReplace:
    """Test the retry-aware atomic rename wrapper (#3386)."""

    def test_renames_directory(self, tmp_path):
        src = tmp_path / "staged"
        src.mkdir()
        (src / "f.txt").write_text("content")
        dst = tmp_path / "live"

        assert robust_replace(src, dst) == dst
        assert (dst / "f.txt").read_text() == "content"
        assert not src.exists()

    @patch("sys.platform", "win32")
    @pytest.mark.parametrize("winerror", [5, 32])
    def test_retries_windows_lock_then_renames(self, tmp_path, winerror):
        src = tmp_path / "staged"
        src.mkdir()
        dst = tmp_path / "live"
        original_replace = Path.replace
        calls = 0

        def flaky_replace(self, target):
            nonlocal calls
            calls += 1
            if calls == 1:
                exc = OSError("held by scanner")
                exc.winerror = winerror
                raise exc
            return original_replace(self, target)

        with (
            patch.object(Path, "replace", flaky_replace),
            patch("apm_cli.utils.file_ops.time.sleep") as sleep,
        ):
            robust_replace(src, dst)

        assert calls == 2
        sleep.assert_called_once()
        assert dst.is_dir()

    def test_non_transient_error_raises_immediately(self, tmp_path):
        with (
            patch("apm_cli.utils.file_ops.time.sleep") as sleep,
            pytest.raises(FileNotFoundError),
        ):
            robust_replace(tmp_path / "missing", tmp_path / "dest")
        sleep.assert_not_called()


# ---------------------------------------------------------------------------
# _on_readonly_retry callback
# ---------------------------------------------------------------------------


class TestOnReadonlyRetry:
    """Test the onerror callback for read-only file removal."""

    def test_makes_writable_and_retries(self, tmp_path):
        f = tmp_path / "readonly.txt"
        f.write_text("locked")
        os.chmod(str(f), stat.S_IREAD)

        # Simulate what shutil.rmtree does: call unlink via callback
        _on_readonly_retry(os.unlink, str(f), None)
        assert not f.exists()

    def test_suppresses_errors(self, tmp_path):
        """If chmod+retry fails, the callback should not raise."""
        # Pass a path that doesn't exist -- should silently fail
        _on_readonly_retry(os.unlink, str(tmp_path / "nonexistent"), None)


# ---------------------------------------------------------------------------
# Integration: _rmtree in github_downloader uses robust_rmtree
# ---------------------------------------------------------------------------


class TestRmtreeIntegration:
    """Verify that github_downloader._rmtree delegates to robust_rmtree."""

    def test_rmtree_delegates_to_robust_rmtree(self, tmp_path):
        from apm_cli.deps.github_downloader import _rmtree

        d = tmp_path / "test_dir"
        d.mkdir()
        (d / "file.txt").write_text("hello")

        _rmtree(d)
        assert not d.exists()

    def test_rmtree_handles_readonly(self, tmp_path):
        from apm_cli.deps.github_downloader import _rmtree

        d = tmp_path / "ro_dir"
        d.mkdir()
        f = d / "readonly.txt"
        f.write_text("locked")
        os.chmod(str(f), stat.S_IREAD)

        _rmtree(d)
        assert not d.exists()

    def test_rmtree_silent_on_failure(self, tmp_path):
        """_rmtree should not raise (ignore_errors=True)."""
        from apm_cli.deps.github_downloader import _rmtree

        with patch("apm_cli.utils.file_ops.shutil.rmtree", side_effect=PermissionError("denied")):
            # Should not raise
            _rmtree(str(tmp_path / "nonexistent-apm-test-dir"))


# ---------------------------------------------------------------------------
# Reflink integration in robust_copy2 / robust_copytree
# ---------------------------------------------------------------------------


class TestReflinkIntegration:
    """robust_copy2 / robust_copytree must transparently use reflinks.

    The contract: when the underlying filesystem supports clones,
    callers see no behavioural change beyond reduced wall time.
    When clones are unsupported the copy still completes via
    shutil.copy2.
    """

    def test_robust_copy2_attempts_clone_first(self, tmp_path):
        """robust_copy2 routes through the reflink fast-path."""
        from apm_cli.utils.file_ops import _reflink_copy_file, robust_copy2

        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"hello")
        with patch(
            "apm_cli.utils.file_ops._reflink_copy_file",
            wraps=_reflink_copy_file,
        ) as wrapped:
            robust_copy2(src, dst)
            wrapped.assert_called_once()
        assert dst.read_bytes() == b"hello"

    def test_robust_copy2_falls_back_when_clone_fails(self, tmp_path):
        """When clone_file returns False, shutil.copy2 still completes the copy."""
        from apm_cli.utils.file_ops import robust_copy2

        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"payload")
        with patch("apm_cli.utils.reflink.clone_file", return_value=False):
            robust_copy2(src, dst)
        assert dst.read_bytes() == b"payload"

    def test_robust_copytree_uses_reflink_per_file(self, tmp_path):
        """robust_copytree's copy_function is the reflink-aware wrapper."""
        from apm_cli.utils.file_ops import robust_copytree

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"alpha")
        (src / "b.txt").write_bytes(b"beta")
        dst = tmp_path / "dst"
        with patch(
            "apm_cli.utils.file_ops._reflink_copy_file",
            wraps=__import__(
                "apm_cli.utils.file_ops", fromlist=["_reflink_copy_file"]
            )._reflink_copy_file,
        ) as wrapped:
            robust_copytree(src, dst)
            assert wrapped.call_count == 2
        assert (dst / "a.txt").read_bytes() == b"alpha"
        assert (dst / "b.txt").read_bytes() == b"beta"

    def test_robust_copytree_completes_even_when_clones_unsupported(self, tmp_path):
        """Clone failures must not break the copy."""
        from apm_cli.utils.file_ops import robust_copytree

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        (src / "sub").mkdir()
        (src / "sub" / "b.txt").write_bytes(b"y")
        dst = tmp_path / "dst"
        with patch("apm_cli.utils.reflink.clone_file", return_value=False):
            robust_copytree(src, dst)
        assert (dst / "a.txt").read_bytes() == b"x"
        assert (dst / "sub" / "b.txt").read_bytes() == b"y"

    def test_apm_no_reflink_env_skips_clones(self, tmp_path, monkeypatch):
        """APM_NO_REFLINK forces the fallback path end-to-end."""
        from apm_cli.utils import reflink as reflink_mod
        from apm_cli.utils.file_ops import robust_copytree

        reflink_mod._reset_capability_cache()
        monkeypatch.setenv("APM_NO_REFLINK", "1")
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        with (
            patch.object(reflink_mod, "_clone_macos") as mac,
            patch.object(reflink_mod, "_clone_linux") as lin,
        ):
            robust_copytree(src, tmp_path / "dst")
            mac.assert_not_called()
            lin.assert_not_called()
        assert (tmp_path / "dst" / "a.txt").read_bytes() == b"x"
