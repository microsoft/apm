"""Tests for CLI entry point encoding configuration."""

from unittest.mock import MagicMock, patch

from apm_cli.cli import _configure_encoding


class TestConfigureEncoding:
    """Test _configure_encoding for Windows UTF-8 console setup."""

    @patch("apm_cli.cli.ctypes")
    @patch("apm_cli.cli.os")
    @patch("apm_cli.cli.sys")
    def test_sets_utf8_codepage_on_windows(self, mock_sys, mock_os, mock_ctypes):
        """On win32, should call SetConsoleOutputCP(65001) and SetConsoleCP(65001)."""
        mock_sys.platform = "win32"
        mock_sys.stdout = MagicMock()
        mock_sys.stderr = MagicMock()
        mock_os.environ = {}

        kernel32 = mock_ctypes.windll.kernel32

        _configure_encoding()

        kernel32.SetConsoleOutputCP.assert_called_once_with(65001)
        kernel32.SetConsoleCP.assert_called_once_with(65001)

    @patch("apm_cli.cli.ctypes")
    @patch("apm_cli.cli.os")
    @patch("apm_cli.cli.sys")
    def test_reconfigures_streams_to_utf8(self, mock_sys, mock_os, mock_ctypes):
        """stdout and stderr should be reconfigured to encoding='utf-8'."""
        mock_sys.platform = "win32"
        mock_stdout = MagicMock()
        mock_stderr = MagicMock()
        mock_sys.stdout = mock_stdout
        mock_sys.stderr = mock_stderr
        mock_os.environ = {}

        _configure_encoding()

        mock_stdout.reconfigure.assert_called_once_with(encoding="utf-8")
        mock_stderr.reconfigure.assert_called_once_with(encoding="utf-8")

    @patch("apm_cli.cli.ctypes")
    @patch("apm_cli.cli.sys")
    def test_sets_pythonioencoding(self, mock_sys, mock_ctypes):
        """Should set PYTHONIOENCODING=utf-8 if not already set."""
        import os as real_os

        mock_sys.platform = "win32"
        mock_sys.stdout = MagicMock()
        mock_sys.stderr = MagicMock()
        orig = real_os.environ.pop("PYTHONIOENCODING", None)
        try:
            _configure_encoding()
            assert real_os.environ.get("PYTHONIOENCODING") == "utf-8"
        finally:
            if orig is not None:
                real_os.environ["PYTHONIOENCODING"] = orig
            else:
                real_os.environ.pop("PYTHONIOENCODING", None)

    @patch("apm_cli.cli.sys")
    def test_noop_on_non_windows(self, mock_sys):
        """On non-Windows, should do nothing."""
        mock_sys.platform = "linux"
        mock_stdout = MagicMock()
        mock_sys.stdout = mock_stdout

        _configure_encoding()

        mock_stdout.reconfigure.assert_not_called()

    @patch("apm_cli.cli.ctypes")
    @patch("apm_cli.cli.os")
    @patch("apm_cli.cli.sys")
    def test_handles_no_reconfigure(self, mock_sys, mock_os, mock_ctypes):
        """Should not crash when stream lacks reconfigure (e.g. pytest capture)."""
        mock_sys.platform = "win32"
        mock_sys.stdout = MagicMock(spec=[])  # no reconfigure
        mock_sys.stderr = MagicMock()
        mock_os.environ = {}

        # Should not raise
        _configure_encoding()

    @patch("apm_cli.cli.ctypes")
    @patch("apm_cli.cli.os")
    @patch("apm_cli.cli.sys")
    def test_fallback_on_reconfigure_failure(self, mock_sys, mock_os, mock_ctypes):
        """If strict reconfigure fails, should retry with errors='backslashreplace'."""
        mock_sys.platform = "win32"
        mock_stdout = MagicMock()
        # First call raises, second (with errors=) succeeds
        mock_stdout.reconfigure.side_effect = [Exception("encoding locked"), None]
        mock_sys.stdout = mock_stdout
        mock_sys.stderr = MagicMock()
        mock_os.environ = {}

        _configure_encoding()

        assert mock_stdout.reconfigure.call_count == 2
        mock_stdout.reconfigure.assert_any_call(encoding="utf-8")
        mock_stdout.reconfigure.assert_any_call(encoding="utf-8", errors="backslashreplace")

    @patch("apm_cli.cli.ctypes")
    @patch("apm_cli.cli.os")
    @patch("apm_cli.cli.sys")
    def test_survives_ctypes_failure(self, mock_sys, mock_os, mock_ctypes):
        """If SetConsoleOutputCP raises, should still reconfigure streams."""
        mock_sys.platform = "win32"
        mock_sys.stdout = MagicMock()
        mock_sys.stderr = MagicMock()
        mock_os.environ = {}

        mock_ctypes.windll.kernel32.SetConsoleOutputCP.side_effect = OSError("no console")

        _configure_encoding()

        # Streams should still be reconfigured
        mock_sys.stdout.reconfigure.assert_called_once_with(encoding="utf-8")
