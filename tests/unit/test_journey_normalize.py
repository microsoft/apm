"""Golden normalisation rules shared by the lifecycle journeys (N5, N7)."""

import pytest

from tests.lifecycle.journeys import normalize as norm

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I9")]


def test_golden_rules_follow_the_o4_exemptions_and_resolve_order() -> None:
    """N5/N7: allow_repeat lines are not refused; resolve progress compares in any order."""
    text = "$ apm install --frozen\n|     +- wrote: .mcp.json\n|     +- wrote: .mcp.json\n"
    assert norm.golden_problems(text) == ["duplicate line '|     +- wrote: .mcp.json'"]
    assert norm.golden_problems(text, ["|     +- wrote: .mcp.json"]) == []
    first = ["[>] Resolving b...", "[>] Resolving a...", "[i] Targets: copilot"]
    second = ["[>] Resolving a...", "[>] Resolving b...", "[i] Targets: copilot"]
    assert norm.canonical(first) == norm.canonical(second)
    assert norm.canonical(["[i] x", "[i] y"]) != norm.canonical(["[i] y", "[i] x"])
