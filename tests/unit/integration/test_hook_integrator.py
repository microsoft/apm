"""Unit tests for HookIntegrator.

Tests cover:
- Hook file discovery (.apm/hooks/ and hooks/ directories)
- VSCode integration (JSON copy + script copy + path rewriting)
- Claude integration (settings.json merge + script copy)
- Sync/cleanup integration (nuke-and-regenerate)
- Official plugin formats (hookify, learning-output-style, ralph-loop)
- Script path rewriting for ${CLAUDE_PLUGIN_ROOT} references
"""

import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.integration.hook_integrator import HookIntegrator, HookIntegrationResult
from apm_cli.models.apm_package import APMPackage, PackageInfo


def _make_package_info(install_path: Path, name: str = "test-pkg") -> PackageInfo:
    """Create a minimal PackageInfo for testing."""
    package = APMPackage(name=name, version="1.0.0")
    return PackageInfo(package=package, install_path=install_path)


# ─── Hook file fixtures mirroring official Claude plugins ─────────────────────

HOOKIFY_HOOKS_JSON = {
    "description": "Hookify plugin - User-configurable hooks from .local.md files",
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
        ],
        "PostToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/posttooluse.py",
                        "timeout": 10,
                    }
                ]
            }
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop.py",
                        "timeout": 10,
                    }
                ]
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/userpromptsubmit.py",
                        "timeout": 10,
                    }
                ]
            }
        ],
    },
}

LEARNING_OUTPUT_STYLE_HOOKS_JSON = {
    "description": "Learning mode hook that adds interactive learning instructions",
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PLUGIN_ROOT}/hooks-handlers/session-start.sh",
                    }
                ]
            }
        ]
    },
}

RALPH_LOOP_HOOKS_JSON = {
    "description": "Ralph Loop plugin stop hook for self-referential loops",
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PLUGIN_ROOT}/hooks/stop-hook.sh",
                    }
                ]
            }
        ]
    },
}


# ─── Discovery tests ─────────────────────────────────────────────────────────


class TestHookDiscovery:
    """Tests for finding hook JSON files in packages."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_find_no_hooks(self, temp_project):
        """No hooks found when package has no hook directories."""
        pkg_dir = temp_project / "pkg"
        pkg_dir.mkdir()
        integrator = HookIntegrator()
        assert integrator.find_hook_files(pkg_dir) == []

    def test_find_hooks_in_apm_hooks(self, temp_project):
        """Find hook JSON files in .apm/hooks/ directory."""
        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "security.json").write_text(json.dumps({"hooks": {}}))
        (hooks_dir / "quality.json").write_text(json.dumps({"hooks": {}}))
        (hooks_dir / "readme.md").write_text("# Not a hook")  # Should be ignored

        integrator = HookIntegrator()
        files = integrator.find_hook_files(pkg_dir)
        assert len(files) == 2
        assert all(f.suffix == ".json" for f in files)

    def test_find_hooks_in_hooks_dir(self, temp_project):
        """Find hook JSON files in hooks/ directory (Claude-native convention)."""
        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps({"hooks": {}}))

        integrator = HookIntegrator()
        files = integrator.find_hook_files(pkg_dir)
        assert len(files) == 1
        assert files[0].name == "hooks.json"

    def test_find_hooks_deduplicates(self, temp_project):
        """Do not return duplicate hook files when .apm/hooks/ and hooks/ overlap."""
        pkg_dir = temp_project / "pkg"
        # Create both directories pointing to the same conceptual hooks
        apm_hooks = pkg_dir / ".apm" / "hooks"
        apm_hooks.mkdir(parents=True, exist_ok=True)
        (apm_hooks / "a.json").write_text(json.dumps({"hooks": {}}))

        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "b.json").write_text(json.dumps({"hooks": {}}))

        integrator = HookIntegrator()
        files = integrator.find_hook_files(pkg_dir)
        assert len(files) == 2  # Different files, should both be found

    def test_should_integrate_always_true(self, temp_project):
        """Integration is always enabled (zero-config)."""
        integrator = HookIntegrator()
        assert integrator.should_integrate(temp_project)


# ─── Parsing tests ────────────────────────────────────────────────────────────


class TestHookParsing:
    """Tests for parsing hook JSON files."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_parse_valid_hook_json(self, temp_project):
        hook_file = temp_project / "hooks.json"
        hook_file.write_text(json.dumps(HOOKIFY_HOOKS_JSON))

        integrator = HookIntegrator()
        data = integrator._parse_hook_json(hook_file)
        assert data is not None
        assert "hooks" in data
        assert "PreToolUse" in data["hooks"]

    def test_parse_invalid_json(self, temp_project):
        hook_file = temp_project / "bad.json"
        hook_file.write_text("not valid json {{{")

        integrator = HookIntegrator()
        assert integrator._parse_hook_json(hook_file) is None

    def test_parse_non_dict_json(self, temp_project):
        hook_file = temp_project / "array.json"
        hook_file.write_text(json.dumps([1, 2, 3]))

        integrator = HookIntegrator()
        assert integrator._parse_hook_json(hook_file) is None

    def test_parse_missing_file(self, temp_project):
        integrator = HookIntegrator()
        assert integrator._parse_hook_json(temp_project / "missing.json") is None


# ─── VSCode integration tests ────────────────────────────────────────────────


class TestVSCodeIntegration:
    """Tests for VSCode hook integration (.github/hooks/)."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        project = Path(temp_dir)
        (project / ".github").mkdir()
        yield project
        shutil.rmtree(temp_dir, ignore_errors=True)

    def _setup_hookify_package(self, project: Path) -> PackageInfo:
        """Create a hookify-like package structure."""
        pkg_dir = project / "apm_modules" / "anthropics" / "hookify"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        (hooks_dir / "hooks.json").write_text(json.dumps(HOOKIFY_HOOKS_JSON, indent=2))

        # Create the script files
        for script in ["pretooluse.py", "posttooluse.py", "stop.py", "userpromptsubmit.py"]:
            (hooks_dir / script).write_text(f"#!/usr/bin/env python3\n# {script}")

        return _make_package_info(pkg_dir, "hookify")

    def test_integrate_hookify_vscode(self, temp_project):
        """Test VSCode integration of hookify plugin (multiple events + Python scripts)."""
        pkg_info = self._setup_hookify_package(temp_project)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)

        assert result.files_integrated == 1
        assert result.scripts_copied == 4

        # Check hook JSON was created
        target_json = temp_project / ".github" / "hooks" / "hookify-hooks.json"
        assert target_json.exists()

        # Verify rewritten paths
        data = json.loads(target_json.read_text())
        cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "${CLAUDE_PLUGIN_ROOT}" not in cmd
        assert ".github/hooks/scripts/hookify/hooks/pretooluse.py" in cmd
        assert cmd.startswith("python3 ")

        # Check scripts were copied
        scripts_dir = temp_project / ".github" / "hooks" / "scripts" / "hookify" / "hooks"
        assert (scripts_dir / "pretooluse.py").exists()
        assert (scripts_dir / "posttooluse.py").exists()
        assert (scripts_dir / "stop.py").exists()
        assert (scripts_dir / "userpromptsubmit.py").exists()

    def test_integrate_learning_output_style_vscode(self, temp_project):
        """Test VSCode integration of learning-output-style plugin (different script dir)."""
        pkg_dir = temp_project / "apm_modules" / "anthropics" / "learning-output-style"
        hooks_dir = pkg_dir / "hooks"
        handlers_dir = pkg_dir / "hooks-handlers"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        handlers_dir.mkdir(parents=True, exist_ok=True)

        (hooks_dir / "hooks.json").write_text(json.dumps(LEARNING_OUTPUT_STYLE_HOOKS_JSON))
        (handlers_dir / "session-start.sh").write_text("#!/bin/bash\necho 'start'")

        pkg_info = _make_package_info(pkg_dir, "learning-output-style")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)

        assert result.files_integrated == 1
        assert result.scripts_copied == 1

        # Verify rewritten paths
        target_json = temp_project / ".github" / "hooks" / "learning-output-style-hooks.json"
        data = json.loads(target_json.read_text())
        cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "${CLAUDE_PLUGIN_ROOT}" not in cmd
        assert "learning-output-style" in cmd
        assert "session-start.sh" in cmd

        # Check script was copied
        assert (
            temp_project
            / ".github"
            / "hooks"
            / "scripts"
            / "learning-output-style"
            / "hooks-handlers"
            / "session-start.sh"
        ).exists()

    def test_integrate_ralph_loop_vscode(self, temp_project):
        """Test VSCode integration of ralph-loop plugin (Stop hook)."""
        pkg_dir = temp_project / "apm_modules" / "anthropics" / "ralph-loop"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        (hooks_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")

        pkg_info = _make_package_info(pkg_dir, "ralph-loop")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)

        assert result.files_integrated == 1
        assert result.scripts_copied == 1

        target_json = temp_project / ".github" / "hooks" / "ralph-loop-hooks.json"
        data = json.loads(target_json.read_text())
        cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "ralph-loop" in cmd
        assert "stop-hook.sh" in cmd

    def test_integrate_no_hooks(self, temp_project):
        """Test integration with package that has no hooks."""
        pkg_dir = temp_project / "pkg"
        pkg_dir.mkdir()

        pkg_info = _make_package_info(pkg_dir)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)
        assert result.files_integrated == 0
        assert result.scripts_copied == 0

    def test_integrate_hooks_from_apm_convention(self, temp_project):
        """Test VSCode integration using .apm/hooks/ convention."""
        pkg_dir = temp_project / "apm_modules" / "myorg" / "security-hooks"
        hooks_dir = pkg_dir / ".apm" / "hooks"
        scripts_dir = pkg_dir / "scripts"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_data = {
            "hooks": {
                "PreToolUse": [
                    {
                        "hooks": [
                            {"type": "command", "command": "./scripts/validate.sh"}
                        ]
                    }
                ]
            }
        }
        (hooks_dir / "security.json").write_text(json.dumps(hook_data))
        (scripts_dir / "validate.sh").write_text("#!/bin/bash\necho 'validate'")

        pkg_info = _make_package_info(pkg_dir, "security-hooks")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)

        assert result.files_integrated == 1
        target_json = temp_project / ".github" / "hooks" / "security-hooks-security.json"
        assert target_json.exists()

    def test_integrate_system_command_passthrough(self, temp_project):
        """Test that system commands without file paths are passed through unchanged."""
        pkg_dir = temp_project / "apm_modules" / "myorg" / "format-pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        hook_data = {
            "hooks": {
                "PreToolUse": [
                    {
                        "hooks": [
                            {"type": "command", "command": "npx prettier --check ."}
                        ]
                    }
                ]
            }
        }
        (hooks_dir / "format.json").write_text(json.dumps(hook_data))

        pkg_info = _make_package_info(pkg_dir, "format-pkg")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)

        assert result.files_integrated == 1
        assert result.scripts_copied == 0  # No scripts to copy for system commands

        target_json = temp_project / ".github" / "hooks" / "format-pkg-format.json"
        data = json.loads(target_json.read_text())
        cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert cmd == "npx prettier --check ."

    def test_invalid_json_skipped(self, temp_project):
        """Test that invalid JSON hook files are skipped gracefully."""
        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "bad.json").write_text("not json")

        pkg_info = _make_package_info(pkg_dir)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)
        assert result.files_integrated == 0

    def test_creates_github_hooks_dir(self, temp_project):
        """Test that .github/hooks/ directory is created if it doesn't exist."""
        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps({"hooks": {"Stop": []}}))

        pkg_info = _make_package_info(pkg_dir)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)
        assert (temp_project / ".github" / "hooks").exists()


# ─── Claude integration tests ────────────────────────────────────────────────


class TestClaudeIntegration:
    """Tests for Claude hook integration (.claude/settings.json merge)."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        project = Path(temp_dir)
        (project / ".claude").mkdir()
        yield project
        shutil.rmtree(temp_dir, ignore_errors=True)

    def _setup_hookify_package(self, project: Path) -> PackageInfo:
        """Create a hookify-like package structure."""
        pkg_dir = project / "apm_modules" / "anthropics" / "hookify"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        (hooks_dir / "hooks.json").write_text(json.dumps(HOOKIFY_HOOKS_JSON, indent=2))

        for script in ["pretooluse.py", "posttooluse.py", "stop.py", "userpromptsubmit.py"]:
            (hooks_dir / script).write_text(f"#!/usr/bin/env python3\n# {script}")

        return _make_package_info(pkg_dir, "hookify")

    def test_integrate_hookify_claude(self, temp_project):
        """Test Claude integration of hookify plugin (merge into settings.json)."""
        pkg_info = self._setup_hookify_package(temp_project)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        assert result.files_integrated == 1
        assert result.scripts_copied == 4

        # Check settings.json was created/updated
        settings_path = temp_project / ".claude" / "settings.json"
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings
        assert "PreToolUse" in settings["hooks"]
        assert "PostToolUse" in settings["hooks"]
        assert "Stop" in settings["hooks"]
        assert "UserPromptSubmit" in settings["hooks"]

        # Check APM source marker for cleanup
        assert settings["hooks"]["PreToolUse"][0]["_apm_source"] == "hookify"

        # Verify rewritten paths
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert ".claude/hooks/hookify/hooks/pretooluse.py" in cmd

    def test_integrate_learning_output_style_claude(self, temp_project):
        """Test Claude integration of learning-output-style plugin."""
        pkg_dir = temp_project / "apm_modules" / "anthropics" / "learning-output-style"
        hooks_dir = pkg_dir / "hooks"
        handlers_dir = pkg_dir / "hooks-handlers"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        handlers_dir.mkdir(parents=True, exist_ok=True)

        (hooks_dir / "hooks.json").write_text(json.dumps(LEARNING_OUTPUT_STYLE_HOOKS_JSON))
        (handlers_dir / "session-start.sh").write_text("#!/bin/bash\necho 'start'")

        pkg_info = _make_package_info(pkg_dir, "learning-output-style")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        assert result.files_integrated == 1
        settings = json.loads((temp_project / ".claude" / "settings.json").read_text())
        assert "SessionStart" in settings["hooks"]

    def test_integrate_ralph_loop_claude(self, temp_project):
        """Test Claude integration of ralph-loop plugin."""
        pkg_dir = temp_project / "apm_modules" / "anthropics" / "ralph-loop"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        (hooks_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")

        pkg_info = _make_package_info(pkg_dir, "ralph-loop")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        assert result.files_integrated == 1
        settings = json.loads((temp_project / ".claude" / "settings.json").read_text())
        assert "Stop" in settings["hooks"]
        cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "ralph-loop" in cmd

    def test_merge_into_existing_settings(self, temp_project):
        """Test that hooks are merged into existing settings.json without clobbering."""
        settings_path = temp_project / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({
            "model": "claude-sonnet-4-20250514",
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": "echo user-hook"}]}]
            }
        }))

        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")

        pkg_info = _make_package_info(pkg_dir, "ralph-loop")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        settings = json.loads(settings_path.read_text())
        # Original settings preserved
        assert settings["model"] == "claude-sonnet-4-20250514"
        # User hook preserved
        assert len(settings["hooks"]["PreToolUse"]) == 1
        # New hook added
        assert "Stop" in settings["hooks"]

    def test_additive_merge_same_event(self, temp_project):
        """Test that multiple packages can add hooks to the same event (additive)."""
        integrator = HookIntegrator()

        # First package: ralph-loop with Stop hook
        pkg1_dir = temp_project / "pkg1"
        hooks1_dir = pkg1_dir / "hooks"
        hooks1_dir.mkdir(parents=True, exist_ok=True)
        (hooks1_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks1_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")
        pkg1_info = _make_package_info(pkg1_dir, "ralph-loop")

        integrator.integrate_package_hooks_claude(pkg1_info, temp_project)

        # Second package: also has Stop hook
        pkg2_dir = temp_project / "pkg2"
        hooks2_dir = pkg2_dir / "hooks"
        hooks2_dir.mkdir(parents=True, exist_ok=True)
        other_hooks = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "echo other-stop"}]}]
            }
        }
        (hooks2_dir / "hooks.json").write_text(json.dumps(other_hooks))
        pkg2_info = _make_package_info(pkg2_dir, "other-pkg")

        integrator.integrate_package_hooks_claude(pkg2_info, temp_project)

        settings = json.loads((temp_project / ".claude" / "settings.json").read_text())
        # Both Stop hooks should be present (additive)
        assert len(settings["hooks"]["Stop"]) == 2

    def test_reinstall_is_idempotent(self, temp_project):
        """Re-running integration for the same package must not duplicate its entries.

        Regression test for microsoft/apm#708: before the fix, each subsequent
        `apm install` appended another copy of every hook owned by an
        already-integrated package.
        """
        # `_get_package_name` derives the package name from install_path.name,
        # so the directory name is what ends up as `_apm_source`.
        pkg_dir = temp_project / "ralph-loop"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")
        pkg_info = _make_package_info(pkg_dir, "ralph-loop")
        integrator = HookIntegrator()

        integrator.integrate_package_hooks_claude(pkg_info, temp_project)
        first = (temp_project / ".claude" / "settings.json").read_text()

        # Re-run twice more — the file should be byte-identical each time.
        for _ in range(2):
            integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        settings = json.loads((temp_project / ".claude" / "settings.json").read_text())
        assert len(settings["hooks"]["Stop"]) == 1
        assert settings["hooks"]["Stop"][0]["_apm_source"] == "ralph-loop"
        assert (temp_project / ".claude" / "settings.json").read_text() == first

    def test_reinstall_heals_preexisting_duplicates(self, temp_project):
        """Existing duplicate entries for a package get collapsed on re-integration.

        Upgrades from a pre-#708 apm can leave a settings.json with multiple
        identical `_apm_source` entries; the next install should clean them up.
        """
        pkg_dir = temp_project / "ralph-loop"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")
        pkg_info = _make_package_info(pkg_dir, "ralph-loop")
        integrator = HookIntegrator()

        # Seed a settings.json with three duplicate ralph-loop Stop entries
        # plus one unrelated user hook that must survive.
        dup_entry = {
            "matcher": "",
            "hooks": [{"type": "command", "command": "stale"}],
            "_apm_source": "ralph-loop",
        }
        settings_path = temp_project / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "user-owned"}]},
                    dup_entry,
                    dup_entry,
                    dup_entry,
                ]
            }
        }))

        integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        settings = json.loads(settings_path.read_text())
        apm_entries = [
            e for e in settings["hooks"]["Stop"]
            if isinstance(e, dict) and e.get("_apm_source") == "ralph-loop"
        ]
        user_entries = [
            e for e in settings["hooks"]["Stop"]
            if not (isinstance(e, dict) and "_apm_source" in e)
        ]
        assert len(apm_entries) == 1
        # Stale command replaced with the freshly rewritten one.
        assert "stop-hook.sh" in apm_entries[0]["hooks"][0]["command"]
        assert len(user_entries) == 1
        assert user_entries[0]["hooks"][0]["command"] == "user-owned"

    def test_reinstall_preserves_multiple_hook_files_same_event(self, temp_project):
        """A package can contribute to one event from several hook files.

        The idempotent-upsert must only strip prior-owned entries once per
        event per install run — otherwise the second hook file's iteration
        wipes the first file's fresh entries before extending. Also verifies
        the combined output is stable across re-runs.
        """
        pkg_dir = temp_project / "multi-stop-pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        def stop_hook(script: str) -> dict:
            return {"hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": f"${{CLAUDE_PLUGIN_ROOT}}/hooks/{script}"}
            ]}]}}

        (hooks_dir / "hooks-a.json").write_text(json.dumps(stop_hook("stop-a.sh")))
        (hooks_dir / "hooks-b.json").write_text(json.dumps(stop_hook("stop-b.sh")))
        (hooks_dir / "stop-a.sh").write_text("#!/bin/bash\nexit 0")
        (hooks_dir / "stop-b.sh").write_text("#!/bin/bash\nexit 0")

        pkg_info = _make_package_info(pkg_dir, "multi-stop-pkg")
        integrator = HookIntegrator()
        integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        settings_path = temp_project / ".claude" / "settings.json"
        first = settings_path.read_text()

        def extract_commands(text: str) -> set:
            stop = json.loads(text)["hooks"]["Stop"]
            assert all(e["_apm_source"] == "multi-stop-pkg" for e in stop)
            return {h["command"] for entry in stop for h in entry["hooks"]}

        assert extract_commands(first) == {
            ".claude/hooks/multi-stop-pkg/hooks/stop-a.sh",
            ".claude/hooks/multi-stop-pkg/hooks/stop-b.sh",
        }

        # Re-run twice more — both entries must survive and the file must
        # be byte-identical each time (idempotent across hook files too).
        for _ in range(2):
            integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        assert settings_path.read_text() == first

    def test_no_hooks_returns_empty_result(self, temp_project):
        """Test Claude integration with no hook files returns empty result."""
        pkg_dir = temp_project / "pkg"
        pkg_dir.mkdir()

        pkg_info = _make_package_info(pkg_dir)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_claude(pkg_info, temp_project)
        assert result.files_integrated == 0

    def test_creates_settings_json(self, temp_project):
        """Test that .claude/settings.json is created if it doesn't exist."""
        # Remove existing .claude dir
        shutil.rmtree(temp_project / ".claude")

        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")

        pkg_info = _make_package_info(pkg_dir, "ralph-loop")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_claude(pkg_info, temp_project)
        assert result.files_integrated == 1
        assert (temp_project / ".claude" / "settings.json").exists()

    def test_integrate_hooks_with_scripts_in_hooks_subdir_claude(self, temp_project):
        """Test Claude integration when hook JSON and scripts are both inside hooks/ subdir."""
        pkg_dir = temp_project / "apm_modules" / "myorg" / "lint-hooks"
        hooks_dir = pkg_dir / "hooks"
        scripts_dir = hooks_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_data = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": {"tool_name": "write_to_file"},
                        "hooks": [
                            {"type": "command", "command": "./scripts/lint.sh", "timeout": 10}
                        ]
                    }
                ]
            }
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        (scripts_dir / "lint.sh").write_text("#!/bin/bash\necho lint")

        pkg_info = _make_package_info(pkg_dir, "lint-hooks")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_claude(pkg_info, temp_project)

        assert result.files_integrated == 1
        assert result.scripts_copied == 1

        # Verify rewritten command in settings.json
        settings = json.loads((temp_project / ".claude" / "settings.json").read_text())
        cmd = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert ".claude/hooks/lint-hooks/scripts/lint.sh" in cmd
        assert "./" not in cmd

        # Verify script was copied to Claude target location
        copied_script = temp_project / ".claude" / "hooks" / "lint-hooks" / "scripts" / "lint.sh"
        assert copied_script.exists()
        assert copied_script.read_text() == "#!/bin/bash\necho lint"


# ─── Cursor integration tests ────────────────────────────────────────────────


class TestCursorIntegration:
    """Tests for Cursor hook integration (.cursor/hooks.json merge)."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        project = Path(temp_dir)
        (project / ".cursor").mkdir()
        yield project
        shutil.rmtree(temp_dir, ignore_errors=True)

    def _setup_hookify_package(self, project: Path) -> PackageInfo:
        """Create a hookify-like package structure."""
        pkg_dir = project / "apm_modules" / "anthropics" / "hookify"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        (hooks_dir / "hooks.json").write_text(json.dumps(HOOKIFY_HOOKS_JSON, indent=2))

        for script in ["pretooluse.py", "posttooluse.py", "stop.py", "userpromptsubmit.py"]:
            (hooks_dir / script).write_text(f"#!/usr/bin/env python3\n# {script}")

        return _make_package_info(pkg_dir, "hookify")

    def test_integrate_hookify_cursor(self, temp_project):
        """Test Cursor integration of hookify plugin (merge into hooks.json)."""
        pkg_info = self._setup_hookify_package(temp_project)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_cursor(pkg_info, temp_project)

        assert result.files_integrated == 1
        assert result.scripts_copied == 4

        # Check hooks.json was created/updated
        hooks_path = temp_project / ".cursor" / "hooks.json"
        assert hooks_path.exists()

        config = json.loads(hooks_path.read_text())
        assert "hooks" in config
        assert "PreToolUse" in config["hooks"]
        assert "PostToolUse" in config["hooks"]
        assert "Stop" in config["hooks"]
        assert "UserPromptSubmit" in config["hooks"]

        # Check APM source marker for cleanup
        assert config["hooks"]["PreToolUse"][0]["_apm_source"] == "hookify"

        # Verify rewritten paths point to .cursor/hooks/
        cmd = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert ".cursor/hooks/hookify/hooks/pretooluse.py" in cmd

    def test_skips_when_no_cursor_dir(self, temp_project):
        """Test that Cursor integration is skipped when .cursor/ doesn't exist."""
        # Remove .cursor/ directory
        shutil.rmtree(temp_project / ".cursor")

        pkg_info = self._setup_hookify_package(temp_project)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_cursor(pkg_info, temp_project)

        assert result.files_integrated == 0
        assert result.scripts_copied == 0
        assert not (temp_project / ".cursor" / "hooks.json").exists()

    def test_merge_into_existing_hooks_json(self, temp_project):
        """Test that hooks are merged into existing hooks.json without clobbering."""
        hooks_path = temp_project / ".cursor" / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {
                "afterFileEdit": [{"command": "echo user-hook"}]
            }
        }))

        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")

        pkg_info = _make_package_info(pkg_dir, "ralph-loop")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_cursor(pkg_info, temp_project)

        config = json.loads(hooks_path.read_text())
        # User hook preserved
        assert len(config["hooks"]["afterFileEdit"]) == 1
        assert config["hooks"]["afterFileEdit"][0]["command"] == "echo user-hook"
        # New hook added
        assert "Stop" in config["hooks"]
        assert config["hooks"]["Stop"][0]["_apm_source"] == "pkg"

    def test_additive_merge_same_event(self, temp_project):
        """Test that multiple packages can add hooks to the same event."""
        integrator = HookIntegrator()

        # First package
        pkg1_dir = temp_project / "apm_modules" / "ralph-loop"
        hooks1_dir = pkg1_dir / "hooks"
        hooks1_dir.mkdir(parents=True, exist_ok=True)
        (hooks1_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks1_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")
        pkg1_info = _make_package_info(pkg1_dir, "ralph-loop")

        integrator.integrate_package_hooks_cursor(pkg1_info, temp_project)

        # Second package with same event
        pkg2_dir = temp_project / "apm_modules" / "other-pkg"
        hooks2_dir = pkg2_dir / "hooks"
        hooks2_dir.mkdir(parents=True, exist_ok=True)
        (hooks2_dir / "hooks.json").write_text(json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "echo other-stop"}]}]
            }
        }))
        pkg2_info = _make_package_info(pkg2_dir, "other-pkg")

        integrator.integrate_package_hooks_cursor(pkg2_info, temp_project)

        config = json.loads((temp_project / ".cursor" / "hooks.json").read_text())
        # Both entries present under Stop
        assert len(config["hooks"]["Stop"]) == 2
        assert config["hooks"]["Stop"][0]["_apm_source"] == "ralph-loop"
        assert config["hooks"]["Stop"][1]["_apm_source"] == "other-pkg"

    def test_scripts_copied_to_cursor_hooks_dir(self, temp_project):
        """Test that scripts are copied to .cursor/hooks/<pkg>/."""
        pkg_info = self._setup_hookify_package(temp_project)
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks_cursor(pkg_info, temp_project)

        # Verify scripts exist under .cursor/hooks/hookify/
        scripts_dir = temp_project / ".cursor" / "hooks" / "hookify" / "hooks"
        assert scripts_dir.exists()
        assert (scripts_dir / "pretooluse.py").exists()
        assert (scripts_dir / "posttooluse.py").exists()
        assert (scripts_dir / "stop.py").exists()
        assert (scripts_dir / "userpromptsubmit.py").exists()

    def test_sync_removes_cursor_hook_entries(self, temp_project):
        """Test that sync removes APM-managed entries from .cursor/hooks.json."""
        hooks_path = temp_project / ".cursor" / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {
                "Stop": [
                    {"_apm_source": "ralph-loop", "hooks": [{"type": "command", "command": "..."}]},
                    {"command": "echo user-hook"},
                ],
                "PreToolUse": [
                    {"_apm_source": "hookify", "hooks": [{"type": "command", "command": "..."}]}
                ],
            }
        }))

        integrator = HookIntegrator()
        stats = integrator.sync_integration(None, temp_project)

        updated = json.loads(hooks_path.read_text())
        # APM entries removed, user entries preserved
        assert "Stop" in updated["hooks"]
        assert len(updated["hooks"]["Stop"]) == 1
        assert "_apm_source" not in updated["hooks"]["Stop"][0]
        # PreToolUse completely removed (only had APM entries)
        assert "PreToolUse" not in updated["hooks"]

    def test_sync_removes_cursor_hooks_scripts(self, temp_project):
        """Test that sync removes .cursor/hooks/ scripts via manifest mode."""
        cursor_hooks = temp_project / ".cursor" / "hooks" / "hookify"
        cursor_hooks.mkdir(parents=True, exist_ok=True)
        (cursor_hooks / "pretooluse.py").write_text("# script")

        integrator = HookIntegrator()
        managed_files = {".cursor/hooks/hookify/pretooluse.py"}
        stats = integrator.sync_integration(None, temp_project, managed_files=managed_files)

        assert stats["files_removed"] == 1
        assert not (temp_project / ".cursor" / "hooks").exists()

    def test_sync_removes_empty_hooks_key_cursor(self, temp_project):
        """Test that empty hooks key is removed from hooks.json after cleanup."""
        hooks_path = temp_project / ".cursor" / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {
                "Stop": [{"_apm_source": "test", "hooks": []}]
            }
        }))

        integrator = HookIntegrator()
        integrator.sync_integration(None, temp_project)

        updated = json.loads(hooks_path.read_text())
        assert "hooks" not in updated


# ─── Sync/cleanup tests ──────────────────────────────────────────────────────


class TestSyncIntegration:
    """Tests for sync_integration (nuke-and-regenerate during uninstall)."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_sync_removes_vscode_hook_files(self, temp_project):
        """Test that sync removes all *-apm.json files from .github/hooks/."""
        hooks_dir = temp_project / ".github" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        (hooks_dir / "hookify-hooks-apm.json").write_text("{}")
        (hooks_dir / "ralph-loop-hooks-apm.json").write_text("{}")
        (hooks_dir / "user-custom.json").write_text("{}")  # Should NOT be removed

        integrator = HookIntegrator()
        stats = integrator.sync_integration(None, temp_project)

        assert stats["files_removed"] == 2
        assert not (hooks_dir / "hookify-hooks-apm.json").exists()
        assert not (hooks_dir / "ralph-loop-hooks-apm.json").exists()
        assert (hooks_dir / "user-custom.json").exists()

    def test_sync_removes_scripts_directory(self, temp_project):
        """Test that sync removes scripts via manifest mode and cleans empty parents."""
        hooks_dir = temp_project / ".github" / "hooks"
        scripts_dir = hooks_dir / "scripts" / "hookify" / "hooks"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "pretooluse.py").write_text("# script")

        integrator = HookIntegrator()
        managed_files = {".github/hooks/scripts/hookify/hooks/pretooluse.py"}
        stats = integrator.sync_integration(None, temp_project, managed_files=managed_files)

        assert stats["files_removed"] == 1
        assert not (hooks_dir / "scripts").exists()

    def test_sync_removes_claude_hook_entries(self, temp_project):
        """Test that sync removes APM-managed entries from .claude/settings.json."""
        claude_dir = temp_project / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"

        settings = {
            "model": "claude-sonnet-4-20250514",
            "hooks": {
                "Stop": [
                    {"_apm_source": "ralph-loop", "hooks": [{"type": "command", "command": "..."}]},
                    {"hooks": [{"type": "command", "command": "echo user-hook"}]},
                ],
                "PreToolUse": [
                    {"_apm_source": "hookify", "hooks": [{"type": "command", "command": "..."}]}
                ],
            },
        }
        settings_path.write_text(json.dumps(settings))

        integrator = HookIntegrator()
        stats = integrator.sync_integration(None, temp_project)

        updated_settings = json.loads(settings_path.read_text())
        # Model preserved
        assert updated_settings["model"] == "claude-sonnet-4-20250514"
        # APM entries removed, user entries preserved
        assert "Stop" in updated_settings["hooks"]
        assert len(updated_settings["hooks"]["Stop"]) == 1
        assert "_apm_source" not in updated_settings["hooks"]["Stop"][0]
        # PreToolUse completely removed (only had APM entries)
        assert "PreToolUse" not in updated_settings["hooks"]

    def test_sync_removes_claude_hooks_dir(self, temp_project):
        """Test that sync removes .claude/hooks/ scripts via manifest mode and cleans empty parents."""
        claude_hooks = temp_project / ".claude" / "hooks" / "hookify"
        claude_hooks.mkdir(parents=True, exist_ok=True)
        (claude_hooks / "pretooluse.py").write_text("# script")

        integrator = HookIntegrator()
        managed_files = {".claude/hooks/hookify/pretooluse.py"}
        stats = integrator.sync_integration(None, temp_project, managed_files=managed_files)

        assert stats["files_removed"] == 1
        assert not (temp_project / ".claude" / "hooks").exists()

    def test_sync_empty_project(self, temp_project):
        """Test sync on project with no hook artifacts."""
        integrator = HookIntegrator()
        stats = integrator.sync_integration(None, temp_project)
        assert stats["files_removed"] == 0
        assert stats["errors"] == 0

    def test_sync_removes_empty_hooks_key(self, temp_project):
        """Test that empty hooks key is removed from settings.json after cleanup."""
        claude_dir = temp_project / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        settings = {
            "hooks": {
                "Stop": [{"_apm_source": "test", "hooks": []}]
            }
        }
        settings_path.write_text(json.dumps(settings))

        integrator = HookIntegrator()
        integrator.sync_integration(None, temp_project)

        updated = json.loads(settings_path.read_text())
        assert "hooks" not in updated  # Completely removed when empty


# ─── Script path rewriting tests ─────────────────────────────────────────────


class TestScriptPathRewriting:
    """Tests for command path rewriting logic."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_rewrite_claude_plugin_root(self, temp_project):
        """Test rewriting ${CLAUDE_PLUGIN_ROOT} variable."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "hooks").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "hooks" / "script.sh").write_text("#!/bin/bash")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/script.sh",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "${CLAUDE_PLUGIN_ROOT}" not in cmd
        assert ".github/hooks/scripts/my-pkg/hooks/script.sh" in cmd
        assert len(scripts) == 1

    def test_rewrite_relative_path(self, temp_project):
        """Test rewriting relative ./path references."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "check.sh").write_text("#!/bin/bash")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/check.sh",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "./" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/check.sh" in cmd
        assert len(scripts) == 1

    def test_system_command_unchanged(self, temp_project):
        """Test that system commands are not modified."""
        pkg_dir = temp_project / "pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "npx prettier --check .",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert cmd == "npx prettier --check ."
        assert len(scripts) == 0

    def test_rewrite_for_claude_target(self, temp_project):
        """Test that Claude target uses .claude/hooks/ path."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "hooks").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "hooks" / "run.sh").write_text("#!/bin/bash")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh",
            pkg_dir,
            "my-pkg",
            "claude",
        )

        assert ".claude/hooks/my-pkg/hooks/run.sh" in cmd
        assert len(scripts) == 1

    def test_nonexistent_script_not_rewritten(self, temp_project):
        """Test that references to non-existent scripts are left as-is."""
        pkg_dir = temp_project / "pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "${CLAUDE_PLUGIN_ROOT}/missing/script.sh",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        # Variable is left in the command since the file doesn't exist
        assert "${CLAUDE_PLUGIN_ROOT}" in cmd
        assert len(scripts) == 0

    def test_rewrite_preserves_binary_prefix(self, temp_project):
        """Test that binary prefix (e.g., python3) is preserved in rewritten commands."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "hooks").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "hooks" / "check.py").write_text("#!/usr/bin/env python3")

        integrator = HookIntegrator()
        cmd, _ = integrator._rewrite_command_for_target(
            "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/check.py",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert cmd.startswith("python3 ")
        assert cmd.endswith("hooks/check.py")

    def test_rewrite_relative_path_with_hook_file_dir(self, temp_project):
        """Test that ./path is resolved from hook_file_dir, not package root."""
        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        scripts_dir = hooks_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "lint.sh").write_text("#!/bin/bash")

        integrator = HookIntegrator()
        # Script lives at hooks/scripts/lint.sh — only resolvable from hooks/ dir
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/lint.sh",
            pkg_dir,
            "my-pkg",
            "vscode",
            hook_file_dir=hooks_dir,
        )

        assert "./" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/lint.sh" in cmd
        assert len(scripts) == 1
        assert scripts[0][0] == (scripts_dir / "lint.sh").resolve()

    def test_rewrite_relative_path_fails_without_hook_file_dir(self, temp_project):
        """Test that ./path is NOT found when resolved from package root (no hook_file_dir)."""
        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        scripts_dir = hooks_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "lint.sh").write_text("#!/bin/bash")

        integrator = HookIntegrator()
        # Without hook_file_dir, resolves from pkg_dir — scripts/lint.sh doesn't exist there
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/lint.sh",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        # Script not found at pkg_dir/scripts/lint.sh, so left unchanged
        assert cmd == "./scripts/lint.sh"
        assert len(scripts) == 0

    def test_rewrite_rejects_plugin_root_path_traversal(self, temp_project):
        """Test that ${CLAUDE_PLUGIN_ROOT}/../ paths are rejected (path traversal)."""
        pkg_dir = temp_project / "pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        # Create a file outside the package directory
        secret = temp_project / "secrets.txt"
        secret.write_text("top-secret")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "cat ${CLAUDE_PLUGIN_ROOT}/../secrets.txt",
            pkg_dir,
            "evil-pkg",
            "vscode",
        )

        # The traversal path should NOT be rewritten and no scripts copied
        assert "${CLAUDE_PLUGIN_ROOT}/../secrets.txt" in cmd
        assert len(scripts) == 0

    def test_rewrite_rejects_relative_path_traversal(self, temp_project):
        """Test that ./../../ paths are rejected (path traversal via relative refs)."""
        pkg_dir = temp_project / "pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        # Create a file outside the package directory
        secret = temp_project / "secrets.txt"
        secret.write_text("top-secret")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./../../secrets.txt",
            pkg_dir,
            "evil-pkg",
            "claude",
            hook_file_dir=hooks_dir,
        )

        # The traversal path should NOT be rewritten and no scripts copied
        assert cmd == "./../../secrets.txt"
        assert len(scripts) == 0

    def test_rewrite_bash_key(self, temp_project):
        """Test rewriting the bash key (GitHub Copilot format)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "check.sh").write_text("#!/bin/bash")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/check.sh",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "./" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/check.sh" in cmd
        assert len(scripts) == 1

    def test_rewrite_powershell_key(self, temp_project):
        """Test rewriting the powershell key (GitHub Copilot format)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "check.ps1").write_text("Write-Host 'ok'")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/check.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "./" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/check.ps1" in cmd
        assert len(scripts) == 1

    def test_rewrite_windows_key(self, temp_project):
        """Test rewriting the windows key (GitHub Copilot format)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "scan-secrets.ps1").write_text("Write-Host 'scanning'")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/scan-secrets.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "./" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/scan-secrets.ps1" in cmd
        assert len(scripts) == 1

    def test_rewrite_linux_key(self, temp_project):
        """Test rewriting the linux key (VS Code OS-specific override)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "validate.sh").write_text("#!/bin/bash")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/validate.sh",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "./" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/validate.sh" in cmd
        assert len(scripts) == 1

    def test_rewrite_osx_key(self, temp_project):
        """Test rewriting the osx key (VS Code OS-specific override)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "format-mac.sh").write_text("#!/bin/bash")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/format-mac.sh",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "./" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/format-mac.sh" in cmd
        assert len(scripts) == 1

    # -- Windows backslash path tests ------------------------------------------

    def test_rewrite_backslash_relative_path(self, temp_project):
        """Test rewriting .\\ backslash relative path (Windows convention)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "scan.ps1").write_text("Write-Host 'ok'")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            ".\\scripts\\scan.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert ".\\" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/scan.ps1" in cmd
        assert len(scripts) == 1

    def test_rewrite_backslash_with_command_prefix(self, temp_project):
        """Test .\\ path preceded by command text (e.g. pwsh -File .\\scan.ps1)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "scan.ps1").write_text("Write-Host 'ok'")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "pwsh -File .\\scripts\\scan.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert cmd.startswith("pwsh -File ")
        assert ".\\" not in cmd
        assert ".github/hooks/scripts/my-pkg/scripts/scan.ps1" in cmd
        assert len(scripts) == 1

    def test_rewrite_backslash_plugin_root(self, temp_project):
        """Test ${CLAUDE_PLUGIN_ROOT} with backslash separators."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "hooks").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "hooks" / "validate.ps1").write_text("Write-Host 'ok'")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "pwsh ${CLAUDE_PLUGIN_ROOT}\\hooks\\validate.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "${CLAUDE_PLUGIN_ROOT}" not in cmd
        assert "\\" not in cmd
        assert cmd.startswith("pwsh ")
        assert ".github/hooks/scripts/my-pkg/hooks/validate.ps1" in cmd
        assert len(scripts) == 1

    def test_rewrite_backslash_normalizes_to_forward_slash(self, temp_project):
        """Output paths always use forward slashes regardless of input."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "sub" / "dir").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "sub" / "dir" / "run.ps1").write_text("Write-Host 'ok'")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            ".\\sub\\dir\\run.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert "\\" not in cmd
        assert ".github/hooks/scripts/my-pkg/sub/dir/run.ps1" in cmd
        # Target path in scripts_to_copy also uses forward slashes
        assert all("\\" not in target_rel for _, target_rel in scripts)

    def test_rewrite_backslash_path_traversal_rejected(self, temp_project):
        """Backslash path traversal (..\\) is still rejected."""
        pkg_dir = temp_project / "pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        # Create file outside package dir
        (temp_project / "secret.ps1").write_text("bad")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            ".\\..\\secret.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        # Path traversal should be rejected — command unchanged, no scripts
        assert len(scripts) == 0

    def test_rewrite_hooks_data_windows_backslash_flat(self, temp_project):
        """Test _rewrite_hooks_data handles backslash paths in windows key."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "validate.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "validate.ps1").write_text("Write-Host 'ok'")

        integrator = HookIntegrator()
        data = {
            "hooks": {
                "preToolUse": [
                    {
                        "type": "command",
                        "bash": "./scripts/validate.sh",
                        "windows": ".\\scripts\\validate.ps1",
                    }
                ]
            }
        }
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["preToolUse"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.sh" in hook["bash"]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.ps1" in hook["windows"]
        assert "\\" not in hook["windows"]
        assert len(scripts) == 2

    def test_rewrite_hooks_data_windows_flat_format(self, temp_project):
        """Test _rewrite_hooks_data handles windows key in flat format (GitHub Copilot)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "validate.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "validate.ps1").write_text("Write-Host 'ok'")

        data = {
            "version": 1,
            "hooks": {
                "preToolUse": [
                    {
                        "type": "command",
                        "bash": "./scripts/validate.sh",
                        "windows": "./scripts/validate.ps1",
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["preToolUse"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.sh" in hook["bash"]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.ps1" in hook["windows"]
        assert len(scripts) == 2

    def test_rewrite_hooks_data_windows_nested_format(self, temp_project):
        """Test _rewrite_hooks_data handles windows key in nested format (Claude-style)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "validate.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "validate.ps1").write_text("Write-Host 'ok'")

        data = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "./scripts/validate.sh",
                                "windows": "./scripts/validate.ps1",
                            }
                        ]
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["PreToolUse"][0]["hooks"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.sh" in hook["command"]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.ps1" in hook["windows"]
        assert len(scripts) == 2

    def test_rewrite_hooks_data_linux_flat_format(self, temp_project):
        """Test _rewrite_hooks_data handles linux key in flat format (VS Code)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "format.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "format-linux.sh").write_text("#!/bin/bash")

        data = {
            "hooks": {
                "PostToolUse": [
                    {
                        "type": "command",
                        "command": "./scripts/format.sh",
                        "linux": "./scripts/format-linux.sh",
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["PostToolUse"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/format.sh" in hook["command"]
        assert ".github/hooks/scripts/my-pkg/scripts/format-linux.sh" in hook["linux"]
        assert len(scripts) == 2

    def test_rewrite_hooks_data_linux_nested_format(self, temp_project):
        """Test _rewrite_hooks_data handles linux key in nested format (Claude-style)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "validate.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "validate-linux.sh").write_text("#!/bin/bash")

        data = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "./scripts/validate.sh",
                                "linux": "./scripts/validate-linux.sh",
                            }
                        ]
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["PreToolUse"][0]["hooks"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.sh" in hook["command"]
        assert ".github/hooks/scripts/my-pkg/scripts/validate-linux.sh" in hook["linux"]
        assert len(scripts) == 2

    def test_rewrite_hooks_data_osx_flat_format(self, temp_project):
        """Test _rewrite_hooks_data handles osx key in flat format (VS Code)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "format.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "format-mac.sh").write_text("#!/bin/bash")

        data = {
            "hooks": {
                "PostToolUse": [
                    {
                        "type": "command",
                        "command": "./scripts/format.sh",
                        "osx": "./scripts/format-mac.sh",
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["PostToolUse"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/format.sh" in hook["command"]
        assert ".github/hooks/scripts/my-pkg/scripts/format-mac.sh" in hook["osx"]
        assert len(scripts) == 2

    def test_rewrite_hooks_data_osx_nested_format(self, temp_project):
        """Test _rewrite_hooks_data handles osx key in nested format (Claude-style)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "validate.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "validate-mac.sh").write_text("#!/bin/bash")

        data = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "./scripts/validate.sh",
                                "osx": "./scripts/validate-mac.sh",
                            }
                        ]
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["PreToolUse"][0]["hooks"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.sh" in hook["command"]
        assert ".github/hooks/scripts/my-pkg/scripts/validate-mac.sh" in hook["osx"]
        assert len(scripts) == 2

    def test_rewrite_hooks_data_all_platform_keys(self, temp_project):
        """Test _rewrite_hooks_data handles all 6 platform keys together."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "run.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "run.ps1").write_text("Write-Host 'ok'")
        (pkg_dir / "scripts" / "run-win.ps1").write_text("Write-Host 'win'")
        (pkg_dir / "scripts" / "run-linux.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "run-mac.sh").write_text("#!/bin/bash")

        data = {
            "version": 1,
            "hooks": {
                "preToolUse": [
                    {
                        "type": "command",
                        "command": "./scripts/run.sh",
                        "bash": "./scripts/run.sh",
                        "powershell": "./scripts/run.ps1",
                        "windows": "./scripts/run-win.ps1",
                        "linux": "./scripts/run-linux.sh",
                        "osx": "./scripts/run-mac.sh",
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["preToolUse"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/run.sh" in hook["command"]
        assert ".github/hooks/scripts/my-pkg/scripts/run.sh" in hook["bash"]
        assert ".github/hooks/scripts/my-pkg/scripts/run.ps1" in hook["powershell"]
        assert ".github/hooks/scripts/my-pkg/scripts/run-win.ps1" in hook["windows"]
        assert ".github/hooks/scripts/my-pkg/scripts/run-linux.sh" in hook["linux"]
        assert ".github/hooks/scripts/my-pkg/scripts/run-mac.sh" in hook["osx"]
        # Scripts are de-duplicated by target path. command and bash both
        # reference run.sh with the same target, so only 5 unique entries.
        assert len(scripts) == 5
        script_targets = [t for _, t in scripts]
        assert script_targets.count(".github/hooks/scripts/my-pkg/scripts/run.sh") == 1
        assert script_targets.count(".github/hooks/scripts/my-pkg/scripts/run.ps1") == 1
        assert script_targets.count(".github/hooks/scripts/my-pkg/scripts/run-win.ps1") == 1
        assert script_targets.count(".github/hooks/scripts/my-pkg/scripts/run-linux.sh") == 1
        assert script_targets.count(".github/hooks/scripts/my-pkg/scripts/run-mac.sh") == 1

    def test_rewrite_hooks_data_github_copilot_flat_format(self, temp_project):
        """Test _rewrite_hooks_data handles GitHub Copilot flat format (bash/powershell at top level)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "validate.sh").write_text("#!/bin/bash")
        (pkg_dir / "scripts" / "validate.ps1").write_text("Write-Host 'ok'")

        data = {
            "version": 1,
            "hooks": {
                "preToolUse": [
                    {
                        "type": "command",
                        "bash": "./scripts/validate.sh",
                        "powershell": "./scripts/validate.ps1",
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["preToolUse"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.sh" in hook["bash"]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.ps1" in hook["powershell"]
        assert len(scripts) == 2

    def test_rewrite_hooks_data_claude_nested_format(self, temp_project):
        """Test _rewrite_hooks_data handles Claude nested format (command in inner hooks array)."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "validate.sh").write_text("#!/bin/bash")

        data = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "./scripts/validate.sh"}
                        ]
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["PreToolUse"][0]["hooks"][0]
        assert ".github/hooks/scripts/my-pkg/scripts/validate.sh" in hook["command"]
        assert len(scripts) == 1

    def test_integrate_hooks_with_scripts_in_hooks_subdir(self, temp_project):
        """Test full integration when hook JSON and scripts are both inside hooks/ subdir."""
        pkg_dir = temp_project / "apm_modules" / "myorg" / "lint-hooks"
        hooks_dir = pkg_dir / "hooks"
        scripts_dir = hooks_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_data = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": {"tool_name": "write_to_file"},
                        "hooks": [
                            {"type": "command", "command": "./scripts/lint.sh", "timeout": 10}
                        ]
                    }
                ]
            }
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        (scripts_dir / "lint.sh").write_text("#!/bin/bash\necho lint")

        pkg_info = _make_package_info(pkg_dir, "lint-hooks")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pkg_info, temp_project)

        assert result.files_integrated == 1
        assert result.scripts_copied == 1

        # Verify the rewritten command points to the bundled script
        target_json = temp_project / ".github" / "hooks" / "lint-hooks-hooks.json"
        data = json.loads(target_json.read_text())
        cmd = data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert ".github/hooks/scripts/lint-hooks/scripts/lint.sh" in cmd
        assert "./" not in cmd

        # Verify the script was actually copied
        copied_script = temp_project / ".github" / "hooks" / "scripts" / "lint-hooks" / "scripts" / "lint.sh"
        assert copied_script.exists()
        assert copied_script.read_text() == "#!/bin/bash\necho lint"


# ─── End-to-end: install → verify → cleanup ──────────────────────────────────


class TestEndToEnd:
    """End-to-end tests covering full install → verify → cleanup cycle."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        project = Path(temp_dir)
        (project / ".github").mkdir()
        (project / ".claude").mkdir()
        yield project
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_full_hookify_lifecycle(self, temp_project):
        """Test full lifecycle: install hookify → verify → cleanup."""
        integrator = HookIntegrator()

        # Setup hookify package
        pkg_dir = temp_project / "apm_modules" / "anthropics" / "hookify"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(HOOKIFY_HOOKS_JSON))
        for script in ["pretooluse.py", "posttooluse.py", "stop.py", "userpromptsubmit.py"]:
            (hooks_dir / script).write_text(f"# {script}")

        pkg_info = _make_package_info(pkg_dir, "hookify")

        # Install VSCode hooks
        vscode_result = integrator.integrate_package_hooks(pkg_info, temp_project)
        assert vscode_result.files_integrated == 1
        assert vscode_result.scripts_copied == 4

        # Install Claude hooks
        claude_result = integrator.integrate_package_hooks_claude(pkg_info, temp_project)
        assert claude_result.files_integrated == 1

        # Verify files exist
        assert (temp_project / ".github" / "hooks" / "hookify-hooks.json").exists()
        assert (temp_project / ".claude" / "settings.json").exists()

        # Cleanup — manifest mode with paths from integration results
        managed_files = {
            str(p.relative_to(temp_project))
            for p in vscode_result.target_paths + claude_result.target_paths
        }
        stats = integrator.sync_integration(None, temp_project, managed_files=managed_files)
        assert stats["files_removed"] > 0

        # Verify cleanup
        assert not (temp_project / ".github" / "hooks" / "hookify-hooks.json").exists()
        assert not (temp_project / ".github" / "hooks" / "scripts").exists()
        assert not (temp_project / ".claude" / "hooks").exists()

    def test_multiple_packages_lifecycle(self, temp_project):
        """Test installing hooks from multiple packages, then cleaning up."""
        integrator = HookIntegrator()

        # Package 1: ralph-loop
        pkg1_dir = temp_project / "apm_modules" / "anthropics" / "ralph-loop"
        hooks1_dir = pkg1_dir / "hooks"
        hooks1_dir.mkdir(parents=True, exist_ok=True)
        (hooks1_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks1_dir / "stop-hook.sh").write_text("#!/bin/bash")
        pkg1_info = _make_package_info(pkg1_dir, "ralph-loop")

        # Package 2: learning-output-style
        pkg2_dir = temp_project / "apm_modules" / "anthropics" / "learning-output-style"
        hooks2_dir = pkg2_dir / "hooks"
        handlers_dir = pkg2_dir / "hooks-handlers"
        hooks2_dir.mkdir(parents=True, exist_ok=True)
        handlers_dir.mkdir(parents=True, exist_ok=True)
        (hooks2_dir / "hooks.json").write_text(json.dumps(LEARNING_OUTPUT_STYLE_HOOKS_JSON))
        (handlers_dir / "session-start.sh").write_text("#!/bin/bash")
        pkg2_info = _make_package_info(pkg2_dir, "learning-output-style")

        # Install both
        r1 = integrator.integrate_package_hooks(pkg1_info, temp_project)
        r2 = integrator.integrate_package_hooks(pkg2_info, temp_project)

        # Both hook JSONs should exist
        assert (temp_project / ".github" / "hooks" / "ralph-loop-hooks.json").exists()
        assert (temp_project / ".github" / "hooks" / "learning-output-style-hooks.json").exists()

        # Cleanup removes all — manifest mode
        managed_files = {
            str(p.relative_to(temp_project))
            for p in r1.target_paths + r2.target_paths
        }
        stats = integrator.sync_integration(None, temp_project, managed_files=managed_files)
        assert stats["files_removed"] >= 2
        assert not (temp_project / ".github" / "hooks" / "ralph-loop-hooks.json").exists()
        assert not (temp_project / ".github" / "hooks" / "learning-output-style-hooks.json").exists()


# ─── Deep copy safety test ───────────────────────────────────────────────────


class TestDeepCopySafety:
    """Test that rewriting doesn't mutate the original data."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_rewrite_does_not_mutate_original(self, temp_project):
        """Ensure _rewrite_hooks_data returns a copy, not mutating original."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "hooks").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "hooks" / "script.sh").write_text("#!/bin/bash")

        data = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/script.sh"}]}]
            }
        }
        original_cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]

        integrator = HookIntegrator()
        rewritten, _ = integrator._rewrite_hooks_data(data, pkg_dir, "test", "vscode")

        # Original should be unchanged
        assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == original_cmd
        # Rewritten should be different
        assert rewritten["hooks"]["Stop"][0]["hooks"][0]["command"] != original_cmd


# --- Codex hook integration tests ---------------------------------------------


class TestCodexHookIntegration:
    """Tests for Codex hooks.json merge with _apm_source markers."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        (self.root / ".codex").mkdir()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_package_info(self, name="test-pkg", hook_data=None):
        """Create a mock package info with hook files."""
        pkg_dir = self.root / "apm_modules" / name
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        if hook_data is None:
            hook_data = {
                "hooks": {
                    "SessionStart": [
                        {"type": "command", "command": "echo hello"}
                    ]
                }
            }

        hook_file = hooks_dir / "hooks.json"
        with open(hook_file, 'w', encoding='utf-8') as f:
            json.dump(hook_data, f)

        pi = MagicMock()
        pi.install_path = pkg_dir
        pi.package = MagicMock()
        pi.package.name = name
        return pi

    def test_codex_hooks_merge_into_hooks_json(self):
        """Hooks are merged into .codex/hooks.json with _apm_source markers."""
        pi = self._make_package_info()
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_codex(pi, self.root)

        assert result.files_integrated == 1
        hooks_json = self.root / ".codex" / "hooks.json"
        assert hooks_json.exists()
        data = json.loads(hooks_json.read_text())
        assert "SessionStart" in data["hooks"]
        entries = data["hooks"]["SessionStart"]
        assert any(e.get("_apm_source") == "test-pkg" for e in entries)

    def test_codex_hooks_preserve_user_hooks(self):
        """Existing user hooks in .codex/hooks.json are preserved."""
        # Write existing user hooks
        hooks_json = self.root / ".codex" / "hooks.json"
        hooks_json.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [
                    {"type": "command", "command": "echo user-hook"}
                ]
            }
        }))

        pi = self._make_package_info()
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_codex(pi, self.root)

        data = json.loads(hooks_json.read_text())
        # User hook preserved
        assert "PreToolUse" in data["hooks"]
        user_entries = [e for e in data["hooks"]["PreToolUse"] if "_apm_source" not in e]
        assert len(user_entries) == 1
        assert user_entries[0]["command"] == "echo user-hook"
        # APM hook added
        assert "SessionStart" in data["hooks"]

    def test_codex_hooks_not_deployed_without_codex_dir(self):
        """Hooks are not deployed if .codex/ directory doesn't exist."""
        shutil.rmtree(self.root / ".codex")

        pi = self._make_package_info()
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_codex(pi, self.root)

        assert result.files_integrated == 0


# --- Gemini hook integration tests -----------------------------------------------


class TestGeminiHookIntegration:
    """Tests for Gemini hook integration (.gemini/settings.json merge)."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        project = Path(temp_dir)
        (project / ".gemini").mkdir()
        yield project
        shutil.rmtree(temp_dir, ignore_errors=True)

    def _setup_hook_package(self, project: Path, name: str = "test-hooks") -> PackageInfo:
        pkg_dir = project / "apm_modules" / name
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(RALPH_LOOP_HOOKS_JSON))
        (hooks_dir / "stop-hook.sh").write_text("#!/bin/bash\nexit 0")
        return _make_package_info(pkg_dir, name)

    def test_integrate_hooks_gemini(self, temp_project):
        """Test Gemini integration merges hooks into settings.json."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        pkg_info = self._setup_hook_package(temp_project, "ralph-loop")
        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()

        result = integrator.integrate_hooks_for_target(target, pkg_info, temp_project)

        assert result.files_integrated == 1
        settings = json.loads(
            (temp_project / ".gemini" / "settings.json").read_text()
        )
        assert "hooks" in settings
        # "Stop" is mapped to "SessionEnd" for Gemini
        assert "SessionEnd" in settings["hooks"]
        assert "Stop" not in settings["hooks"]
        assert settings["hooks"]["SessionEnd"][0]["_apm_source"] == "ralph-loop"

    def test_skips_when_no_gemini_dir(self, temp_project):
        """Gemini hooks are not deployed when .gemini/ does not exist."""
        shutil.rmtree(temp_project / ".gemini")

        from apm_cli.integration.targets import KNOWN_TARGETS
        pkg_info = self._setup_hook_package(temp_project, "ralph-loop")
        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()

        result = integrator.integrate_hooks_for_target(target, pkg_info, temp_project)

        assert result.files_integrated == 0
        assert not (temp_project / ".gemini").exists()

    def test_merge_preserves_existing_keys(self, temp_project):
        """Hook merge preserves mcpServers and other top-level keys."""
        settings_path = temp_project / ".gemini" / "settings.json"
        settings_path.write_text(json.dumps({
            "mcpServers": {"srv": {"command": "echo"}},
            "theme": "dark",
        }))

        from apm_cli.integration.targets import KNOWN_TARGETS
        pkg_info = self._setup_hook_package(temp_project, "ralph-loop")
        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()

        integrator.integrate_hooks_for_target(target, pkg_info, temp_project)

        settings = json.loads(settings_path.read_text())
        assert settings["mcpServers"]["srv"]["command"] == "echo"
        assert settings["theme"] == "dark"
        assert "SessionEnd" in settings["hooks"]

    def test_additive_merge_same_event(self, temp_project):
        """Multiple packages can add hooks to the same event."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()

        pkg1_info = self._setup_hook_package(temp_project, "ralph-loop")
        integrator.integrate_hooks_for_target(target, pkg1_info, temp_project)

        pkg2_dir = temp_project / "apm_modules" / "other-pkg"
        hooks2_dir = pkg2_dir / "hooks"
        hooks2_dir.mkdir(parents=True, exist_ok=True)
        (hooks2_dir / "hooks.json").write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "echo other-stop"}
            ]}]}
        }))
        pkg2_info = _make_package_info(pkg2_dir, "other-pkg")
        integrator.integrate_hooks_for_target(target, pkg2_info, temp_project)

        settings = json.loads(
            (temp_project / ".gemini" / "settings.json").read_text()
        )
        # Both "Stop" entries land under "SessionEnd" after mapping
        assert len(settings["hooks"]["SessionEnd"]) == 2

    def test_reinstall_is_idempotent(self, temp_project):
        """Re-running integration does not duplicate hook entries."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        target = KNOWN_TARGETS["gemini"]
        pkg_info = self._setup_hook_package(temp_project, "ralph-loop")
        integrator = HookIntegrator()

        integrator.integrate_hooks_for_target(target, pkg_info, temp_project)
        first = (temp_project / ".gemini" / "settings.json").read_text()

        for _ in range(2):
            integrator.integrate_hooks_for_target(target, pkg_info, temp_project)

        settings = json.loads(
            (temp_project / ".gemini" / "settings.json").read_text()
        )
        assert len(settings["hooks"]["SessionEnd"]) == 1
        assert (temp_project / ".gemini" / "settings.json").read_text() == first

    def test_sync_removes_gemini_hook_entries(self, temp_project):
        """Sync removes APM-managed entries from .gemini/settings.json."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        settings_path = temp_project / ".gemini" / "settings.json"
        settings_path.write_text(json.dumps({
            "mcpServers": {"srv": {"command": "echo"}},
            "hooks": {
                "SessionEnd": [
                    {"_apm_source": "ralph-loop", "hooks": [
                        {"type": "command", "command": "..."}
                    ]},
                    {"hooks": [{"type": "command", "command": "echo user-hook"}]},
                ],
            }
        }))

        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()
        integrator.sync_integration(None, temp_project, targets=[target])

        settings = json.loads(settings_path.read_text())
        assert settings["mcpServers"]["srv"]["command"] == "echo"
        assert "SessionEnd" in settings["hooks"]
        assert len(settings["hooks"]["SessionEnd"]) == 1
        assert "_apm_source" not in settings["hooks"]["SessionEnd"][0]

    def test_sync_removes_empty_hooks_key(self, temp_project):
        """Empty hooks key is removed after sync cleanup."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        settings_path = temp_project / ".gemini" / "settings.json"
        settings_path.write_text(json.dumps({
            "mcpServers": {"srv": {"command": "echo"}},
            "hooks": {
                "SessionEnd": [{"_apm_source": "test", "hooks": []}]
            }
        }))

        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()
        integrator.sync_integration(None, temp_project, targets=[target])

        settings = json.loads(settings_path.read_text())
        assert "hooks" not in settings
        assert "mcpServers" in settings

    def test_event_name_mapping_pretooluse_to_beforetool(self, temp_project):
        """preToolUse (Copilot convention) maps to BeforeTool for Gemini."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        pkg_dir = temp_project / "apm_modules" / "lint-pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps({
            "hooks": {
                "preToolUse": [{"hooks": [
                    {"type": "command", "command": "echo lint"}
                ]}],
                "postToolUse": [{"hooks": [
                    {"type": "command", "command": "echo done"}
                ]}],
            }
        }))
        pkg_info = _make_package_info(pkg_dir, "lint-pkg")

        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()
        integrator.integrate_hooks_for_target(target, pkg_info, temp_project)

        settings = json.loads(
            (temp_project / ".gemini" / "settings.json").read_text()
        )
        assert "BeforeTool" in settings["hooks"]
        assert "AfterTool" in settings["hooks"]
        assert "preToolUse" not in settings["hooks"]
        assert "postToolUse" not in settings["hooks"]

    def test_unmapped_events_pass_through(self, temp_project):
        """Gemini-native events (BeforeAgent etc.) pass through unchanged."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        pkg_dir = temp_project / "apm_modules" / "agent-pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps({
            "hooks": {
                "BeforeAgent": [{"hooks": [
                    {"type": "command", "command": "echo agent"}
                ]}],
            }
        }))
        pkg_info = _make_package_info(pkg_dir, "agent-pkg")

        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()
        integrator.integrate_hooks_for_target(target, pkg_info, temp_project)

        settings = json.loads(
            (temp_project / ".gemini" / "settings.json").read_text()
        )
        assert "BeforeAgent" in settings["hooks"]

    def test_flat_copilot_entries_become_nested_gemini(self, temp_project):
        """Flat Copilot hook entries (bash, timeoutSec) are transformed to Gemini format."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        pkg_dir = temp_project / "apm_modules" / "flat-pkg"
        hooks_dir = pkg_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps({
            "hooks": {
                "preToolUse": [
                    {"type": "command", "bash": "echo lint", "timeoutSec": 10}
                ],
            }
        }))
        pkg_info = _make_package_info(pkg_dir, "flat-pkg")

        target = KNOWN_TARGETS["gemini"]
        integrator = HookIntegrator()
        integrator.integrate_hooks_for_target(target, pkg_info, temp_project)

        settings = json.loads(
            (temp_project / ".gemini" / "settings.json").read_text()
        )
        assert "BeforeTool" in settings["hooks"]
        entry = settings["hooks"]["BeforeTool"][0]
        # Must be nested: outer has "hooks" list, inner has "command" not "bash"
        assert "hooks" in entry
        inner = entry["hooks"][0]
        assert inner["command"] == "echo lint"
        assert "bash" not in inner
        # timeoutSec converted to timeout in milliseconds
        assert inner["timeout"] == 10000
        assert "timeoutSec" not in inner


# ─── Scope-resolved target tests (PR #566 rework) ────────────────────────────


class TestScopeResolvedHookDeployment:
    """Tests for scope-aware hook deployment using target.root_dir."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.root = Path(self.tmpdir)
        # Create package with hooks
        self.pkg_dir = self.root / "apm_modules" / "scope-pkg"
        hooks_dir = self.pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hooks_dir.joinpath("hooks.json").write_text(json.dumps({
            "hooks": {
                "SessionStart": [{"type": "command", "command": "echo hello"}]
            }
        }), encoding="utf-8")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_target(self, name, root_dir, primitives=None):
        """Create a minimal mock TargetProfile."""
        from unittest.mock import MagicMock
        t = MagicMock()
        t.name = name
        t.root_dir = root_dir
        t.supports = lambda prim: prim in (primitives or {"hooks"})
        if primitives is None:
            primitives = {"hooks"}
        t.primitives = {}
        for p in primitives:
            mapping = MagicMock()
            mapping.deploy_root = None
            t.primitives[p] = mapping
        return t

    def test_copilot_hooks_deploy_to_scope_resolved_dir(self):
        """Copilot hooks at user scope deploy to .copilot/hooks/ not .github/hooks/."""
        copilot_target = self._make_target("copilot", ".copilot")
        pi = _make_package_info(self.pkg_dir, "scope-pkg")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(
            pi, self.root, target=copilot_target,
        )

        assert result.files_integrated > 0
        # Hook file should be under .copilot/hooks/, not .github/hooks/
        hooks_dir = self.root / ".copilot" / "hooks"
        assert hooks_dir.exists()
        assert not (self.root / ".github" / "hooks").exists()

    def test_copilot_hooks_default_to_github(self):
        """Without target, hooks deploy to .github/hooks/ (backward compat)."""
        pi = _make_package_info(self.pkg_dir, "scope-pkg")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(pi, self.root)

        assert result.files_integrated > 0
        assert (self.root / ".github" / "hooks").exists()

    def test_merged_hooks_use_target_root_dir(self):
        """Claude hooks at user scope use target.root_dir for JSON path."""
        claude_target = self._make_target("claude", ".claude")
        (self.root / ".claude").mkdir()
        pi = _make_package_info(self.pkg_dir, "scope-pkg")
        integrator = HookIntegrator()

        result = integrator.integrate_hooks_for_target(
            claude_target, pi, self.root,
        )

        assert result.files_integrated > 0
        assert (self.root / ".claude" / "settings.json").exists()

    def test_codex_hooks_use_scope_resolved_root_dir(self):
        """Codex hooks at user scope merge into .codex/hooks.json."""
        codex_target = self._make_target("codex", ".codex")
        (self.root / ".codex").mkdir()
        pi = _make_package_info(self.pkg_dir, "scope-pkg")
        integrator = HookIntegrator()

        result = integrator.integrate_hooks_for_target(
            codex_target, pi, self.root,
        )

        assert result.files_integrated > 0
        assert (self.root / ".codex" / "hooks.json").exists()

    def test_script_paths_rewritten_with_scope_root(self):
        """Script paths in hook commands use the scope-resolved root_dir."""
        # Create a hook with a script reference
        hooks_dir = self.pkg_dir / ".apm" / "hooks"
        script = hooks_dir / "run.sh"
        script.write_text("#!/bin/bash\necho test", encoding="utf-8")
        hooks_dir.joinpath("hooks.json").write_text(json.dumps({
            "hooks": {
                "SessionStart": [{"type": "command", "command": "./run.sh"}]
            }
        }), encoding="utf-8")

        copilot_target = self._make_target("copilot", ".copilot")
        pi = _make_package_info(self.pkg_dir, "scope-pkg")
        integrator = HookIntegrator()

        result = integrator.integrate_package_hooks(
            pi, self.root, target=copilot_target,
        )

        # Script should be copied to .copilot/hooks/scripts/scope-pkg/
        scripts_dir = self.root / ".copilot" / "hooks" / "scripts" / "scope-pkg"
        assert scripts_dir.exists()
        assert (scripts_dir / "run.sh").exists()

    def test_sync_with_copilot_scope_prefix(self):
        """sync_integration removes .copilot/hooks/ files when target is present."""
        # Deploy first
        copilot_target = self._make_target("copilot", ".copilot")
        pi = _make_package_info(self.pkg_dir, "scope-pkg")
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(
            pi, self.root, target=copilot_target,
        )

        # Collect deployed paths
        managed = set()
        for p in result.target_paths:
            try:
                managed.add(str(p.relative_to(self.root)).replace("\\", "/"))
            except ValueError:
                pass

        # Sync should clean them up
        stats = integrator.sync_integration(
            None, self.root, managed_files=managed, targets=[copilot_target],
        )
        assert stats['files_removed'] > 0

    def test_auto_create_guard(self):
        """Targets with auto_create=False should not get directories created."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        # All targets except copilot have auto_create=False
        for name, profile in KNOWN_TARGETS.items():
            if not profile.auto_create:
                assert name != "copilot", "copilot should have auto_create=True"


# ─── Backslash path rewrite tests (issue #520) ───────────────────────────────


class TestBackslashPathRewrite:
    """Windows-style backslash paths in hook commands must be rewritten."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_rewrite_backslash_relative_path(self, temp_project):
        """Backslash relative paths should be rewritten like forward-slash paths."""
        pkg_dir = temp_project / "pkg"
        scripts_dir = pkg_dir / "secrets-scanner"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "scan-secrets.ps1").write_text("Write-Host 'scanning'")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "pwsh -File .\\secrets-scanner\\scan-secrets.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert ".github/hooks/scripts/my-pkg/secrets-scanner/scan-secrets.ps1" in cmd
        assert len(scripts) == 1

    def test_rewrite_backslash_hooks_data_flat(self, temp_project):
        """End-to-end: windows key with backslash paths in flat format."""
        pkg_dir = temp_project / "pkg"
        scripts_dir = pkg_dir / "secrets-scanner"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "scan.sh").write_text("#!/bin/bash")
        (scripts_dir / "scan.ps1").write_text("Write-Host 'ok'")

        data = {
            "hooks": {
                "Stop": [
                    {
                        "type": "command",
                        "command": "./secrets-scanner/scan.sh",
                        "windows": "pwsh -File .\\secrets-scanner\\scan.ps1",
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["Stop"][0]
        assert ".github/hooks/scripts/my-pkg/secrets-scanner/scan.sh" in hook["command"]
        assert ".github/hooks/scripts/my-pkg/secrets-scanner/scan.ps1" in hook["windows"]
        assert len(scripts) == 2

    def test_rewrite_backslash_hooks_data_nested(self, temp_project):
        """End-to-end: windows key with backslash paths in nested Claude format."""
        pkg_dir = temp_project / "pkg"
        scripts_dir = pkg_dir / "session-auto-commit"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "auto-commit.sh").write_text("#!/bin/bash")
        (scripts_dir / "auto-commit.ps1").write_text("Write-Host 'commit'")

        data = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "Always",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "./session-auto-commit/auto-commit.sh",
                                "windows": "pwsh -File .\\session-auto-commit\\auto-commit.ps1",
                            }
                        ]
                    }
                ]
            }
        }

        integrator = HookIntegrator()
        rewritten, scripts = integrator._rewrite_hooks_data(
            data, pkg_dir, "my-pkg", "vscode",
        )

        hook = rewritten["hooks"]["Stop"][0]["hooks"][0]
        assert ".github/hooks/scripts/my-pkg/session-auto-commit/auto-commit.sh" in hook["command"]
        assert ".github/hooks/scripts/my-pkg/session-auto-commit/auto-commit.ps1" in hook["windows"]
        assert len(scripts) == 2

    def test_rewrite_forward_slash_still_works(self, temp_project):
        """Forward-slash windows paths (./scripts/scan.ps1) still rewrite correctly."""
        pkg_dir = temp_project / "pkg"
        (pkg_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "scripts" / "scan.ps1").write_text("Write-Host 'ok'")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/scan.ps1",
            pkg_dir,
            "my-pkg",
            "vscode",
        )

        assert ".github/hooks/scripts/my-pkg/scripts/scan.ps1" in cmd
        assert len(scripts) == 1
