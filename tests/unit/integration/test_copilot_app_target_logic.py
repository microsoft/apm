"""Unit tests for copilot-app target gating in apm_cli.integration.targets (pure logic cases)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apm_cli.integration.targets import (
    KNOWN_TARGETS,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_config_cache():
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


class TestForScope:
    def test_for_scope_resolver_returns_none(self) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_app_root",
            return_value=None,
        ):
            result = KNOWN_TARGETS["copilot-app"].for_scope(user_scope=True)
        assert result is None
