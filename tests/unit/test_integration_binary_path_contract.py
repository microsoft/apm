"""Contracts for authoritative integration-test binary selection."""

import sys

import pytest

from tests.integration import conftest as integration_conftest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_binary_resolution_cache() -> None:
    integration_conftest._resolve_apm_binary.cache_clear()
    yield
    integration_conftest._resolve_apm_binary.cache_clear()


def test_engine_command_is_explicitly_independent_of_packaged_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary-fault contracts use the installed engine, never a frozen fallback."""
    monkeypatch.setenv("APM_BINARY_PATH", "/deliberately/not/an/engine")
    command = integration_conftest.apm_engine_command.__wrapped__()
    assert command[0] == sys.executable
    assert command[1:] == ("-m", "apm_cli.cli")
