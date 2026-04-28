"""Shared fixtures for ``tests/unit/marketplace/``."""

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
def _enable_marketplace_flag():
    """Pre-enable the ``marketplace_authoring`` experimental flag.

    Tests in this directory that invoke the ``marketplace`` Click group
    need the flag enabled so the group callback does not exit early.

    Patches ``is_enabled`` at the source module so it survives any
    config-cache isolation performed by individual tests.
    """
    with patch(
        "apm_cli.core.experimental.is_enabled",
        side_effect=_marketplace_enabled_is_enabled,
    ):
        yield
