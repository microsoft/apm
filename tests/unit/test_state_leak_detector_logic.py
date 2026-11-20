"""Pure-logic cases for the state-leak detector (tests/utils/state_leaks.py)."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from tests.utils import state_leaks

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]


def test_session_check_names_mock_bound_module_globals() -> None:
    """A module first imported under a patch keeps the mock; the check names it."""
    leaked = types.ModuleType("apm_cli.fake_leaked")
    leaked.is_github_hostname = MagicMock(return_value=False)
    clean = types.ModuleType("apm_cli.fake_clean")
    clean.is_github_hostname = len
    outside = types.ModuleType("other_pkg")
    outside.helper = MagicMock()

    found = state_leaks.mock_bound_module_globals(
        {"apm_cli.fake_leaked": leaked, "apm_cli.fake_clean": clean, "other_pkg": outside}
    )

    assert found == ["apm_cli.fake_leaked.is_github_hostname"]
