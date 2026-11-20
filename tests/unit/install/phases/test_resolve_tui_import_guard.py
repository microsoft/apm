"""The resolve phase source reads the TUI attribute defensively."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-user-facing-output-diagnostics")]


def test_resolve_module_imports_tui_attr_safely() -> None:
    """Resolve uses getattr(ctx, 'tui', None) -- ctx without tui is OK.

    Pins the duck-typed access pattern so older test fixtures
    constructing minimal contexts don't break.
    """
    from apm_cli.install.phases import resolve as resolve_mod

    # The module must use getattr(ctx, "tui", None) -- not direct
    # attribute access -- so a missing attr does not raise.
    src = resolve_mod.__file__
    with open(src) as fh:
        text = fh.read()
    assert 'getattr(ctx, "tui", None)' in text, (
        "resolve.py must access ctx.tui via getattr(...,None) so "
        "minimal/older context objects don't trigger AttributeError"
    )
