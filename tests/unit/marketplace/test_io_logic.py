"""Tests for _io.py -- shared atomic write helper (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace._io import __all__

pytestmark = pytest.mark.unit


class TestAtomicWrite:
    """Tests for the shared ``atomic_write()`` function."""

    # ------------------------------------------------------------------
    # Tmp file path
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Exception during write: tmp file is cleaned up
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # __all__
    # ------------------------------------------------------------------

    def test_dunder_all_contains_atomic_write(self) -> None:
        assert "atomic_write" in __all__

    def test_dunder_all_does_not_expose_os(self) -> None:
        assert "os" not in __all__
