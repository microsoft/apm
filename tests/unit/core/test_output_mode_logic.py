"""Regression tests for machine-output detection at the root CLI (pure logic cases)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_human_output_is_not_machine_readable() -> None:
    from apm_cli.core.output_mode import detect_output_mode

    assert not detect_output_mode(("policy", "status", "--output", "table")).machine_readable
