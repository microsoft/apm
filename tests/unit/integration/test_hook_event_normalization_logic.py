"""Regression coverage for native hook event normalization (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.integration.hook_integrator import (
    _HOOK_EVENT_MAP,
    _emit_hook_event_diagnostics,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("target", "source_event", "native_event"),
    [
        ("copilot", "SessionStart", "sessionStart"),
        ("copilot", "sessionStart", "sessionStart"),
        ("copilot", "Stop", "agentStop"),
        ("copilot", "AgentStop", "agentStop"),
        ("copilot", "agentStop", "agentStop"),
        ("copilot", "UserPromptSubmit", "userPromptSubmitted"),
        ("copilot", "userPromptSubmit", "userPromptSubmitted"),
        ("copilot", "userPromptSubmitted", "userPromptSubmitted"),
        ("claude", "SessionStart", "SessionStart"),
        ("claude", "sessionStart", "SessionStart"),
        ("claude", "Stop", "Stop"),
        ("claude", "AgentStop", "Stop"),
        ("claude", "agentStop", "Stop"),
    ],
)
def test_supported_aliases_map_to_documented_native_events(
    target: str,
    source_event: str,
    native_event: str,
) -> None:
    assert _HOOK_EVENT_MAP[target][source_event] == native_event


def test_unsupported_lowercase_stop_is_not_declared_as_native_alias() -> None:
    assert "stop" not in _HOOK_EVENT_MAP["copilot"]
    assert "stop" not in _HOOK_EVENT_MAP["claude"]


def test_supported_alias_diagnostics_do_not_warn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    aliases = ["SessionStart", "sessionStart", "Stop", "AgentStop", "agentStop"]

    for target in ("copilot", "claude"):
        _emit_hook_event_diagnostics(aliases, target, _HOOK_EVENT_MAP[target])

    assert "may not be recognized" not in capsys.readouterr().out
