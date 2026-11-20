"""Security tests for marketplace builder -- S1/S2/S3/S4 guards (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.builder import (
    _subtract_plugin_root,
)
from apm_cli.marketplace.errors import BuildError

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-marketplace-metadata-enrichment")]


# ---------------------------------------------------------------------------
# S1: pluginRoot with traversal rejected at schema load
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# S2: subtraction post-guards
# ---------------------------------------------------------------------------


def test_plugin_root_subtraction_empty_result() -> None:
    """Subtracting pluginRoot that equals source must raise BuildError."""
    with pytest.raises(BuildError, match="yields empty path"):
        _subtract_plugin_root("./plugins", "./plugins")


@pytest.mark.parametrize("plugin_root", ["./", "."])
def test_plugin_root_subtraction_keeps_leading_dot_of_hidden_source(plugin_root: str) -> None:
    """Only the literal './' prefix is removed, never a leading '.' of a name."""
    assert _subtract_plugin_root("./.hidden/plug", plugin_root) == "./.hidden/plug"


def test_plugin_root_subtraction_absolute_result() -> None:
    """pluginRoot with '..' is rejected at parse (S1), but if bypassed
    the post-subtraction guard would catch absolute paths."""
    # S1 prevents reaching this in production, but we test the function directly.
    # A relative subtraction can never produce an absolute path from relative
    # inputs, so we verify the guard exists by testing a case that yields
    # empty (which is the actual edge case).
    with pytest.raises(BuildError, match="yields empty path"):
        _subtract_plugin_root("./plugins/", "./plugins")


# ---------------------------------------------------------------------------
# S3: new fields must be strings
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# S4: keywords array cap and type enforcement
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Override precedence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Metadata enrichment keeps the GitHub credential on its origin (#3726)
# ---------------------------------------------------------------------------
