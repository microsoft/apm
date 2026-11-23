"""Which lifecycle ``apm audit`` runs the records-off parity replay repeats (#4940)."""

from __future__ import annotations

import pytest

from tests.utils.apm_lifecycle_runner import _audit_parity_applies, _without_temp_names

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-audit-scratch-materialization")]


@pytest.mark.parametrize(
    "args",
    [
        ("audit", "--strip"),
        ("audit", "--ci", "-o", "apm.sarif"),
        ("audit", "--ci", "--output", "apm.sarif"),
        ("audit", "--ci", "--output=apm.sarif"),
        ("audit", "--ci", "-oapm.sarif"),
    ],
)
def test_an_audit_that_writes_is_not_replayed(args: tuple[str, ...]) -> None:
    assert not _audit_parity_applies(args, {})


@pytest.mark.parametrize("args", [("audit",), ("audit", "--ci", "-f", "sarif"), ("audit", "-v")])
def test_a_read_only_audit_is_replayed_with_records_off(args: tuple[str, ...]) -> None:
    assert _audit_parity_applies(args, {})
    assert not _audit_parity_applies(args, {"APM_AUDIT_RECORDS": "1"})


def test_fresh_scratch_names_are_not_an_audit_difference() -> None:
    served = "blocked (/t/apm_drift_a9cr8vut/apm_modules/x) and /t/tmpab12cd34/y"
    live = "blocked (/t/apm_drift_yg5oapbq/apm_modules/x) and /t/tmpzz98yy76/y"

    assert _without_temp_names(served) == _without_temp_names(live)
    assert _without_temp_names("/t/apm_drift_a9cr8vut/a") != _without_temp_names("/t/other/a")
