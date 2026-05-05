"""Regression-trap tests for the ``apm pack`` package-count rendering.

Bug fix at ``src/apm_cli/commands/pack.py``: the success and dry-run
messages reported ``len(report.resolved)`` only, undercounting builds
that emit upstream-sourced packages. This test pins the fix by asserting
the rendered message reflects the **total** of direct + upstream entries
for every output path the builder can take.

If the count regresses (e.g. reverts to ``len(resolved)``), these tests
fail before the build finishes -- giving the regression a hard failure
rather than a silent UX drift.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from apm_cli.commands.pack import _render_marketplace_result
from apm_cli.marketplace.builder import BuildReport


def _make_report(
    *,
    resolved_count: int,
    upstream_count: int,
    dry_run: bool = False,
) -> BuildReport:
    """Build a minimal ``BuildReport`` with the requested counts.

    Only ``resolved`` and ``upstream_resolved`` lengths are exercised
    by the renderer; payloads can be opaque sentinels.
    """
    return BuildReport(
        resolved=tuple(object() for _ in range(resolved_count)),
        errors=(),
        warnings=(),
        unchanged_count=0,
        added_count=resolved_count + upstream_count,
        updated_count=0,
        removed_count=0,
        output_path=Path("/tmp/marketplace.json"),
        dry_run=dry_run,
        upstream_resolved=tuple(object() for _ in range(upstream_count)),
    )


def test_success_message_counts_direct_plus_upstream() -> None:
    """Success path: ``Built marketplace.json (N direct + M upstream)`` when
    upstream packages are present."""
    logger = MagicMock()
    report = _make_report(resolved_count=2, upstream_count=3)

    _render_marketplace_result(logger, report, dry_run=False)

    logger.success.assert_called_once()
    msg = logger.success.call_args[0][0]
    assert "(2 direct + 3 upstream)" in msg, msg
    assert "Built marketplace.json" in msg


def test_success_message_with_only_upstream_packages() -> None:
    """Upstream-only build shows ``0 direct + N upstream`` breakdown."""
    logger = MagicMock()
    report = _make_report(resolved_count=0, upstream_count=4)

    _render_marketplace_result(logger, report, dry_run=False)

    msg = logger.success.call_args[0][0]
    assert "(0 direct + 4 upstream)" in msg, msg


def test_dry_run_message_counts_direct_plus_upstream() -> None:
    """Dry-run path renders the same total via ``dry_run_notice``."""
    logger = MagicMock()
    report = _make_report(resolved_count=1, upstream_count=2, dry_run=True)

    _render_marketplace_result(logger, report, dry_run=True)

    logger.dry_run_notice.assert_called_once()
    msg = logger.dry_run_notice.call_args[0][0]
    assert "(3 package(s))" in msg, msg
    assert "Would write marketplace.json" in msg


def test_no_packages_renders_zero_count() -> None:
    """Empty build -- count is 0, no off-by-one."""
    logger = MagicMock()
    report = _make_report(resolved_count=0, upstream_count=0)

    _render_marketplace_result(logger, report, dry_run=False)

    msg = logger.success.call_args[0][0]
    assert "(0 package(s))" in msg, msg
