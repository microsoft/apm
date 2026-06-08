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
        None,  # target (uses default root_dir)
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
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
    target_file = tmp_path / ".github" / "hooks" / "anthropics-hookify-hooks.json"
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
    payload_json = json.loads(payload["rendered_json"])

    # The rewritten data must not contain the template variable
    rendered_str = payload["rendered_json"]
    assert "${CLAUDE_PLUGIN_ROOT}" not in rendered_str

    # The path in the rendered JSON must reference the .claude subtree
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
        None,
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
    )

    assert result.files_integrated == 0
    assert result.display_payloads == []


def test_iter_hook_entries_matches_what_build_display_payload_shows(tmp_path):
    """Verify _build_display_payload uses _iter_hook_entries on the rewritten dict,
    not the original, ensuring summary == executed content."""
    package_info, _ = _setup_hook_package(tmp_path)
    integrator = HookIntegrator()

    result = integrator.integrate_package_hooks(
        None,
        package_info,
        tmp_path,
        force=False,
        managed_files=set(),
        diagnostics=None,
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
