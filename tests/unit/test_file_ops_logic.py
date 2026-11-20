"""Unit tests for apm_cli.utils.file_ops -- retry-aware file operations (pure logic cases)."""

import errno
import os
from unittest.mock import patch

import pytest

from apm_cli.utils.file_ops import (
    _is_transient_lock_error,
    _retry_on_lock,
)

pytestmark = pytest.mark.unit

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

    def test_debug_output_omits_sensitive_details(self, capsys):
        exc = OSError(errno.EBUSY, "SENSITIVE_ERROR_SENTINEL")
        attempt = 0

        def fail_once():
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise exc
            return "ok"

        with patch("apm_cli.utils.file_ops.time.sleep"), patch.dict(os.environ, {"APM_DEBUG": "1"}):
            _retry_on_lock(
                fail_once,
                "SENSITIVE_PATH_SENTINEL",
                max_retries=3,
                initial_delay=0.1,
            )

        captured = capsys.readouterr()
        assert "[DEBUG] Transient file lock (attempt 1/3); retrying in 0.10s" in captured.err
        assert "SENSITIVE_PATH_SENTINEL" not in captured.err
        assert "SENSITIVE_ERROR_SENTINEL" not in captured.err


# ---------------------------------------------------------------------------
# robust_rmtree
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# robust_copytree
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# robust_copy2
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# robust_replace
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _on_readonly_retry callback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Integration: _rmtree in github_downloader uses robust_rmtree
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reflink integration in robust_copy2 / robust_copytree
# ---------------------------------------------------------------------------
