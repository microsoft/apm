"""Unit tests for apm_cli.utils.reflink -- copy-on-write file cloning (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.utils.reflink import (
    _reset_capability_cache,
    reflink_supported,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    """Reset the per-device capability cache between tests."""
    _reset_capability_cache()
    yield
    _reset_capability_cache()


@pytest.fixture(autouse=True)
def _clear_no_reflink_env(monkeypatch):
    """Remove APM_NO_REFLINK so each test starts from the same baseline."""
    monkeypatch.delenv("APM_NO_REFLINK", raising=False)


class TestReflinkSupported:
    """Test the reflink_supported() probe."""

    def test_apm_no_reflink_disables(self, monkeypatch):
        monkeypatch.setenv("APM_NO_REFLINK", "1")
        assert reflink_supported() is False

    def test_returns_bool(self):
        assert isinstance(reflink_supported(), bool)
