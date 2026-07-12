"""Unit tests for apm_cli.install.summary."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.summary import classify_post_install_result, render_post_install_summary
from apm_cli.models.results import InstallDisposition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> MagicMock:
    logger = MagicMock()
    logger.stale_cleaned_total = 0
    return logger


def _make_diag(
    *,
    has_diagnostics: bool = False,
    error_count: int = 0,
    has_critical_security: bool = False,
) -> MagicMock:
    diag = MagicMock()
    diag.has_diagnostics = has_diagnostics
    diag.error_count = error_count
    diag.has_critical_security = has_critical_security
    return diag


# ---------------------------------------------------------------------------
# render_post_install_summary
# ---------------------------------------------------------------------------


class TestRenderPostInstallSummary:
    """Tests for render_post_install_summary."""

    def test_calls_install_summary_with_counts(self) -> None:
        logger = _make_logger()
        with patch("apm_cli.install.summary._rich_blank_line"):
            render_post_install_summary(
                logger=logger,
                apm_count=3,
                mcp_count=1,
                apm_diagnostics=None,
                force=False,
            )
        logger.install_summary.assert_called_once_with(
            apm_count=3,
            mcp_count=1,
            lsp_count=0,
            errors=0,
            stale_cleaned=0,
            elapsed_seconds=None,
            disposition=InstallDisposition.SUCCESS,
        )

    def test_renders_diagnostics_when_present(self) -> None:
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=True)
        with patch("apm_cli.install.summary._rich_blank_line") as mock_blank:
            render_post_install_summary(
                logger=logger,
                apm_count=1,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )
        diag.render_summary.assert_called_once()
        mock_blank.assert_not_called()

    def test_rich_blank_line_when_no_diagnostics(self) -> None:
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=False)
        with patch("apm_cli.install.summary._rich_blank_line") as mock_blank:
            render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )
        mock_blank.assert_called_once()
        diag.render_summary.assert_not_called()

    def test_rich_blank_line_when_apm_diagnostics_is_none(self) -> None:
        logger = _make_logger()
        with patch("apm_cli.install.summary._rich_blank_line") as mock_blank:
            render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=None,
                force=False,
            )
        mock_blank.assert_called_once()

    def test_error_count_forwarded_to_install_summary(self) -> None:
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=True, error_count=2)
        with patch("apm_cli.install.summary._rich_blank_line"):
            result = render_post_install_summary(
                logger=logger,
                apm_count=1,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )
        assert result.exit_code == 1
        call_kwargs = logger.install_summary.call_args[1]
        assert call_kwargs["errors"] == 2

    def test_hard_fail_on_reported_errors_without_critical_security(self) -> None:
        """Bug 2 (#1496): ``Installation failed with N error(s)`` must exit 1.

        Mirrors npm/pip/cargo: any per-dep install failure -> non-zero exit
        so CI scripts can detect failure without parsing stderr. The
        ``--force`` flag covers critical-security overrides only; it does
        NOT suppress the hard-fail on reported errors.
        """
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=True, error_count=1, has_critical_security=False)
        with patch("apm_cli.install.summary._rich_blank_line"):
            result = render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )
        assert result.exit_code == 1

    def test_force_does_not_suppress_reported_errors(self) -> None:
        """``--force`` overrides only the critical-security hard-fail; a
        non-zero ``error_count`` must still exit 1 so scripted installers
        cannot mask a "failed to download dep" by passing ``--force``."""
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=True, error_count=1, has_critical_security=False)
        with patch("apm_cli.install.summary._rich_blank_line"):
            result = render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=diag,
                force=True,
            )
        assert result.exit_code == 1

    def test_elapsed_seconds_forwarded(self) -> None:
        logger = _make_logger()
        with patch("apm_cli.install.summary._rich_blank_line"):
            render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=None,
                force=False,
                elapsed_seconds=3.7,
            )
        call_kwargs = logger.install_summary.call_args[1]
        assert call_kwargs["elapsed_seconds"] == pytest.approx(3.7)

    def test_hard_fail_on_critical_security_without_force(self) -> None:
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=True, has_critical_security=True)
        with patch("apm_cli.install.summary._rich_blank_line"):
            result = render_post_install_summary(
                logger=logger,
                apm_count=1,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )
        assert result.exit_code == 1

    def test_no_hard_fail_when_force_is_true(self) -> None:
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=True, has_critical_security=True)
        with patch("apm_cli.install.summary._rich_blank_line"):
            # Should NOT raise SystemExit
            render_post_install_summary(
                logger=logger,
                apm_count=1,
                mcp_count=0,
                apm_diagnostics=diag,
                force=True,
            )

    def test_no_hard_fail_when_no_critical_security(self) -> None:
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=True, has_critical_security=False)
        with patch("apm_cli.install.summary._rich_blank_line"):
            render_post_install_summary(
                logger=logger,
                apm_count=1,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )

    def test_stale_cleaned_total_forwarded(self) -> None:
        logger = _make_logger()
        logger.stale_cleaned_total = 5
        with patch("apm_cli.install.summary._rich_blank_line"):
            render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=None,
                force=False,
            )
        call_kwargs = logger.install_summary.call_args[1]
        assert call_kwargs["stale_cleaned"] == 5

    def test_invalid_error_count_defaults_to_zero(self) -> None:
        logger = _make_logger()
        diag = _make_diag(has_diagnostics=True)
        diag.error_count = "not-an-int"
        with patch("apm_cli.install.summary._rich_blank_line"):
            render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )
        call_kwargs = logger.install_summary.call_args[1]
        assert call_kwargs["errors"] == 0


def test_classification_does_not_render_before_transaction_completion() -> None:
    """Disposition classification is side-effect free so commit can run first."""
    logger = _make_logger()
    diagnostics = _make_diag(has_diagnostics=True, error_count=1)

    result = classify_post_install_result(
        apm_count=1,
        apm_diagnostics=diagnostics,
        force=False,
    )

    assert result.disposition is InstallDisposition.FAILED
    logger.install_summary.assert_not_called()
    diagnostics.render_summary.assert_not_called()


def test_renderer_receives_final_committed_disposition() -> None:
    """The final summary receives the result after transaction promotion."""
    logger = _make_logger()
    result = classify_post_install_result(
        apm_count=1,
        apm_diagnostics=None,
        force=False,
    )
    result.disposition = InstallDisposition.PARTIAL_SUCCESS
    result.committed = True

    with patch("apm_cli.install.summary._rich_blank_line"):
        rendered = render_post_install_summary(
            logger=logger,
            apm_count=1,
            mcp_count=0,
            apm_diagnostics=None,
            force=False,
            result=result,
        )

    assert rendered is result
    assert logger.install_summary.call_args.kwargs["disposition"] is (
        InstallDisposition.PARTIAL_SUCCESS
    )
