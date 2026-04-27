"""Unit tests for apm_cli.utils.file_ops -- retry-aware file operations."""

import errno
import os
import shutil
import stat
import sys  # noqa: F401
from pathlib import Path  # noqa: F401
from unittest.mock import patch

import pytest

from apm_cli.utils.file_ops import (
    _is_transient_lock_error,
    _on_readonly_retry,
    _retry_on_lock,
    robust_copy2,
    robust_copytree,
    robust_rmtree,
)

# ---------------------------------------------------------------------------
# _is_transient_lock_error
# ---------------------------------------------------------------------------


class TestIsTransientLockError:
    """Test the error classification predicate."""

    def test_ebusy_is_transient(self):
        exc = OSError(errno.EBUSY, "Device busy")
        assert _is_transient_lock_error(exc) is True

    def test_enoent_is_not_transient(self):
        exc = OSError(errno.ENOENT, "No such file or directory")
        assert _is_transient_lock_error(exc) is False

    def test_eacces_is_not_transient_on_unix(self):
        """EACCES on Unix is a real permission problem, not transient."""
        exc = OSError(errno.EACCES, "Permission denied")
        with patch("sys.platform", "linux"):
            assert _is_transient_lock_error(exc) is False

    @patch("sys.platform", "win32")
    def test_winerror_32_is_transient(self):
        """WinError 32 (ERROR_SHARING_VIOLATION) is the canonical AV lock."""
        exc = OSError("sharing violation")
        exc.winerror = 32
        assert _is_transient_lock_error(exc) is True

    @patch("sys.platform", "win32")
    def test_winerror_5_is_transient(self):
        """WinError 5 (ERROR_ACCESS_DENIED) from endpoint protection."""
        exc = OSError("access denied")
        exc.winerror = 5
        assert _is_transient_lock_error(exc) is True

    @patch("sys.platform", "win32")
    def test_winerror_2_is_not_transient(self):
        """WinError 2 (ERROR_FILE_NOT_FOUND) is not transient."""
        exc = OSError("file not found")
        exc.winerror = 2
        assert _is_transient_lock_error(exc) is False

    def test_generic_oserror_not_transient(self):
        """OSError with errno 0 (generic/unknown) is not classified as transient."""
        exc = OSError(0, "generic")
        assert _is_transient_lock_error(exc) is False


# ---------------------------------------------------------------------------
# _retry_on_lock
# ---------------------------------------------------------------------------


class TestRetryOnLock:
    """Test the generic retry loop."""

    def test_success_on_first_try(self):
        result = _retry_on_lock(lambda: 42, "test op")
        assert result == 42

    def test_retries_on_transient_error(self):
        call_count = 0
        exc = OSError(errno.EBUSY, "busy")

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise exc
            return "ok"

        with patch("apm_cli.utils.file_ops.time.sleep"):
            result = _retry_on_lock(flaky, "test op", max_retries=5)

        assert result == "ok"
        assert call_count == 3

    def test_raises_after_max_retries(self):
        exc = OSError(errno.EBUSY, "busy")

        def always_fail():
            raise exc

        with patch("apm_cli.utils.file_ops.time.sleep"), pytest.raises(OSError, match="busy"):
            _retry_on_lock(always_fail, "test op", max_retries=2)

    def test_non_transient_error_raises_immediately(self):
        call_count = 0
        exc = OSError(errno.ENOENT, "not found")

        def fail():
            nonlocal call_count
            call_count += 1
            raise exc

        with pytest.raises(OSError, match="not found"):
            _retry_on_lock(fail, "test op", max_retries=5)

        assert call_count == 1

    def test_exponential_backoff(self):
        sleep_calls = []
        exc = OSError(errno.EBUSY, "busy")
        attempt = 0

        def fail_three_times():
            nonlocal attempt
            attempt += 1
            if attempt <= 3:
                raise exc
            return "ok"

        with patch(
            "apm_cli.utils.file_ops.time.sleep", side_effect=lambda d: sleep_calls.append(d)
        ):
            _retry_on_lock(
                fail_three_times,
                "test",
                initial_delay=0.1,
                backoff_factor=2.0,
                max_delay=10.0,
                max_retries=5,
            )

        assert len(sleep_calls) == 3
        assert sleep_calls[0] == pytest.approx(0.1)
        assert sleep_calls[1] == pytest.approx(0.2)
        assert sleep_calls[2] == pytest.approx(0.4)

    def test_delay_capped_at_max_delay(self):
        sleep_calls = []
        exc = OSError(errno.EBUSY, "busy")
        attempt = 0

        def fail_many():
            nonlocal attempt
            attempt += 1
            if attempt <= 4:
                raise exc
            return "ok"

        with patch(
            "apm_cli.utils.file_ops.time.sleep", side_effect=lambda d: sleep_calls.append(d)
        ):
            _retry_on_lock(
                fail_many,
                "test",
                initial_delay=1.0,
                backoff_factor=2.0,
                max_delay=2.0,
                max_retries=5,
            )

        # 1.0, 2.0, 2.0, 2.0 -- capped at 2.0
        assert sleep_calls[-1] == pytest.approx(2.0)
        assert all(d <= 2.0 for d in sleep_calls)

    def test_before_retry_called(self):
        cleanup_calls = 0
        exc = OSError(errno.EBUSY, "busy")
        attempt = 0

        def fail_once():
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise exc
            return "ok"

        def cleanup():
            nonlocal cleanup_calls
            cleanup_calls += 1

        with patch("apm_cli.utils.file_ops.time.sleep"):
            _retry_on_lock(fail_once, "test", before_retry=cleanup, max_retries=3)

        assert cleanup_calls == 1

    def test_before_retry_exception_suppressed(self):
        exc = OSError(errno.EBUSY, "busy")
        attempt = 0

        def fail_once():
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise exc
            return "ok"

        def bad_cleanup():
            raise OSError("cleanup failed")

        with patch("apm_cli.utils.file_ops.time.sleep"):
            result = _retry_on_lock(
                fail_once,
                "test",
                before_retry=bad_cleanup,
                max_retries=3,
            )
        assert result == "ok"

    def test_debug_output_when_apm_debug_set(self, capsys):
        exc = OSError(errno.EBUSY, "busy")
        attempt = 0

        def fail_once():
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise exc
            return "ok"

        with patch("apm_cli.utils.file_ops.time.sleep"), patch.dict(os.environ, {"APM_DEBUG": "1"}):
            _retry_on_lock(fail_once, "test op", max_retries=3)

        captured = capsys.readouterr()
        assert "transient lock" in captured.err
        assert "test op" in captured.err


# ---------------------------------------------------------------------------
# robust_rmtree
# ---------------------------------------------------------------------------


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

        with (
            patch("apm_cli.utils.file_ops.shutil.copy2", side_effect=flaky_copy2),
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
