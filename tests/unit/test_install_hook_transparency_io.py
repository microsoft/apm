"""Tests for install-time hook transparency (issue #316) (filesystem, process and CLI boundary cases)."""

import json
from pathlib import Path

import pytest

from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import APMPackage, PackageInfo

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-neutral-hook-contract", "P-OWN-apm-package-manifest-construction"),
]


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
    assert action["event"] == "preToolUse"
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


# ---------------------------------------------------------------------------
# Regression: command summaries are always single-line (log-spoofing guard)
# ---------------------------------------------------------------------------


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
