"""Unit tests for apm_cli.core.null_logger.NullCommandLogger.

Covers:
- verbose attribute is False
- start() calls _rich_info
- progress() calls _rich_info
- mcp_lookup_heartbeat(0) does nothing (early return)
- mcp_lookup_heartbeat(1) calls _rich_info with "server" (singular)
- mcp_lookup_heartbeat(3) calls _rich_info with "servers" (plural)
- success() calls _rich_success
- warning() calls _rich_warning
- error() calls _rich_error
- verbose_detail() is a no-op (no calls, no exception)
- tree_item() calls _rich_echo with color="green"
- package_inline_warning() is a no-op (no calls, no exception)
"""

from __future__ import annotations

from unittest.mock import patch

from apm_cli.core.null_logger import NullCommandLogger

# Module path where the console helpers are looked up
_MODULE = "apm_cli.core.null_logger"


class TestNullCommandLoggerAttributes:
    """Tests for NullCommandLogger class-level attributes."""

    def test_verbose_is_false(self) -> None:
        logger = NullCommandLogger()
        assert logger.verbose is False

    def test_verbose_is_false_class_attribute(self) -> None:
        assert NullCommandLogger.verbose is False


class TestNullCommandLoggerStart:
    """Tests for NullCommandLogger.start()."""

    def test_start_calls_rich_info(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.start("Starting operation")
        mock_info.assert_called_once_with("Starting operation", symbol="running")

    def test_start_custom_symbol(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.start("msg", symbol="spin")
        mock_info.assert_called_once_with("msg", symbol="spin")


class TestNullCommandLoggerProgress:
    """Tests for NullCommandLogger.progress()."""

    def test_progress_calls_rich_info(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.progress("In progress...")
        mock_info.assert_called_once_with("In progress...", symbol="info")

    def test_progress_custom_symbol(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.progress("msg", symbol="dots")
        mock_info.assert_called_once_with("msg", symbol="dots")


class TestNullCommandLoggerMcpLookupHeartbeat:
    """Tests for NullCommandLogger.mcp_lookup_heartbeat()."""

    def test_count_zero_does_nothing(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.mcp_lookup_heartbeat(0)
        mock_info.assert_not_called()

    def test_negative_count_does_nothing(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.mcp_lookup_heartbeat(-5)
        mock_info.assert_not_called()

    def test_count_one_uses_singular_noun(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.mcp_lookup_heartbeat(1)
        mock_info.assert_called_once()
        message = mock_info.call_args[0][0]
        assert "server" in message
        assert "servers" not in message

    def test_count_two_uses_plural_noun(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.mcp_lookup_heartbeat(2)
        mock_info.assert_called_once()
        message = mock_info.call_args[0][0]
        assert "servers" in message

    def test_count_three_uses_plural_noun(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.mcp_lookup_heartbeat(3)
        mock_info.assert_called_once()
        message = mock_info.call_args[0][0]
        assert "servers" in message

    def test_count_message_includes_count(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.mcp_lookup_heartbeat(5)
        message = mock_info.call_args[0][0]
        assert "5" in message

    def test_heartbeat_uses_running_symbol(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_info") as mock_info:
            logger.mcp_lookup_heartbeat(1)
        assert mock_info.call_args[1].get("symbol") == "running"


class TestNullCommandLoggerSuccess:
    """Tests for NullCommandLogger.success()."""

    def test_success_calls_rich_success(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_success") as mock_success:
            logger.success("All done!")
        mock_success.assert_called_once_with("All done!", symbol="sparkles")

    def test_success_custom_symbol(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_success") as mock_success:
            logger.success("Done", symbol="check")
        mock_success.assert_called_once_with("Done", symbol="check")


class TestNullCommandLoggerWarning:
    """Tests for NullCommandLogger.warning()."""

    def test_warning_calls_rich_warning(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_warning") as mock_warning:
            logger.warning("Watch out!")
        mock_warning.assert_called_once_with("Watch out!", symbol="warning")

    def test_warning_custom_symbol(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_warning") as mock_warning:
            logger.warning("msg", symbol="alert")
        mock_warning.assert_called_once_with("msg", symbol="alert")


class TestNullCommandLoggerError:
    """Tests for NullCommandLogger.error()."""

    def test_error_calls_rich_error(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_error") as mock_error:
            logger.error("Something failed!")
        mock_error.assert_called_once_with("Something failed!", symbol="error")

    def test_error_custom_symbol(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_error") as mock_error:
            logger.error("msg", symbol="x")
        mock_error.assert_called_once_with("msg", symbol="x")


class TestNullCommandLoggerVerboseDetail:
    """Tests for NullCommandLogger.verbose_detail()."""

    def test_verbose_detail_is_noop(self) -> None:
        """verbose_detail() must not call any console helper."""
        logger = NullCommandLogger()
        with (
            patch(f"{_MODULE}._rich_info") as mock_info,
            patch(f"{_MODULE}._rich_success") as mock_success,
            patch(f"{_MODULE}._rich_warning") as mock_warning,
            patch(f"{_MODULE}._rich_error") as mock_error,
            patch(f"{_MODULE}._rich_echo") as mock_echo,
        ):
            logger.verbose_detail("this should be discarded")

        mock_info.assert_not_called()
        mock_success.assert_not_called()
        mock_warning.assert_not_called()
        mock_error.assert_not_called()
        mock_echo.assert_not_called()

    def test_verbose_detail_does_not_raise(self) -> None:
        logger = NullCommandLogger()
        # Should complete without exception
        logger.verbose_detail("some detail")


class TestNullCommandLoggerTreeItem:
    """Tests for NullCommandLogger.tree_item()."""

    def test_tree_item_calls_rich_echo_with_green(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_echo") as mock_echo:
            logger.tree_item("├── package-a")
        mock_echo.assert_called_once_with("├── package-a", color="green")

    def test_tree_item_passes_message_correctly(self) -> None:
        logger = NullCommandLogger()
        with patch(f"{_MODULE}._rich_echo") as mock_echo:
            logger.tree_item("└── last-item")
        assert mock_echo.call_args[0][0] == "└── last-item"
        assert mock_echo.call_args[1]["color"] == "green"


class TestNullCommandLoggerPackageInlineWarning:
    """Tests for NullCommandLogger.package_inline_warning()."""

    def test_package_inline_warning_is_noop(self) -> None:
        """package_inline_warning() must not call any console helper."""
        logger = NullCommandLogger()
        with (
            patch(f"{_MODULE}._rich_info") as mock_info,
            patch(f"{_MODULE}._rich_success") as mock_success,
            patch(f"{_MODULE}._rich_warning") as mock_warning,
            patch(f"{_MODULE}._rich_error") as mock_error,
            patch(f"{_MODULE}._rich_echo") as mock_echo,
        ):
            logger.package_inline_warning("suppressed warning")

        mock_info.assert_not_called()
        mock_success.assert_not_called()
        mock_warning.assert_not_called()
        mock_error.assert_not_called()
        mock_echo.assert_not_called()

    def test_package_inline_warning_does_not_raise(self) -> None:
        logger = NullCommandLogger()
        logger.package_inline_warning("irrelevant")
