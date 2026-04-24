"""Shared fixtures for ``tests/unit/commands/``."""

from __future__ import annotations

from unittest.mock import patch

import pytest


# Cache the *real* is_enabled so we can delegate non-marketplace flags.
from apm_cli.core.experimental import is_enabled as _real_is_enabled


def _marketplace_enabled_is_enabled(name: str) -> bool:
    """Stub that forces ``marketplace_authoring`` to True."""
    if name == "marketplace_authoring":
        return True
    return _real_is_enabled(name)


@pytest.fixture(autouse=True)
def _enable_marketplace_flag(request):
    """Pre-enable the ``marketplace_authoring`` experimental flag.

    The marketplace group callback guards execution behind this flag.
    All *existing* marketplace tests need the flag enabled so they
    exercise the subcommand logic rather than hitting the guard.

    Only applies to test modules whose name contains "marketplace"
    (excluding ``test_marketplace_gating`` which tests disabled state).

    Patches ``is_enabled`` at the source module so it survives any
    config-cache isolation performed by individual tests.
    """
    module_name = request.module.__name__
    is_marketplace_test = "marketplace" in module_name
    is_gating_test = "gating" in module_name

    if is_marketplace_test and not is_gating_test:
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=_marketplace_enabled_is_enabled,
        ):
            yield
    else:
        yield
