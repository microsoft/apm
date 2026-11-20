"""Unit tests for target-aware hook event diagnostics.

Covers:
- _emit_hook_event_diagnostics logs event names per target at INFO level
- Naming-convention mismatch triggers a user-visible warning
- Multi-target fixture: per-target diagnostics distinguish events correctly
- Full integration: integrate_hooks_for_target emits diagnostics during deploy
- Regression: diagnostics suppressed when collision forces hook file skip
"""

import logging

import pytest

from apm_cli.integration.hook_integrator import (
    _HOOK_EVENT_EXPECTED_CASING,
    _detect_event_casing,
    _emit_hook_event_diagnostics,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _detect_event_casing
# ---------------------------------------------------------------------------


class TestDetectEventCasing:
    def test_camel_case(self):
        assert _detect_event_casing("preToolUse") == "camelCase"

    def test_camel_case_session_start(self):
        assert _detect_event_casing("sessionStart") == "camelCase"

    def test_pascal_case(self):
        assert _detect_event_casing("PreToolUse") == "PascalCase"

    def test_pascal_case_session_start(self):
        assert _detect_event_casing("SessionStart") == "PascalCase"

    def test_lowercase_only(self):
        assert _detect_event_casing("stop") is None

    def test_empty_string(self):
        assert _detect_event_casing("") is None


# ---------------------------------------------------------------------------
# _HOOK_EVENT_EXPECTED_CASING coverage
# ---------------------------------------------------------------------------


class TestHookEventExpectedCasing:
    def test_copilot_expects_camel(self):
        assert _HOOK_EVENT_EXPECTED_CASING["copilot"] == "camelCase"

    def test_claude_expects_pascal(self):
        assert _HOOK_EVENT_EXPECTED_CASING["claude"] == "PascalCase"

    def test_cursor_expects_camel(self):
        assert _HOOK_EVENT_EXPECTED_CASING["cursor"] == "camelCase"

    def test_vscode_expects_pascal(self):
        assert _HOOK_EVENT_EXPECTED_CASING["vscode"] == "PascalCase"


# ---------------------------------------------------------------------------
# _emit_hook_event_diagnostics -- INFO logging
# ---------------------------------------------------------------------------


class TestEmitHookEventDiagnosticsLogging:
    """Verify that per-target event names appear in structured INFO logs."""

    def test_logs_events_at_info_level(self, caplog):
        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            _emit_hook_event_diagnostics(["PreToolUse", "Stop"], "claude", {})

        assert "claude" in caplog.text
        assert "PreToolUse" in caplog.text
        assert "Stop" in caplog.text

    def test_logs_events_for_copilot_target(self, caplog):
        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            _emit_hook_event_diagnostics(["preToolUse", "postToolUse"], "copilot", {})

        assert "copilot" in caplog.text
        assert "preToolUse" in caplog.text
        assert "postToolUse" in caplog.text

    def test_no_log_for_empty_events(self, caplog):
        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            _emit_hook_event_diagnostics([], "claude", {})

        assert caplog.text == ""

    def test_multi_target_logs_are_distinct(self, caplog):
        """Events logged per target must be distinguishable in log output."""
        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            _emit_hook_event_diagnostics(["PreToolUse", "Stop"], "claude", {})
            _emit_hook_event_diagnostics(["preToolUse", "postToolUse"], "copilot", {})

        records = [r for r in caplog.records if r.levelno == logging.INFO]
        claude_records = [r for r in records if "claude" in r.getMessage()]
        copilot_records = [r for r in records if "copilot" in r.getMessage()]

        assert claude_records, "Expected INFO record for claude target"
        assert copilot_records, "Expected INFO record for copilot target"
        # Each target's record contains that target's events
        assert "PreToolUse" in claude_records[0].getMessage()
        assert "preToolUse" in copilot_records[0].getMessage()


# ---------------------------------------------------------------------------
# _emit_hook_event_diagnostics -- mismatch warnings
# ---------------------------------------------------------------------------


class TestEmitHookEventDiagnosticsMismatch:
    """Convention-mismatch events without a known mapping trigger user warning."""

    def test_warns_camel_event_on_pascal_target(self, capsys):
        # An unknown camelCase event has no native Claude mapping.
        _emit_hook_event_diagnostics(["mysteryEvent"], "claude", {})
        captured = capsys.readouterr()
        assert "mysteryEvent" in captured.out

    def test_warns_pascal_event_on_camel_target(self, capsys):
        # "PreToolUse" is PascalCase but copilot expects camelCase;
        # no mapping exists so it may not be recognized.
        _emit_hook_event_diagnostics(["PreToolUse"], "copilot", {})
        captured = capsys.readouterr()
        assert "PreToolUse" in captured.out

    def test_no_warn_when_event_is_mapped(self, capsys):
        # "preToolUse" is remapped to "PreToolUse" for claude, so no warning.
        event_map = {"preToolUse": "PreToolUse"}
        _emit_hook_event_diagnostics(["preToolUse"], "claude", event_map)
        captured = capsys.readouterr()
        # No user-visible warning about mismatched casing for a mapped event
        assert "may not be recognized" not in captured.out

    def test_no_warn_when_casing_matches_target(self, capsys):
        _emit_hook_event_diagnostics(["PreToolUse", "Stop"], "claude", {})
        captured = capsys.readouterr()
        assert "may not be recognized" not in captured.out


# ---------------------------------------------------------------------------
# Multi-target integration fixture
# ---------------------------------------------------------------------------
