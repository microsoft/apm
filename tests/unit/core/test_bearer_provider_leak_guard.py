"""A bearer provider left patched fails its own test, not a later shard (#4830).

Overlapping per-thread ``patch()`` contexts on ``azure_cli.get_bearer_provider``
restore each other's mock and leave the module global patched for the rest of
the xdist worker. The per-test state-leak detector tracks the global, so the
test that leaks it fails and the next test sees the real provider.
"""

from unittest.mock import MagicMock

import pytest

from apm_cli.core import azure_cli
from tests.utils import state_leaks

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-host-credential-resolution")]


def test_a_bearer_provider_left_patched_is_a_leak_and_is_restored() -> None:
    real = azure_cli.get_bearer_provider
    before = state_leaks.take_snapshot()
    azure_cli.get_bearer_provider = MagicMock()
    try:
        leaked = state_leaks.leaked_keys(before, state_leaks.take_snapshot())
        state_leaks.restore(before)
    finally:
        restored = azure_cli.get_bearer_provider
        azure_cli.get_bearer_provider = real

    assert leaked == ["apm_cli.core.azure_cli.get_bearer_provider"]
    assert restored is real
