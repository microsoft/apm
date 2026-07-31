"""Regression tests for _execute_install_and_summary disposition pipeline.

Guards three behaviours that were previously missing (dropped main delta):
  (a) A cancelled install propagates CANCELLED to the final result/exit code.
  (b) --dry-run sets DRY_RUN disposition.
  (c) The summary line reflects the post-commit result, not a diagnostics-only
      classification (i.e. _post_install_summary receives result= not None).

All patches go through apm_cli.commands.install.* per RULE B.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

from apm_cli.install.install_cmd_phases import _execute_install_and_summary
from apm_cli.models.results import InstallDisposition, InstallResult

# ---------------------------------------------------------------------------
# Minimal install_ctx stub
# ---------------------------------------------------------------------------


def _make_ctx(*, dry_run: bool = False, install_result=None, transaction=None):
    """Return a minimal install_ctx stub with the fields _execute_install_and_summary reads."""
    logger = MagicMock()
    logger.tree_item = MagicMock()
    ctx = types.SimpleNamespace(
        dry_run=dry_run,
        force=False,
        install_result=install_result,
        transaction=transaction,
        logger=logger,
    )
    return ctx


def _noop_transaction():
    """Mock transaction whose complete() is transparent."""
    txn = MagicMock()
    txn.complete.side_effect = lambda r: r
    return txn


# ---------------------------------------------------------------------------
# (a) Cancelled install propagates CANCELLED disposition
# ---------------------------------------------------------------------------


def test_cancelled_install_propagates_cancelled_disposition(tmp_path) -> None:
    """_execute_install_and_summary must surface CANCELLED from an aborted inner install."""
    cancelled_result = InstallResult(
        installed_count=0,
        disposition=InstallDisposition.CANCELLED,
    )

    def _fake_install(ctx, _outcome):
        # Mirrors apm_packages.py: set ctx.install_result then return counts.
        ctx.install_result = cancelled_result
        return 0, 0, 0, None

    summary_result = InstallResult(installed_count=0, disposition=InstallDisposition.CANCELLED)

    ctx = _make_ctx(transaction=_noop_transaction())

    with (
        patch("apm_cli.commands.install._install_apm_packages", side_effect=_fake_install),
        patch(
            "apm_cli.commands.install._post_install_summary", return_value=summary_result
        ) as mock_summary,
    ):
        result = _execute_install_and_summary(ctx, None, False, 0.0)

    assert result.disposition is InstallDisposition.CANCELLED
    # Verify result= was passed to summary (not None / diagnostics-only fallback).
    call_kwargs = mock_summary.call_args.kwargs
    assert call_kwargs.get("result") is not None
    assert call_kwargs["result"].disposition is InstallDisposition.CANCELLED


# ---------------------------------------------------------------------------
# (b) --dry-run flag sets DRY_RUN disposition
# ---------------------------------------------------------------------------


def test_dry_run_flag_sets_dry_run_disposition(tmp_path) -> None:
    """install_ctx.dry_run=True must propagate DRY_RUN to the committed result."""
    success_result = InstallResult(installed_count=1)

    def _fake_install(ctx, _outcome):
        return 1, 0, 0, None

    ctx = _make_ctx(dry_run=True, transaction=_noop_transaction())

    captured: dict = {}

    def _capture_summary(**kwargs):
        captured["result"] = kwargs.get("result")
        return kwargs.get("result") or success_result

    with (
        patch("apm_cli.commands.install._install_apm_packages", side_effect=_fake_install),
        patch("apm_cli.commands.install._post_install_summary", side_effect=_capture_summary),
    ):
        _execute_install_and_summary(ctx, None, False, 0.0)

    assert captured["result"] is not None, "result= must be passed to _post_install_summary"
    assert captured["result"].disposition is InstallDisposition.DRY_RUN, (
        "DRY_RUN disposition must be set before transaction.complete and summary"
    )


# ---------------------------------------------------------------------------
# (c) Summary receives post-commit result, not diagnostics-only classification
# ---------------------------------------------------------------------------


def test_summary_receives_committed_result_not_diagnostics_only(tmp_path) -> None:
    """_post_install_summary must be called with result= so it uses the committed disposition."""
    committed_result = InstallResult(installed_count=2, disposition=InstallDisposition.SUCCESS)
    committed_result.committed = True

    txn = MagicMock()
    txn.complete.return_value = committed_result

    def _fake_install(ctx, _outcome):
        return 2, 0, 0, None

    ctx = _make_ctx(transaction=txn)

    with (
        patch("apm_cli.commands.install._install_apm_packages", side_effect=_fake_install),
        patch(
            "apm_cli.commands.install._post_install_summary",
            return_value=committed_result,
        ) as mock_summary,
    ):
        _execute_install_and_summary(ctx, None, False, 0.0)

    call_kwargs = mock_summary.call_args.kwargs
    passed_result = call_kwargs.get("result")
    assert passed_result is not None, (
        "_post_install_summary must receive result= (committed) not fall back to "
        "diagnostics-only classify_post_install_result"
    )
    assert passed_result is committed_result, (
        "The committed result from transaction.complete must be forwarded verbatim"
    )
