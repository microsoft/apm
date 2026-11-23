"""When the root CLI's update notice defers to the helpers (update_notice.py).

The notice declines only where ``_check_and_notify_updates`` would print and
fetch nothing; everywhere else the helper runs exactly as before.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apm_cli import update_notice

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-cli-startup-import-budget")]


def _notified(*, due: bool, enabled: bool = True, refused: bool = False) -> bool:
    calls: list[int] = []
    with (
        patch("apm_cli.update_policy.is_self_update_enabled", return_value=enabled),
        patch("apm_cli.deps.egress_route.is_refused", return_value=refused),
        patch("apm_cli.utils.version_checker.should_check_for_updates", return_value=due),
        patch(
            "apm_cli.commands._helpers._check_and_notify_updates",
            side_effect=lambda: calls.append(1),
        ),
    ):
        update_notice.check_and_notify_updates()
    return bool(calls)


def test_a_due_daily_check_runs_the_notice(monkeypatch) -> None:
    monkeypatch.delenv("APM_E2E_TESTS", raising=False)
    assert _notified(due=True) is True


def test_a_fresh_daily_check_skips_the_notice(monkeypatch) -> None:
    monkeypatch.delenv("APM_E2E_TESTS", raising=False)
    assert _notified(due=False) is False


def test_the_install_decision_seam_skips_the_notice(monkeypatch) -> None:
    """APM_INSTALL_FAST_PATH=decide promises no writes: no update cache, no fetch."""
    monkeypatch.delenv("APM_E2E_TESTS", raising=False)
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "decide")
    assert _notified(due=True) is False


def test_disabled_self_update_skips_the_notice(monkeypatch) -> None:
    monkeypatch.delenv("APM_E2E_TESTS", raising=False)
    assert _notified(due=True, enabled=False) is False


def test_a_refused_version_check_still_reaches_the_helper(monkeypatch) -> None:
    """The helper prints the verbose 'update check skipped' note."""
    monkeypatch.delenv("APM_E2E_TESTS", raising=False)
    assert _notified(due=False, refused=True) is True
