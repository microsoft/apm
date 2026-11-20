"""Contracts separating live ADO acceptance from release publication (pure logic cases)."""

from __future__ import annotations

import pytest

from tests.integration import test_ado_e2e

pytestmark = pytest.mark.unit


def test_ado_e2e_is_live_and_credential_gated() -> None:
    """The real ADO fixture requires both opt-in scheduling and a PAT."""
    assert {mark.name for mark in test_ado_e2e.pytestmark} == {
        "live",
        "requires_ado_pat",
    }
