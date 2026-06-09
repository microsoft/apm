"""Tests for install-time hook transparency (issue #316).

Security contract: display_payloads must faithfully reflect the
post-path-rewrite hook data that is actually written to disk and
executed (_rewrite_hooks_data output). A summary that shows
pre-rewrite paths would give false assurance.
"""

import json
from pathlib import Path

from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import APMPackage, PackageInfo


def _make_package_info(install_path: Path, name: str = "hookify") -> PackageInfo:
    package = APMPackage(name=name, version="1.0.0")
    return PackageInfo(package=package, install_path=install_path)


def _setup_hook_package(tmp_path: Path) -> tuple[PackageInfo, Path]:
    """Create a minimal hook package with a pretooluse.py hook."""
    pkg_dir = tmp_path / "apm_modules" / "anthropics" / "hookify"
    hooks_dir = pkg_dir / "hooks"
    hooks_dir.mkdir(parents=True)

    hook_data = {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/pretooluse.py",
                            "timeout": 10,
                        }
                    ]
                }
            ]
        }
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
    (hooks_dir / "pretooluse.py").write_text("#!/usr/bin/env python3\nprint('ok')\n")
    return _make_package_info(pkg_dir), pkg_dir


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


def test_display_payloads_reflect_rewritten_paths_vscode(tmp_path):
    """VSCode: display_payloads must use post-rewrite paths (what's on disk),
    not the original ${CLAUDE_PLUGIN_ROOT} template strings."""
    package_info, _ = _setup_hook_package(tmp_path)
    integrator = HookIntegrator()

    result = integrator.integrate_package_hooks(
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
        target=None,
    )

    assert result.files_integrated == 1
    assert len(result.display_payloads) == 1

    payload = result.display_payloads[0]

    # Verify actions are present
    assert len(payload["actions"]) >= 1
    action = payload["actions"][0]
    assert action["event"] == "PreToolUse"
    # Summary must reference the rewritten path, not the template variable
    assert "${CLAUDE_PLUGIN_ROOT}" not in action["summary"]
    assert ".github" in action["summary"]

    # Verify rendered_json reflects what was written to disk
    target_file = tmp_path / ".github" / "hooks" / "hookify-hooks.json"
    assert target_file.exists()
    on_disk = json.loads(target_file.read_text())
    payload_json = json.loads(payload["rendered_json"])
    assert on_disk == payload_json, (
        "display_payloads rendered_json must match what was written to disk"
    )


def test_display_payloads_reflect_rewritten_paths_claude(tmp_path):
    """Claude: display_payloads rendered_json must reflect the rewritten data
    that was merged into .claude/settings.json."""
    package_info, _ = _setup_hook_package(tmp_path)
    (tmp_path / ".claude").mkdir()
    integrator = HookIntegrator()

    result = integrator.integrate_package_hooks_claude(
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
    )

    assert result.files_integrated >= 1
    assert len(result.display_payloads) >= 1

    payload = result.display_payloads[0]

    # The rewritten data must not contain the template variable
    rendered_str = payload["rendered_json"]
    assert "${CLAUDE_PLUGIN_ROOT}" not in rendered_str

    # The path in the rendered JSON must reference the .claude subtree
    payload_json = json.loads(rendered_str)
    rendered_commands = [
        v
        for hook_list in payload_json.get("hooks", {}).values()
        for entry in hook_list
        if isinstance(entry, dict)
        for inner in entry.get("hooks", [entry])
        if isinstance(inner, dict)
        for v in [inner.get("command", "")]
        if v
    ]
    assert any(".claude" in cmd for cmd in rendered_commands), (
        "Claude display_payloads must show .claude/-rewritten paths, not templates"
    )


def test_display_payloads_empty_when_no_hooks_integrated(tmp_path):
    """No hooks directory -> no payloads."""
    pkg_dir = tmp_path / "apm_modules" / "anthropics" / "empty-pkg"
    pkg_dir.mkdir(parents=True)
    package_info = _make_package_info(pkg_dir, name="empty-pkg")
    integrator = HookIntegrator()

    result = integrator.integrate_package_hooks(
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
        target=None,
    )

    assert result.files_integrated == 0
    assert result.display_payloads == []


def test_iter_hook_entries_matches_what_build_display_payload_shows(tmp_path):
    """Verify _build_display_payload uses _iter_hook_entries on the rewritten dict,
    not the original, ensuring summary == executed content."""
    package_info, _ = _setup_hook_package(tmp_path)
    integrator = HookIntegrator()

    result = integrator.integrate_package_hooks(
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
        target=None,
    )

    assert result.files_integrated == 1
    payload = result.display_payloads[0]
    rewritten_dict = json.loads(payload["rendered_json"])

    # Re-derive actions from the same rewritten dict
    expected_actions = [
        {"event": ev, "summary": HookIntegrator._summarize_command(entry)}
        for ev, entry in HookIntegrator._iter_hook_entries(rewritten_dict)
    ]
    assert payload["actions"] == expected_actions, (
        "actions in display_payload must exactly match _iter_hook_entries on the "
        "rewritten (on-disk) dict"
    )


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


def test_display_payloads_claude_omit_apm_source(tmp_path):
    """Claude settings.json is schema-strict: _apm_source is stripped before
    the disk write. rendered_json must therefore NOT contain _apm_source,
    or it falsely advertises a key the executed config does not have."""
    package_info, _ = _setup_hook_package(tmp_path)
    (tmp_path / ".claude").mkdir()
    integrator = HookIntegrator()

    result = integrator.integrate_package_hooks_claude(
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
    )

    assert result.files_integrated >= 1
    payload = result.display_payloads[0]
    assert "_apm_source" not in payload["rendered_json"], (
        "Claude display_payloads must not show _apm_source -- it is stripped "
        "from the on-disk settings.json"
    )

    # And the rendered hooks must match the on-disk settings.json entries.
    on_disk = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    rendered = json.loads(payload["rendered_json"])
    for event_name, matchers in rendered.get("hooks", {}).items():
        for matcher in matchers:
            assert matcher in on_disk["hooks"][event_name], (
                "every rendered Claude entry must be present verbatim on disk"
            )


# ---------------------------------------------------------------------------
# Regression: Gemini rendered_json must reflect the Gemini transform on disk
# ---------------------------------------------------------------------------


def _setup_gemini_hook_package(tmp_path: Path) -> PackageInfo:
    pkg_dir = tmp_path / "apm_modules" / "anthropics" / "gemini-pkg"
    hooks_dir = pkg_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_data = {
        "hooks": {
            "PreToolUse": [{"bash": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/run.sh", "timeoutSec": 5}]
        }
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
    (hooks_dir / "run.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    return _make_package_info(pkg_dir, name="gemini-pkg")


def test_display_payloads_gemini_reflect_transform(tmp_path):
    """Gemini disk format renames bash->command and timeoutSec(s)->timeout(ms).
    rendered_json must show the transformed schema actually written to disk,
    not the pre-transform Copilot schema."""
    from apm_cli.integration.hook_integrator import _MERGE_HOOK_TARGETS

    package_info = _setup_gemini_hook_package(tmp_path)
    (tmp_path / ".gemini").mkdir()
    integrator = HookIntegrator()

    result = integrator._integrate_merged_hooks(
        _MERGE_HOOK_TARGETS["gemini"],
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
    )

    assert result.files_integrated >= 1
    payload = result.display_payloads[0]
    rendered_str = payload["rendered_json"]

    assert '"bash"' not in rendered_str, (
        "Gemini rendered_json must not show pre-transform 'bash' key"
    )
    assert '"timeoutSec"' not in rendered_str
    assert '"command"' in rendered_str

    # rendered hooks must match the transformed on-disk settings.json.
    on_disk = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    rendered = json.loads(rendered_str)
    for event_name, matchers in rendered.get("hooks", {}).items():
        for matcher in matchers:
            assert matcher in on_disk["hooks"][event_name], (
                "every rendered Gemini entry must be present verbatim on disk"
            )


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
