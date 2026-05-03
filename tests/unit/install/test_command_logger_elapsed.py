"""Unit tests for ``InstallLogger.install_summary`` elapsed-time suffix
(F5, microsoft/apm#1116).

The summary must:
- Append `` in {x:.1f}s`` before the terminating period when an
  ``elapsed_seconds`` is provided.
- Stay byte-identical to the legacy output when ``elapsed_seconds=None``.
- Place the cleanup parenthetical before the timing suffix so the order
  reads "Installed N APM dependencies (M stale files cleaned) in Xs."
"""

from unittest.mock import patch

from apm_cli.core.command_logger import InstallLogger


@patch("apm_cli.core.command_logger._rich_success")
def test_install_summary_appends_elapsed(mock_success):
    logger = InstallLogger()
    logger.install_summary(apm_count=3, mcp_count=0, elapsed_seconds=2.5)
    msg = mock_success.call_args[0][0]
    assert " in 2.5s." in msg
    assert msg.endswith(" in 2.5s.")


@patch("apm_cli.core.command_logger._rich_success")
def test_install_summary_no_elapsed_keeps_legacy(mock_success):
    logger = InstallLogger()
    logger.install_summary(apm_count=3, mcp_count=0, elapsed_seconds=None)
    msg = mock_success.call_args[0][0]
    # Backward-compat: no `` in Xs`` suffix when elapsed not supplied.
    assert " in " not in msg
    assert msg.endswith(".")


@patch("apm_cli.core.command_logger._rich_success")
def test_install_summary_cleanup_precedes_timing(mock_success):
    logger = InstallLogger()
    logger.install_summary(apm_count=3, mcp_count=0, stale_cleaned=4, elapsed_seconds=1.2)
    msg = mock_success.call_args[0][0]
    cleanup_idx = msg.index("(4 stale files cleaned)")
    timing_idx = msg.index(" in 1.2s")
    assert cleanup_idx < timing_idx
    assert msg.endswith(".")


@patch("apm_cli.core.command_logger._rich_warning")
def test_install_interrupted_emits_minimal_line(mock_warning):
    logger = InstallLogger()
    logger.install_interrupted(elapsed_seconds=0.7)
    msg = mock_warning.call_args[0][0]
    assert "Install interrupted" in msg
    assert "0.7s" in msg


@patch("apm_cli.core.command_logger._rich_warning")
def test_install_summary_with_errors_includes_elapsed(mock_warning):
    logger = InstallLogger()
    logger.install_summary(apm_count=2, mcp_count=1, errors=1, elapsed_seconds=3.4)
    msg = mock_warning.call_args[0][0]
    assert "in 3.4s" in msg
    assert "with 1 error" in msg
