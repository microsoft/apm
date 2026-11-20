"""Architecture regression for the console stream and wrap-mode owner."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_ROOT = Path(__file__).resolve().parents[2]


def test_console_stream_guard_pins_the_no_wrap_decision() -> None:
    """Dropping the non-terminal soft-wrap decision in the console owner is caught."""
    path = "src/apm_cli/utils/console.py"
    source = (_ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace("soft_wrap = soft_wrap or not console.is_terminal", "pass", 1)
    assert mutated != source
    rule_id = "registry_delegation.console_streams"

    result = run_selected_rules(_ROOT, {rule_id}, source_overrides={path: mutated})

    assert result.failures == ()
    assert [violation.message for violation in result.violations] == [
        "_rich_echo must keep the stream decision: soft_wrap = soft_wrap or not console.is_terminal"
    ]
