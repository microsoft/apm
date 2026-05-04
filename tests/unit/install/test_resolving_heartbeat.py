"""Unit tests for ``InstallLogger.resolving_heartbeat`` (F1, #1116).

The heartbeat must be a static log line (not a Rich transient) so it
survives ``2>&1 | tee`` pipelines and CI logs -- the duck critique's
explicit must-survive surface.
"""

from unittest.mock import patch

from apm_cli.core.command_logger import InstallLogger


@patch("apm_cli.core.command_logger._rich_info")
def test_resolving_heartbeat_uses_running_symbol(mock_info):
    logger = InstallLogger()
    logger.resolving_heartbeat("owner/repo@v1")
    args, kwargs = mock_info.call_args
    assert "Resolving owner/repo@v1..." in args[0]
    # Must use the static "running" symbol, NOT a transient progress bar.
    assert kwargs.get("symbol") == "running"


@patch("apm_cli.core.command_logger._rich_info")
def test_resolving_heartbeat_emits_one_line_per_call(mock_info):
    logger = InstallLogger()
    logger.resolving_heartbeat("a/x")
    logger.resolving_heartbeat("b/y")
    logger.resolving_heartbeat("c/z")
    assert mock_info.call_count == 3
    rendered = [c.args[0] for c in mock_info.call_args_list]
    assert all(msg.startswith("Resolving ") and msg.endswith("...") for msg in rendered)
