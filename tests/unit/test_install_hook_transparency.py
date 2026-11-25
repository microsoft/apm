"""Tests for install-time hook transparency (issue #316).

Security contract: display_payloads must faithfully reflect the
post-path-rewrite hook data that is actually written to disk and
executed (_rewrite_hooks_data output). A summary that shows
pre-rewrite paths would give false assurance.
"""

import pytest

from apm_cli.integration.hook_integrator import HookIntegrator

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Unit tests for HookIntegrator helper methods
# ---------------------------------------------------------------------------


class TestIterHookEntries:
    def test_flat_command(self):
        payload = {"hooks": {"PreToolUse": [{"command": "echo hi"}]}}
        entries = HookIntegrator._iter_hook_entries(payload)
        assert entries == [("PreToolUse", {"command": "echo hi"})]

    def test_nested_hooks(self):
        payload = {"hooks": {"PostToolUse": [{"hooks": [{"command": "python3 run.py"}]}]}}
        entries = HookIntegrator._iter_hook_entries(payload)
        assert entries == [("PostToolUse", {"command": "python3 run.py"})]

    def test_empty_payload(self):
        assert HookIntegrator._iter_hook_entries({}) == []

    def test_non_dict_hooks_skipped(self):
        payload = {"hooks": "bad"}
        assert HookIntegrator._iter_hook_entries(payload) == []


class TestSummarizeCommand:
    def test_path_token_extracted(self):
        summary = HookIntegrator._summarize_command(
            {"command": "python3 .github/hooks/scripts/x.py"}
        )
        assert summary == "runs .github/hooks/scripts/x.py"

    def test_no_path_falls_back_to_command(self):
        summary = HookIntegrator._summarize_command({"command": "echo hello"})
        assert summary == "runs echo hello"

    def test_empty_entry_returns_default(self):
        summary = HookIntegrator._summarize_command({})
        assert summary == "runs hook command"

    def test_bash_key_used(self):
        summary = HookIntegrator._summarize_command({"bash": "bash /scripts/run.sh"})
        assert "run.sh" in summary


# ---------------------------------------------------------------------------
# Security contract: display_payloads matches on-disk/executed content
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Regression: all six HOOK_COMMAND_KEYS produce transparency output
# ---------------------------------------------------------------------------


def test_iter_hook_entries_includes_os_specific_keys():
    """windows / linux / osx hook keys must each surface an action, not just
    command / bash / powershell. Hooks using OS-specific keys deploy
    correctly, so they must not be invisible in the transparency output."""
    payload = {
        "hooks": {
            "PreToolUse": [
                {"windows": "scripts/w.ps1", "linux": "scripts/l.sh", "osx": "scripts/m.sh"}
            ]
        }
    }
    entries = HookIntegrator._iter_hook_entries(payload)
    keys_seen = {next(iter(entry)) for _event, entry in entries}
    assert keys_seen == {"windows", "linux", "osx"}, (
        "OS-specific hook keys must each produce a transparency entry"
    )


def test_summarize_command_handles_os_specific_keys():
    summary = HookIntegrator._summarize_command({"linux": "bash .github/hooks/run.sh"})
    assert "run.sh" in summary


# ---------------------------------------------------------------------------
# Regression: command summaries are always single-line (log-spoofing guard)
# ---------------------------------------------------------------------------


def test_summarize_command_collapses_internal_newlines():
    """A hook command containing a newline must not yield a multi-line
    summary -- that would break install-log formatting and enable
    log-spoofing (Copilot inline finding)."""
    summary = HookIntegrator._summarize_command({"command": "echo ok\nrm -rf safehouse"})
    assert "\n" not in summary
    assert "\r" not in summary
    assert summary == "runs echo ok rm -rf safehouse"


def test_summarize_command_collapses_tabs_and_runs():
    summary = HookIntegrator._summarize_command({"command": "echo\t\thello   world"})
    assert summary == "runs echo hello world"


# ---------------------------------------------------------------------------
# Regression: Claude rendered_json must not carry _apm_source (not on disk)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Regression: Gemini rendered_json must reflect the Gemini transform on disk
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Coverage: the user-visible install logger for hook transparency
# ---------------------------------------------------------------------------


def test_log_hook_display_payloads_emits_actions_and_verbose_json():
    """_log_hook_display_payloads is the headline user-visible function; it
    must emit one action line per action and, in verbose mode, the rendered
    JSON block."""
    from apm_cli.install.services import _log_hook_display_payloads

    emitted: list = []

    class _Logger:
        def __init__(self):
            self.details: list = []

        def verbose_detail(self, line):
            self.details.append(line)

    logger = _Logger()
    payloads = [
        {
            "source_hook_file": "hooks.json",
            "output_path": ".claude/settings.json",
            "actions": [{"event": "PreToolUse", "summary": "runs .claude/hooks/x.py"}],
            "rendered_json": '{\n  "hooks": {}\n}',
        }
    ]

    _log_hook_display_payloads(payloads, verbose=True, log_fn=emitted.append, logger=logger)

    joined = "\n".join(emitted)
    assert "PreToolUse" in joined
    assert "runs .claude/hooks/x.py" in joined
    assert "hooks.json" in joined
    # Verbose mode renders the JSON block line-by-line via the logger.
    assert any("hooks" in d for d in logger.details)


def test_log_hook_display_payloads_no_actions_fallback():
    from apm_cli.install.services import _log_hook_display_payloads

    emitted: list = []
    payloads = [{"source_hook_file": "empty.json", "actions": [], "rendered_json": ""}]
    _log_hook_display_payloads(payloads, verbose=False, log_fn=emitted.append, logger=None)
    assert any("empty.json" in line for line in emitted)


def test_log_hook_display_payloads_prints_an_identical_line_once():
    """Targets that render one hook identically list it once per package (#4336)."""
    from apm_cli.install.services import _log_hook_display_payloads

    action = {"event": "PreToolUse", "summary": "runs echo hi"}
    payloads = [
        {"source_hook_file": "lint.json", "actions": [action]},
        {"source_hook_file": "lint.json", "actions": [action]},
        {"source_hook_file": "lint.json", "actions": [{**action, "event": "preToolUse"}]},
    ]
    emitted: list[str] = []
    _log_hook_display_payloads(payloads, verbose=False, log_fn=emitted.append, logger=None)
    assert emitted == [
        "  |   PreToolUse: runs echo hi (lint.json)",
        "  |   preToolUse: runs echo hi (lint.json)",
    ]
