"""Owner tests for ``apm_cli.install.outcome`` (install-outcome owner).

Table-driven at the owner symbol so the mutation floor for install-outcome
(tests/mutation/floors.json) is defended by behavior, not by line coverage.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.install.outcome import (
    diagnostic_error_count,
    install_command_outcome,
    missing_requested_components,
    require_requested_components,
)
from apm_cli.models.results import InstallDisposition, InstallResult

pytestmark = pytest.mark.unit


class _RecordingDiagnostics:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []

    def error(self, message: str, *, package: str) -> None:
        self.errors.append((message, package))


@pytest.mark.parametrize(
    ("disposition", "exit_code", "summary_allowed"),
    [
        (InstallDisposition.SUCCESS, 0, True),
        (InstallDisposition.PARTIAL_SUCCESS, 0, True),
        (InstallDisposition.DRY_RUN, 0, True),
        (InstallDisposition.CANCELLED, 0, False),
        (InstallDisposition.VALIDATION_FAILED, 1, False),
        (InstallDisposition.FAILED, 1, False),
    ],
)
def test_install_command_outcome_maps_every_disposition(
    disposition: InstallDisposition, exit_code: int, summary_allowed: bool
) -> None:
    outcome = install_command_outcome(InstallResult(disposition=disposition))

    assert outcome.exit_code == exit_code
    assert outcome.success_summary_allowed is summary_allowed


@pytest.mark.parametrize(
    ("diagnostics", "expected"),
    [
        (None, 0),
        (SimpleNamespace(error_count=3), 3),
        (SimpleNamespace(error_count="2"), 2),
        (SimpleNamespace(), 0),
        (SimpleNamespace(error_count="many"), 0),
        (SimpleNamespace(error_count=object()), 0),
    ],
)
def test_diagnostic_error_count_is_defensive(diagnostics: object, expected: int) -> None:
    assert diagnostic_error_count(diagnostics) == expected


def test_missing_requested_components_normalizes_separators_and_keeps_order() -> None:
    missing = missing_requested_components(
        requested=["skills\\b", "skills/a", "skills/c", "./skills/a"],
        available={"skills/a", "skills\\b"},
    )

    assert missing == ("skills/c",)


def test_require_requested_components_accepts_when_all_available() -> None:
    diagnostics = _RecordingDiagnostics()

    accepted = require_requested_components(
        diagnostics,
        option="--skill",
        component="skill",
        requested=["a"],
        available=["a", "b"],
        package="owner/pkg",
    )

    assert accepted is True
    assert diagnostics.errors == []


def test_require_requested_components_reports_partial_mismatch_once() -> None:
    diagnostics = _RecordingDiagnostics()

    accepted = require_requested_components(
        diagnostics,
        option="--skill",
        component="skill",
        requested=["a", "z", "y"],
        available=["b", "a"],
        package="owner/pkg",
    )

    assert accepted is False
    assert diagnostics.errors == [
        (
            "--skill did not match skills in 'owner/pkg'. Requested: z, y. "
            "Available: a, b. Choose an available skill or update the package "
            "manifest, then reinstall.",
            "owner/pkg",
        )
    ]


def test_require_requested_components_reports_total_mismatch_with_none_available() -> None:
    diagnostics = _RecordingDiagnostics()

    accepted = require_requested_components(
        diagnostics,
        option="--agent",
        component="agent",
        requested=["x"],
        available=[],
        package="pkg",
    )

    assert accepted is False
    assert diagnostics.errors == [
        (
            "--agent matched no declared agents in 'pkg'. Requested: x. "
            "Available: (none). Choose an available agent or update the package "
            "manifest, then reinstall.",
            "pkg",
        )
    ]
