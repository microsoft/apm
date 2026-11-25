"""Printed next step for a stale recorded executables decision (#4053)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.install.frozen_retirement import _stale_decisions_error
from apm_cli.policy.ci_checks import _check_exec_status_current

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-remediation-advice")]

_STALE = SimpleNamespace(describe=lambda: "hooky: recorded deployed, now denied")
_CLAUSE = (
    "run 'apm install' to record the executables decisions the gate makes now "
    "(without --frozen, keeping --root DIR or --global if specified), then commit apm.lock.yaml"
)


def test_frozen_refusal_tip_names_the_catalog_next_step() -> None:
    error = _stale_decisions_error([_STALE])

    assert error.tip == f"Tip: R{_CLAUSE[1:]}."


def test_audit_exec_status_current_names_the_catalog_next_step(tmp_path: Path) -> None:
    with patch("apm_cli.install.exec_gate.stale_exec_decisions", return_value=([_STALE], False)):
        result = _check_exec_status_current(
            tmp_path,
            SimpleNamespace(),
            SimpleNamespace(),
            (),
            user_scope=False,
            modules_root=tmp_path / "apm_modules",
            policy_provider=lambda: None,
        )

    assert not result.passed
    assert result.message == (
        f"1 package records an executables decision the gate no longer makes -- {_CLAUSE}"
    )
