"""Unit tests for CommandIntegrator.

Tests cover:
- Command file discovery
- Command integration during install (no metadata injection)
- Command cleanup during uninstall (nuke-and-regenerate via sync_integration)
- Removal of all APM command files
"""

import shutil
import tempfile
from dataclasses import dataclass  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import pytest

from apm_cli.integration.command_integrator import (
    CommandIntegrator,
    _extract_input_names,
)


def _make_package(project_root, prompts):
    """Create a test package with .prompt.md files and return a mock PackageInfo.

    Module-level helper shared by every test class in this module that
    needs a package-on-disk.  Replaces five duplicated copies that used
    to live as ``_make_package`` methods on individual test classes.
    """
    pkg_dir = project_root / "apm_modules" / "test-pkg"
    pkg_dir.mkdir(parents=True)
    prompts_dir = pkg_dir / ".apm" / "prompts"
    prompts_dir.mkdir(parents=True)
    for name, content in prompts.items():
        (prompts_dir / name).write_text(content)

    mock_info = MagicMock()
    mock_info.install_path = pkg_dir
    mock_info.resolved_reference = None
    mock_info.package = MagicMock()
    mock_info.package.name = "test-pkg"
    return mock_info


class TestCommandIntegratorSyncIntegration:
    """Tests for sync_integration method (nuke-and-regenerate)."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project with .claude/commands directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        # Create commands directory
        commands_dir = temp_path / ".claude" / "commands"
        commands_dir.mkdir(parents=True)

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_sync_removes_all_apm_commands(self, temp_project):
        """Test that sync_integration removes all *-apm.md files."""
        commands_dir = temp_project / ".claude" / "commands"

        # Create command files for two packages
        pkg1_command = commands_dir / "audit-apm.md"
        pkg1_command.write_text("# Audit Command\n")

        pkg2_command = commands_dir / "review-apm.md"
        pkg2_command.write_text("# Review Command\n")

        integrator = CommandIntegrator()
        result = integrator.sync_integration(None, temp_project)

        assert result["files_removed"] == 2
        assert not pkg1_command.exists()
        assert not pkg2_command.exists()

    def test_sync_handles_empty_dependencies(self, temp_project):
        """Test sync removes all apm commands regardless of dependencies."""
        commands_dir = temp_project / ".claude" / "commands"

        command1 = commands_dir / "cmd1-apm.md"
        command1.write_text("# Command 1\n")

        command2 = commands_dir / "cmd2-apm.md"
        command2.write_text("# Command 2\n")

        mock_package = MagicMock()
        mock_package.dependencies = {"apm": []}

        integrator = CommandIntegrator()
        result = integrator.sync_integration(mock_package, temp_project)

        assert result["files_removed"] == 2
        assert not command1.exists()
        assert not command2.exists()

    def test_sync_ignores_non_apm_command_files(self, temp_project):
        """Test that sync_integration ignores command files without -apm suffix."""
        commands_dir = temp_project / ".claude" / "commands"

        # Create a non-APM command file (user-created)
        user_command = commands_dir / "my-custom-command.md"
        user_command.write_text("# My Custom Command\n")

        integrator = CommandIntegrator()
        result = integrator.sync_integration(None, temp_project)

        assert result["files_removed"] == 0
        assert user_command.exists()

    def test_sync_handles_nonexistent_commands_dir(self):
        """Test sync handles missing .claude/commands directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        try:
            integrator = CommandIntegrator()
            result = integrator.sync_integration(None, temp_path)
            assert result["files_removed"] == 0
            assert result["errors"] == 0
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_sync_apm_package_param_is_unused(self, temp_project):
        """Test that sync works regardless of what apm_package is passed."""
        commands_dir = temp_project / ".claude" / "commands"

        cmd = commands_dir / "test-apm.md"
        cmd.write_text("# Test\n")

        integrator = CommandIntegrator()

        # Works with None
        result = integrator.sync_integration(None, temp_project)
        assert result["files_removed"] == 1


class TestRemovePackageCommands:
    """Tests for remove_package_commands method."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project with .claude/commands directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        commands_dir = temp_path / ".claude" / "commands"
        commands_dir.mkdir(parents=True)

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_removes_all_apm_commands(self, temp_project):
        """Test that remove_package_commands removes all *-apm.md files."""
        commands_dir = temp_project / ".claude" / "commands"

        cmd1 = commands_dir / "audit-apm.md"
        cmd1.write_text("# Audit\n")

        cmd2 = commands_dir / "review-apm.md"
        cmd2.write_text("# Review\n")

        cmd3 = commands_dir / "design-apm.md"
        cmd3.write_text("# Design\n")

        integrator = CommandIntegrator()
        removed = integrator.remove_package_commands("any/package", temp_project)

        assert removed == 3
        assert not cmd1.exists()
        assert not cmd2.exists()
        assert not cmd3.exists()

    def test_returns_zero_when_no_commands_dir(self, temp_project):
        """Test that remove_package_commands returns 0 when no commands directory exists."""
        shutil.rmtree(temp_project / ".claude" / "commands")

        integrator = CommandIntegrator()
        removed = integrator.remove_package_commands("any/package", temp_project)

        assert removed == 0

    def test_preserves_non_apm_files(self, temp_project):
        """Test that non-APM files are preserved."""
        commands_dir = temp_project / ".claude" / "commands"

        user_cmd = commands_dir / "my-command.md"
        user_cmd.write_text("# User command\n")

        apm_cmd = commands_dir / "test-apm.md"
        apm_cmd.write_text("# APM command\n")

        integrator = CommandIntegrator()
        removed = integrator.remove_package_commands("any/package", temp_project)

        assert removed == 1
        assert not apm_cmd.exists()
        assert user_cmd.exists()


class TestIntegrateCommandNoMetadata:
    """Tests that integrate_command does NOT inject APM metadata."""

    @pytest.fixture
    def temp_project(self):
        """Create temporary project with source and target dirs."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        (temp_path / "source").mkdir()
        (temp_path / ".claude" / "commands").mkdir(parents=True)

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_no_apm_metadata_in_output(self, temp_project):
        """Test that integrated command files contain no APM metadata block."""
        source = temp_project / "source" / "audit.prompt.md"
        source.write_text("""---
description: Run audit checks
---
# Audit Command
Run compliance audit.
""")

        target = temp_project / ".claude" / "commands" / "audit-apm.md"

        mock_info = MagicMock()
        mock_info.package.name = "test/pkg"
        mock_info.package.version = "1.0.0"
        mock_info.package.source = "https://github.com/test/pkg"
        mock_info.resolved_reference = None
        mock_info.install_path = temp_project / "source"
        mock_info.installed_at = "2024-01-01"
        mock_info.get_canonical_dependency_string.return_value = "test/pkg"

        integrator = CommandIntegrator()
        integrator.integrate_command(source, target, mock_info, source)

        # Verify no APM metadata
        post = frontmatter.load(target)
        assert "apm" not in post.metadata

        # Verify legitimate metadata IS preserved
        assert post.metadata.get("description") == "Run audit checks"

    def test_content_preserved_verbatim(self, temp_project):
        """Test that command content is preserved without modification."""
        content = "# My Command\nDo something useful.\n\n## Steps\n1. First\n2. Second"
        source = temp_project / "source" / "test.prompt.md"
        source.write_text(f"---\ndescription: Test\n---\n{content}\n")

        target = temp_project / ".claude" / "commands" / "test-apm.md"

        mock_info = MagicMock()
        mock_info.resolved_reference = None

        integrator = CommandIntegrator()
        integrator.integrate_command(source, target, mock_info, source)

        post = frontmatter.load(target)
        assert content in post.content

    def test_claude_metadata_mapping(self, temp_project):
        """Test that Claude-specific frontmatter fields are mapped correctly."""
        source = temp_project / "source" / "cmd.prompt.md"
        source.write_text("""---
description: A command
allowed-tools: ["bash", "edit"]
model: claude-sonnet
argument-hint: "file path"
---
# Command
""")

        target = temp_project / ".claude" / "commands" / "cmd-apm.md"

        mock_info = MagicMock()
        mock_info.resolved_reference = None

        integrator = CommandIntegrator()
        integrator.integrate_command(source, target, mock_info, source)

        post = frontmatter.load(target)
        assert post.metadata["description"] == "A command"
        assert post.metadata["allowed-tools"] == ["bash", "edit"]
        assert post.metadata["model"] == "claude-sonnet"
        assert post.metadata["argument-hint"] == "file path"
        assert "apm" not in post.metadata


class TestSecurityWarningsSurfaced:
    """Verify SecurityGate warnings reach diagnostics."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / "source").mkdir()
        (temp_path / ".claude" / "commands").mkdir(parents=True)
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_critical_chars_recorded_in_diagnostics(self, temp_project):
        """SecurityGate critical finding surfaces via diagnostics.security()."""
        from apm_cli.utils.diagnostics import DiagnosticCollector

        source = temp_project / "source" / "evil.prompt.md"
        source.write_text(
            "---\ndescription: Evil\n---\nHidden tag\U000e0041char.\n",
            encoding="utf-8",
        )
        target = temp_project / ".claude" / "commands" / "evil.md"

        mock_info = MagicMock()
        mock_info.package = MagicMock()
        mock_info.package.name = "evil-pkg"
        mock_info.resolved_reference = None

        diag = DiagnosticCollector()
        integrator = CommandIntegrator()
        integrator.integrate_command(
            source,
            target,
            mock_info,
            source,
            diagnostics=diag,
        )

        assert diag.security_count >= 1
        items = diag.by_category().get("security", [])
        # Critical findings must land in the critical bucket (severity), and
        # the short message must read as critical (not be downgraded).
        assert any(i.severity == "critical" and "critical" in i.message.lower() for i in items)

    def test_warning_only_findings_recorded_in_diagnostics(self, temp_project):
        """SecurityGate warning-only findings (e.g. soft hyphen) also surface."""
        from apm_cli.utils.diagnostics import DiagnosticCollector

        source = temp_project / "source" / "warn.prompt.md"
        # U+00AD soft hyphen is classified as a 'warning', not critical.
        source.write_text(
            "---\ndescription: Warn\n---\nSoft\u00adhyphen here.\n",
            encoding="utf-8",
        )
        target = temp_project / ".claude" / "commands" / "warn.md"

        mock_info = MagicMock()
        mock_info.package = MagicMock()
        mock_info.package.name = "warn-pkg"
        mock_info.resolved_reference = None

        diag = DiagnosticCollector()
        integrator = CommandIntegrator()
        integrator.integrate_command(
            source,
            target,
            mock_info,
            source,
            diagnostics=diag,
        )

        items = diag.by_category().get("security", [])
        assert any(i.severity == "warning" for i in items)


class TestOpenCodeCommandIntegration:
    """Tests for OpenCode command integration."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project with .opencode/ directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / ".opencode").mkdir()
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def temp_project_no_opencode(self):
        """Create a temporary project without .opencode/ directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_skips_when_opencode_dir_missing(self, temp_project_no_opencode):
        """Opt-in: skip if .opencode/ does not exist."""
        pkg_info = _make_package(
            temp_project_no_opencode,
            {"test.prompt.md": "---\ndescription: Test\n---\n# Test"},
        )
        integrator = CommandIntegrator()
        result = integrator.integrate_package_commands_opencode(pkg_info, temp_project_no_opencode)
        assert result.files_integrated == 0
        assert not (temp_project_no_opencode / ".opencode" / "commands").exists()

    def test_deploys_prompts_to_opencode_commands(self, temp_project):
        """Deploy .prompt.md → .opencode/commands/<name>.md."""
        pkg_info = _make_package(
            temp_project,
            {"test.prompt.md": "---\ndescription: A test\n---\n# Test command"},
        )
        integrator = CommandIntegrator()
        result = integrator.integrate_package_commands_opencode(pkg_info, temp_project)
        assert result.files_integrated == 1
        target = temp_project / ".opencode" / "commands" / "test.md"
        assert target.exists()

    def test_deploys_multiple_prompts(self, temp_project):
        """Deploy multiple prompts to .opencode/commands/."""
        pkg_info = _make_package(
            temp_project,
            {
                "review.prompt.md": "---\ndescription: Review\n---\n# Review",
                "fix.prompt.md": "---\ndescription: Fix\n---\n# Fix",
            },
        )
        integrator = CommandIntegrator()
        result = integrator.integrate_package_commands_opencode(pkg_info, temp_project)
        assert result.files_integrated == 2

    def test_sync_removes_apm_commands(self, temp_project):
        """Sync removes APM-managed commands from .opencode/commands/."""
        cmds = temp_project / ".opencode" / "commands"
        cmds.mkdir(parents=True)
        (cmds / "test-apm.md").write_text("# APM managed")
        (cmds / "custom.md").write_text("# User created")

        integrator = CommandIntegrator()
        result = integrator.sync_integration_opencode(None, temp_project)

        assert result["files_removed"] == 1
        assert not (cmds / "test-apm.md").exists()
        assert (cmds / "custom.md").exists()

    def test_sync_handles_missing_dir(self, temp_project_no_opencode):
        """Sync handles missing .opencode/commands/ gracefully."""
        integrator = CommandIntegrator()
        result = integrator.sync_integration_opencode(None, temp_project_no_opencode)
        assert result["files_removed"] == 0


class TestIntegratePackagePrimitivesTargetGating:
    """Tests that _integrate_package_primitives respects target gating.

    Regression test for: commands/agents/hooks were dispatched to targets
    that were not in the active targets list (e.g., --target copilot wrote
    to .claude/).
    """

    def _make_mock_integrators(self):
        """Return a dict of MagicMock integrators for _integrate_package_primitives."""
        from unittest.mock import MagicMock

        def _empty_result(*args, **kwargs):
            r = MagicMock()
            r.files_integrated = 0
            r.files_updated = 0
            r.links_resolved = 0
            r.target_paths = []
            r.skill_created = False
            r.sub_skills_promoted = 0
            r.files_integrated = 0
            return r

        integrators = {}
        for name in (
            "prompt_integrator",
            "agent_integrator",
            "skill_integrator",
            "instruction_integrator",
            "command_integrator",
            "hook_integrator",
        ):
            m = MagicMock()
            # Target-driven methods used by the dispatch loop
            for method in (
                "integrate_prompts_for_target",
                "integrate_agents_for_target",
                "integrate_commands_for_target",
                "integrate_instructions_for_target",
                "integrate_hooks_for_target",
                "integrate_package_skill",
            ):
                getattr(m, method).side_effect = _empty_result
            integrators[name] = m
        return integrators

    def test_copilot_only_does_not_dispatch_commands(self):
        """When targets=[copilot], commands must not be dispatched.

        Copilot has no ``commands`` primitive, so the dispatch loop
        should never call ``integrate_commands_for_target``.
        """
        from apm_cli.commands.install import _integrate_package_primitives
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        temp_dir = tempfile.mkdtemp()
        try:
            project_root = Path(temp_dir)
            (project_root / ".github").mkdir()

            package_info = MagicMock()
            integrators = self._make_mock_integrators()
            diagnostics = DiagnosticCollector(verbose=False)

            _integrate_package_primitives(
                package_info,
                project_root,
                targets=[KNOWN_TARGETS["copilot"]],
                managed_files=set(),
                force=False,
                diagnostics=diagnostics,
                **integrators,
            )

            integrators["command_integrator"].integrate_commands_for_target.assert_not_called()
            assert not (project_root / ".claude" / "commands").exists()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_claude_target_dispatches_commands(self):
        """When targets=[claude], commands must be dispatched."""
        from apm_cli.commands.install import _integrate_package_primitives
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        temp_dir = tempfile.mkdtemp()
        try:
            project_root = Path(temp_dir)
            (project_root / ".claude").mkdir()

            package_info = MagicMock()
            integrators = self._make_mock_integrators()
            diagnostics = DiagnosticCollector(verbose=False)

            _integrate_package_primitives(
                package_info,
                project_root,
                targets=[KNOWN_TARGETS["claude"]],
                managed_files=set(),
                force=False,
                diagnostics=diagnostics,
                **integrators,
            )

            integrators["command_integrator"].integrate_commands_for_target.assert_called_once()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_cursor_target_dispatches_commands(self):
        """When targets=[cursor], commands must be dispatched."""
        from apm_cli.commands.install import _integrate_package_primitives
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        temp_dir = tempfile.mkdtemp()
        try:
            project_root = Path(temp_dir)
            (project_root / ".cursor").mkdir()

            package_info = MagicMock()
            integrators = self._make_mock_integrators()
            diagnostics = DiagnosticCollector(verbose=False)

            _integrate_package_primitives(
                package_info,
                project_root,
                targets=[KNOWN_TARGETS["cursor"]],
                managed_files=set(),
                force=False,
                diagnostics=diagnostics,
                **integrators,
            )

            integrators["command_integrator"].integrate_commands_for_target.assert_called_once()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestCursorCommandEndToEnd:
    """Full dispatch-layer test: .prompt.md -> .cursor/commands/ via real integrators."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / ".cursor").mkdir()
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_full_dispatch_deploys_to_cursor(self, temp_project):
        """Prompt files deploy to .cursor/commands/ via full dispatch pipeline."""
        from apm_cli.install.services import integrate_package_primitives
        from apm_cli.integration import (
            AgentIntegrator,
            HookIntegrator,
            InstructionIntegrator,
            PromptIntegrator,
            SkillIntegrator,
        )
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project,
            {
                "review.prompt.md": (
                    "---\n"
                    "description: Review code quality\n"
                    'allowed-tools: ["bash", "edit"]\n'
                    "---\n"
                    "Review the code for quality issues.\n"
                ),
            },
        )

        result = integrate_package_primitives(
            pkg_info,
            temp_project,
            targets=[KNOWN_TARGETS["cursor"]],
            prompt_integrator=PromptIntegrator(),
            agent_integrator=AgentIntegrator(),
            skill_integrator=SkillIntegrator(),
            instruction_integrator=InstructionIntegrator(),
            command_integrator=CommandIntegrator(),
            hook_integrator=HookIntegrator(),
            force=False,
            managed_files=set(),
            diagnostics=DiagnosticCollector(),
        )

        assert result["commands"] == 1

        target = temp_project / ".cursor" / "commands" / "review.md"
        assert target.exists()
        post = frontmatter.load(target)
        assert post.metadata["description"] == "Review code quality"
        assert post.metadata["allowed-tools"] == ["bash", "edit"]
        assert "Review the code for quality issues." in post.content


class TestExtractInputNames:
    """Tests for _extract_input_names helper."""

    def test_none(self):
        assert _extract_input_names(None) == ([], [])

    def test_string(self):
        assert _extract_input_names("name") == (["name"], [])

    def test_simple_list(self):
        assert _extract_input_names(["a", "b", "c"]) == (["a", "b", "c"], [])

    def test_object_list(self):
        valid, rejected = _extract_input_names(
            [
                {"feature_name": "Name"},
                {"desc": "Description"},
            ]
        )
        assert valid == ["feature_name", "desc"]
        assert rejected == []

    def test_mixed_list(self):
        valid, rejected = _extract_input_names(
            [
                "simple_arg",
                {"complex_arg": "A complex argument"},
            ]
        )
        assert valid == ["simple_arg", "complex_arg"]
        assert rejected == []

    def test_bare_dict(self):
        valid, rejected = _extract_input_names({"a": "desc a", "b": "desc b"})
        assert valid == ["a", "b"]
        assert rejected == []

    def test_empty_string(self):
        assert _extract_input_names("") == ([], [])

    def test_whitespace_only_string(self):
        assert _extract_input_names("   ") == ([], [])

    def test_empty_strings_in_list(self):
        valid, _ = _extract_input_names(["name", "", "  ", "category"])
        assert valid == ["name", "category"]

    def test_empty_keys_in_dict(self):
        valid, _ = _extract_input_names({"": "empty", "name": "ok"})
        assert valid == ["name"]

    def test_empty_keys_in_object_list(self):
        valid, _ = _extract_input_names([{"": "empty"}, {"name": "ok"}])
        assert valid == ["name"]

    def test_yaml_injection_dict_key_rejected(self):
        """A dict key with YAML-significant characters must be rejected."""
        malicious = {"foo>\ninjected_key": "desc"}
        valid, rejected = _extract_input_names(malicious)
        assert valid == []
        assert any("injected_key" in r for r in rejected)

    def test_yaml_injection_list_string_rejected(self):
        """A list string with newline/colon must be rejected."""
        valid, rejected = _extract_input_names(["good", "bad: name", "evil\nkey"])
        assert valid == ["good"]
        assert "bad: name" in rejected
        assert "evil\nkey" in rejected

    def test_leading_digit_rejected(self):
        """Names must start with a letter."""
        valid, rejected = _extract_input_names(["1bad", "good"])
        assert valid == ["good"]
        assert "1bad" in rejected

    def test_overlong_name_rejected(self):
        """Names over 64 chars (1 + 63) are rejected."""
        long_name = "a" + "b" * 64
        valid, rejected = _extract_input_names([long_name, "ok"])
        assert valid == ["ok"]
        assert long_name in rejected

    def test_hyphenated_name_accepted(self):
        valid, rejected = _extract_input_names(["my-arg"])
        assert valid == ["my-arg"]
        assert rejected == []


class TestInputToArgumentsEndToEnd:
    """Full dispatch-layer test: .prompt.md with input -> Claude arguments.

    Exercises integrate_package_primitives with real (non-mocked) integrators
    to verify the input-to-arguments mapping survives the full install path.
    """

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / ".claude").mkdir()
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_full_dispatch_maps_input_to_arguments(self, temp_project):
        """input: [name, category] produces Claude arguments via full dispatch."""
        from apm_cli.install.services import integrate_package_primitives
        from apm_cli.integration import (
            AgentIntegrator,
            HookIntegrator,
            InstructionIntegrator,
            PromptIntegrator,
            SkillIntegrator,
        )
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project,
            {
                "gen.prompt.md": (
                    "---\n"
                    "description: Generate something\n"
                    "input: [name, category]\n"
                    "---\n"
                    "Create ${{input:name}} in ${{input:category}}.\n"
                ),
            },
        )

        result = integrate_package_primitives(
            pkg_info,
            temp_project,
            targets=[KNOWN_TARGETS["claude"]],
            prompt_integrator=PromptIntegrator(),
            agent_integrator=AgentIntegrator(),
            skill_integrator=SkillIntegrator(),
            instruction_integrator=InstructionIntegrator(),
            command_integrator=CommandIntegrator(),
            hook_integrator=HookIntegrator(),
            force=False,
            managed_files=set(),
            diagnostics=DiagnosticCollector(),
        )

        assert result["commands"] == 1

        target = temp_project / ".claude" / "commands" / "gen.md"
        assert target.exists()
        post = frontmatter.load(target)
        assert post.metadata["arguments"] == ["name", "category"]
        assert post.metadata["argument-hint"] == "<name> <category>"
        assert "$name" in post.content
        assert "$category" in post.content
        assert "${{input:" not in post.content


class TestInputToArgumentsIntegration:
    """Integrator-level test: .prompt.md with input -> Claude arguments front-matter."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / ".claude").mkdir()
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_input_list_becomes_arguments(self, temp_project):
        """input: [name, category] maps to arguments: [name, category]."""
        pkg_info = _make_package(
            temp_project,
            {
                "gen.prompt.md": (
                    "---\n"
                    "description: Generate something\n"
                    "input: [name, category]\n"
                    "---\n"
                    "Create ${{input:name}} in ${{input:category}}.\n"
                ),
            },
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(
            KNOWN_TARGETS["claude"],
            pkg_info,
            temp_project,
        )

        target = temp_project / ".claude" / "commands" / "gen.md"
        assert target.exists()

        post = frontmatter.load(target)
        assert post.metadata["arguments"] == ["name", "category"]
        assert post.metadata["argument-hint"] == "<name> <category>"
        assert "$name" in post.content
        assert "$category" in post.content
        assert "${{input:" not in post.content

    def test_input_object_list_becomes_arguments(self, temp_project):
        """input as object list extracts keys as argument names."""
        pkg_info = _make_package(
            temp_project,
            {
                "feat.prompt.md": (
                    "---\n"
                    "description: Feature generator\n"
                    "input:\n"
                    "  - feature_name: Name of the feature\n"
                    "  - feature_desc: Description\n"
                    "---\n"
                    "Build ${{input:feature_name}}: ${{input:feature_desc}}\n"
                ),
            },
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(
            KNOWN_TARGETS["claude"],
            pkg_info,
            temp_project,
        )

        target = temp_project / ".claude" / "commands" / "feat.md"
        post = frontmatter.load(target)
        assert post.metadata["arguments"] == ["feature_name", "feature_desc"]
        assert "$feature_name" in post.content
        assert "$feature_desc" in post.content

    def test_explicit_argument_hint_not_overridden(self, temp_project):
        """When argument-hint is already set, input does not override it."""
        pkg_info = _make_package(
            temp_project,
            {
                "cmd.prompt.md": (
                    "---\n"
                    "description: A command\n"
                    "argument-hint: <custom-hint>\n"
                    "input: [x]\n"
                    "---\n"
                    "Do ${{input:x}}.\n"
                ),
            },
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(
            KNOWN_TARGETS["claude"],
            pkg_info,
            temp_project,
        )

        target = temp_project / ".claude" / "commands" / "cmd.md"
        post = frontmatter.load(target)
        assert post.metadata["argument-hint"] == "<custom-hint>"
        assert post.metadata["arguments"] == ["x"]

    def test_bare_dict_input_becomes_arguments(self, temp_project):
        """input: {a: 'desc'} (bare dict) maps to arguments: [a]."""
        pkg_info = _make_package(
            temp_project,
            {
                "d.prompt.md": (
                    "---\n"
                    "description: Dict input\n"
                    "input:\n"
                    "  feature-name: Name of the feature\n"
                    "  feature-desc: Description\n"
                    "---\n"
                    "Build ${{input:feature-name}}: ${{input:feature-desc}}\n"
                ),
            },
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(
            KNOWN_TARGETS["claude"],
            pkg_info,
            temp_project,
        )

        target = temp_project / ".claude" / "commands" / "d.md"
        post = frontmatter.load(target)
        assert post.metadata["arguments"] == ["feature-name", "feature-desc"]
        assert "$feature-name" in post.content
        assert "$feature-desc" in post.content
        assert "${{input:" not in post.content

    def test_hyphenated_input_names_substituted(self, temp_project):
        """Hyphenated names like feature-name are replaced in content."""
        pkg_info = _make_package(
            temp_project,
            {
                "h.prompt.md": (
                    "---\n"
                    "description: Hyphen test\n"
                    "input: [my-arg]\n"
                    "---\n"
                    "Use ${{input:my-arg}} here.\n"
                ),
            },
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(
            KNOWN_TARGETS["claude"],
            pkg_info,
            temp_project,
        )

        target = temp_project / ".claude" / "commands" / "h.md"
        post = frontmatter.load(target)
        assert "$my-arg" in post.content
        assert "${{input:my-arg}}" not in post.content

    def test_single_brace_input_references_substituted(self, temp_project):
        """${input:name} (single-brace, the canonical docs format) is rewritten."""
        pkg_info = _make_package(
            temp_project,
            {
                "s.prompt.md": (
                    "---\n"
                    "description: Single-brace test\n"
                    "input: [name, category]\n"
                    "---\n"
                    "Create ${input:name} in ${input:category}.\n"
                ),
            },
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(
            KNOWN_TARGETS["claude"],
            pkg_info,
            temp_project,
        )

        target = temp_project / ".claude" / "commands" / "s.md"
        post = frontmatter.load(target)
        assert post.metadata["arguments"] == ["name", "category"]
        assert "$name" in post.content
        assert "$category" in post.content
        assert "${input:" not in post.content


class TestInputMappingDiagnostics:
    """Verify install-time visibility when input -> arguments mapping happens."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / "source").mkdir()
        (temp_path / ".claude" / "commands").mkdir(parents=True)
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_mapping_emits_info_diagnostic(self, temp_project):
        """When input is mapped, an info-level diagnostic is recorded."""
        from apm_cli.utils.diagnostics import DiagnosticCollector

        source = temp_project / "source" / "review.prompt.md"
        source.write_text(
            "---\ndescription: Review\ninput: [feature_name, priority]\n---\n"
            "Review ${input:feature_name} priority ${input:priority}.\n",
            encoding="utf-8",
        )
        target = temp_project / ".claude" / "commands" / "review.md"

        mock_info = MagicMock()
        mock_info.package = MagicMock()
        mock_info.package.name = "test-pkg"
        mock_info.resolved_reference = None

        diag = DiagnosticCollector()
        CommandIntegrator().integrate_command(
            source,
            target,
            mock_info,
            source,
            diagnostics=diag,
        )

        info_items = diag.by_category().get("info", [])
        assert any(
            "Mapped input -> command arguments" in i.message
            and "feature_name" in i.message
            and "priority" in i.message
            for i in info_items
        )

    def test_yaml_injection_attempt_warns(self, temp_project):
        """A package supplying a YAML-injecting dict key gets a warn diagnostic and the key is dropped."""
        from apm_cli.utils.diagnostics import DiagnosticCollector

        source = temp_project / "source" / "evil.prompt.md"
        # The first entry contains a newline+colon that would inject a new key
        # if written verbatim into YAML; the allowlist must reject it.
        source.write_text(
            "---\n"
            "description: Evil\n"
            "input:\n"
            '  - "foo>\\ninjected_key": bad\n'
            "  - good_arg: ok\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )
        target = temp_project / ".claude" / "commands" / "evil.md"

        mock_info = MagicMock()
        mock_info.package = MagicMock()
        mock_info.package.name = "evil-pkg"
        mock_info.resolved_reference = None

        diag = DiagnosticCollector()
        CommandIntegrator().integrate_command(
            source,
            target,
            mock_info,
            source,
            diagnostics=diag,
        )

        post = frontmatter.load(target)
        assert post.metadata["arguments"] == ["good_arg"]
        warn_items = diag.by_category().get("warning", [])
        assert any("rejected" in w.message for w in warn_items)


class TestSecurityScanFailClosed:
    """Verify the security scan fails closed when the gate cannot be loaded."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / "source").mkdir()
        (temp_path / ".claude" / "commands").mkdir(parents=True)
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_import_error_re_raised(self, temp_project, monkeypatch):
        """ImportError from SecurityGate.scan_text must propagate (fail closed)."""
        from apm_cli.integration import command_integrator as ci

        def boom(*args, **kwargs):
            raise ImportError("simulated missing gate")

        monkeypatch.setattr(ci.SecurityGate, "scan_text", boom)

        source = temp_project / "source" / "x.prompt.md"
        source.write_text(
            "---\ndescription: X\n---\nbody\n",
            encoding="utf-8",
        )
        target = temp_project / ".claude" / "commands" / "x.md"

        mock_info = MagicMock()
        mock_info.package = MagicMock()
        mock_info.package.name = "p"
        mock_info.resolved_reference = None

        with pytest.raises(ImportError):
            CommandIntegrator().integrate_command(
                source,
                target,
                mock_info,
                source,
            )
        # Fail-closed: file must NOT have been written.
        assert not target.exists()


# ===================================================================
# Gemini CLI Command Integration (.toml format)
# ===================================================================


class TestGeminiCommandIntegration:
    """Tests for Gemini CLI command integration (.prompt.md → .toml)."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project with .gemini/ directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / ".gemini").mkdir()
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def temp_project_no_gemini(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_skips_when_no_gemini_dir(self, temp_project_no_gemini):
        """Opt-in: skip if .gemini/ does not exist."""
        pkg_info = _make_package(
            temp_project_no_gemini,
            {"test.prompt.md": "---\ndescription: Test\n---\n# Test"},
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        result = integrator.integrate_commands_for_target(
            KNOWN_TARGETS["gemini"], pkg_info, temp_project_no_gemini
        )
        assert result.files_integrated == 0

    def test_deploys_toml_commands(self, temp_project):
        """Deploy .prompt.md → .gemini/commands/<name>.toml."""
        pkg_info = _make_package(
            temp_project,
            {"review.prompt.md": "---\ndescription: Review code\n---\nReview the code."},
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        result = integrator.integrate_commands_for_target(
            KNOWN_TARGETS["gemini"], pkg_info, temp_project
        )
        assert result.files_integrated == 1
        target = temp_project / ".gemini" / "commands" / "review.toml"
        assert target.exists()
        content = target.read_text()
        assert "Review the code." in content
        assert "Review code" in content

    def test_toml_is_valid(self, temp_project):
        """Verify generated file is valid TOML."""
        import toml

        pkg_info = _make_package(
            temp_project,
            {"test.prompt.md": "---\ndescription: A test\n---\nDo the thing."},
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(KNOWN_TARGETS["gemini"], pkg_info, temp_project)
        target = temp_project / ".gemini" / "commands" / "test.toml"
        parsed = toml.loads(target.read_text())
        assert parsed["description"] == "A test"
        assert "Do the thing." in parsed["prompt"]

    def test_arguments_replacement(self, temp_project):
        """$ARGUMENTS is replaced with {{args}}."""
        pkg_info = _make_package(
            temp_project,
            {"cmd.prompt.md": "---\ndescription: Run cmd\n---\nRun with $ARGUMENTS"},
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(KNOWN_TARGETS["gemini"], pkg_info, temp_project)
        target = temp_project / ".gemini" / "commands" / "cmd.toml"
        content = target.read_text()
        assert "{{args}}" in content
        assert "$ARGUMENTS" not in content

    def test_positional_args_prepends_args_line(self, temp_project):
        """When $1 or $2 are found, prepend 'Arguments: {{args}}'."""
        pkg_info = _make_package(
            temp_project,
            {"cmd.prompt.md": "---\ndescription: Fix\n---\nFix file $1"},
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(KNOWN_TARGETS["gemini"], pkg_info, temp_project)
        target = temp_project / ".gemini" / "commands" / "cmd.toml"
        import toml

        parsed = toml.loads(target.read_text())
        assert parsed["prompt"].startswith("Arguments: {{args}}")

    def test_no_description_omits_key(self, temp_project):
        """When no description in frontmatter, TOML omits description key."""
        pkg_info = _make_package(
            temp_project,
            {"cmd.prompt.md": "Just do the thing."},
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(KNOWN_TARGETS["gemini"], pkg_info, temp_project)
        target = temp_project / ".gemini" / "commands" / "cmd.toml"
        import toml

        parsed = toml.loads(target.read_text())
        assert "description" not in parsed
        assert "Just do the thing." in parsed["prompt"]


class TestWriteGeminiCommand:
    """Direct unit tests for CommandIntegrator._write_gemini_command()."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_basic_conversion(self):
        source = Path(self.temp_dir) / "test.prompt.md"
        target = Path(self.temp_dir) / "test.toml"
        source.write_text("---\ndescription: Test command\n---\nDo something.")
        CommandIntegrator._write_gemini_command(source, target)

        import toml

        parsed = toml.loads(target.read_text())
        assert parsed["description"] == "Test command"
        assert parsed["prompt"] == "Do something."

    def test_arguments_replaced(self):
        source = Path(self.temp_dir) / "test.prompt.md"
        target = Path(self.temp_dir) / "test.toml"
        source.write_text("Review $ARGUMENTS")
        CommandIntegrator._write_gemini_command(source, target)

        import toml

        parsed = toml.loads(target.read_text())
        assert "{{args}}" in parsed["prompt"]
        assert "$ARGUMENTS" not in parsed["prompt"]

    def test_creates_parent_dirs(self):
        source = Path(self.temp_dir) / "test.prompt.md"
        target = Path(self.temp_dir) / "sub" / "dir" / "test.toml"
        source.write_text("# Test")
        CommandIntegrator._write_gemini_command(source, target)
        assert target.exists()


class TestCursorCommandIntegration:
    """Tests for Cursor command integration (.prompt.md -> .md with frontmatter)."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / ".cursor").mkdir()
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def temp_project_no_cursor(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_skips_when_cursor_dir_missing(self, temp_project_no_cursor):
        """Opt-in: skip if .cursor/ does not exist."""
        pkg_info = _make_package(
            temp_project_no_cursor,
            {"test.prompt.md": "---\ndescription: Test\n---\n# Test"},
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        result = integrator.integrate_commands_for_target(
            KNOWN_TARGETS["cursor"], pkg_info, temp_project_no_cursor
        )
        assert result.files_integrated == 0
        assert not (temp_project_no_cursor / ".cursor" / "commands").exists()

    def test_deploys_prompts_to_cursor_commands(self, temp_project):
        """Deploy .prompt.md to .cursor/commands/<name>.md."""
        pkg_info = _make_package(
            temp_project,
            {"test.prompt.md": "---\ndescription: A test\n---\n# Test command"},
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        result = integrator.integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
        )
        assert result.files_integrated == 1
        target = temp_project / ".cursor" / "commands" / "test.md"
        assert target.exists()

    def test_deploys_multiple_prompts(self, temp_project):
        """Deploy multiple prompts to .cursor/commands/."""
        pkg_info = _make_package(
            temp_project,
            {
                "review.prompt.md": "---\ndescription: Review\n---\n# Review",
                "fix.prompt.md": "---\ndescription: Fix\n---\n# Fix",
            },
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        result = integrator.integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
        )
        assert result.files_integrated == 2

    def test_frontmatter_normalized_to_supported_subset(self, temp_project):
        """Frontmatter is normalized to the supported command subset.

        The shared command transformer keeps description, allowed-tools,
        model, and argument-hint but drops unknown keys (e.g. author,
        parameters).  This is intentional -- only the command-relevant
        subset is emitted.
        """
        pkg_info = _make_package(
            temp_project,
            {
                "cmd.prompt.md": (
                    "---\n"
                    "description: A command\n"
                    'allowed-tools: ["bash", "edit"]\n'
                    "model: claude-sonnet\n"
                    "argument-hint: file path\n"
                    "author: someone\n"
                    "custom-field: should-be-dropped\n"
                    "---\n"
                    "# Review Command\n"
                ),
            },
        )
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator.integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
        )

        target = temp_project / ".cursor" / "commands" / "cmd.md"
        assert target.exists()
        post = frontmatter.load(target)
        assert post.metadata["description"] == "A command"
        assert post.metadata["allowed-tools"] == ["bash", "edit"]
        assert post.metadata["model"] == "claude-sonnet"
        assert post.metadata["argument-hint"] == "file path"
        assert "author" not in post.metadata
        assert "custom-field" not in post.metadata

    def test_sync_removes_managed_commands(self, temp_project):
        """Sync removes manifest-tracked commands from .cursor/commands/."""
        cmds = temp_project / ".cursor" / "commands"
        cmds.mkdir(parents=True)
        (cmds / "review.md").write_text("# APM managed")
        (cmds / "custom.md").write_text("# User created")

        managed = {".cursor/commands/review.md"}

        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        result = integrator.sync_for_target(
            KNOWN_TARGETS["cursor"],
            None,
            temp_project,
            managed_files=managed,
        )

        assert result["files_removed"] == 1
        assert not (cmds / "review.md").exists()
        assert (cmds / "custom.md").exists()

    def test_sync_handles_missing_dir(self, temp_project_no_cursor):
        """Sync handles missing .cursor/commands/ gracefully."""
        integrator = CommandIntegrator()
        from apm_cli.integration.targets import KNOWN_TARGETS

        result = integrator.sync_for_target(KNOWN_TARGETS["cursor"], None, temp_project_no_cursor)
        assert result["files_removed"] == 0


class TestCursorCommandPanelFindings:
    """Regression tests for APM Review Panel findings on PR #1046.

    Covers dropped-frontmatter warnings, path traversal rejection,
    target-aware diagnostics, IDE consent surface, and skip notes.
    """

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / ".cursor").mkdir()
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def temp_project_no_cursor(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_dropped_frontmatter_keys_warn(self, temp_project):
        """Cursor-specific frontmatter (author, mcp, parameters) -> warn diag."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project,
            {
                "deploy.prompt.md": (
                    "---\n"
                    "description: Deploy command\n"
                    "author: alice\n"
                    "mcp:\n"
                    "  - github\n"
                    "parameters:\n"
                    "  - foo\n"
                    "---\n"
                    "Deploy the thing.\n"
                ),
            },
        )
        diag = DiagnosticCollector()
        CommandIntegrator().integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
            diagnostics=diag,
        )

        warnings = diag.by_category().get("warning", [])
        assert any(
            "frontmatter keys not supported" in w.message
            and "author" in w.message
            and "mcp" in w.message
            and "parameters" in w.message
            for w in warnings
        ), f"expected dropped-key warning, got: {[w.message for w in warnings]}"

    def test_path_traversal_filename_rejected(self, temp_project):
        """A package shipping a traversal-laden filename is skipped, not deployed."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        # Filename ``..prompt.md`` strips to base_name "." which is a
        # traversal segment that validate_path_segments must reject.
        pkg_info = _make_package(
            temp_project,
            {"..prompt.md": "---\ndescription: x\n---\nbody"},
        )
        diag = DiagnosticCollector()
        result = CommandIntegrator().integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
            diagnostics=diag,
        )

        warnings = diag.by_category().get("warning", [])
        assert any(
            "Rejected command filename" in w.message or "Rejected command target path" in w.message
            for w in warnings
        ), f"expected traversal rejection warning, got: {[w.message for w in warnings]}"
        # Nothing was written.
        assert result.files_integrated == 0
        assert not (temp_project / ".cursor" / "commands" / "..md").exists()

    def test_target_aware_info_message(self, temp_project):
        """Cursor install must NOT emit Claude-branded mapping diagnostic."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project,
            {
                "review.prompt.md": (
                    "---\ndescription: Review\ninput: [feature]\n---\nLook at ${input:feature}.\n"
                ),
            },
        )
        diag = DiagnosticCollector()
        CommandIntegrator().integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
            diagnostics=diag,
        )
        info_items = diag.by_category().get("info", [])
        assert all("Claude arguments" not in i.message for i in info_items), (
            "Cursor installs must not surface Claude-branded diagnostic strings"
        )
        assert any("Mapped input -> command arguments" in i.message for i in info_items)

    def test_skip_note_when_cursor_dir_missing(self, temp_project_no_cursor):
        """No .cursor/ -> info note explaining the skip (no silent skip)."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project_no_cursor,
            {"x.prompt.md": "---\ndescription: x\n---\nbody"},
        )
        diag = DiagnosticCollector()
        CommandIntegrator().integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project_no_cursor,
            diagnostics=diag,
        )
        info_items = diag.by_category().get("info", [])
        assert any(
            "Skipped .cursor/commands/" in i.message and "create a .cursor/" in i.message
            for i in info_items
        ), f"expected skip note, got: {[i.message for i in info_items]}"

    def test_passthrough_notice_suppressed_on_clean_install(self, temp_project):
        """Happy path: prompts use only the cross-tool subset -> NO passthrough notice.

        Regression for cli-logging-expert + devx-ux-expert convergent
        finding: the one-shot 'Cursor command files keep Claude-compatible
        frontmatter ...' info message was firing on every Cursor install,
        even when no frontmatter keys were dropped.  It must be suppressed
        when the batch had zero dropped keys.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project,
            {
                "review.prompt.md": (
                    '---\ndescription: Review\nallowed-tools: ["bash"]\n---\nReview the code.\n'
                ),
            },
        )
        diag = DiagnosticCollector()
        CommandIntegrator().integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
            diagnostics=diag,
        )
        info_items = diag.by_category().get("info", [])
        assert not any("cross-tool compatibility" in i.message for i in info_items), (
            "passthrough notice must not fire when no frontmatter keys "
            f"were dropped; got: {[i.message for i in info_items]}"
        )

    def test_passthrough_notice_emitted_when_any_file_drops_keys(self, temp_project):
        """When at least one file in the batch drops keys, the one-shot notice fires once."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project,
            {
                "clean.prompt.md": ("---\ndescription: Clean\n---\nClean.\n"),
                "lossy.prompt.md": ("---\ndescription: Lossy\nauthor: alice\n---\nLossy.\n"),
            },
        )
        diag = DiagnosticCollector()
        CommandIntegrator().integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
            diagnostics=diag,
        )
        info_items = diag.by_category().get("info", [])
        cross_tool_notices = [i for i in info_items if "cross-tool compatibility" in i.message]
        assert len(cross_tool_notices) == 1, (
            f"expected exactly one passthrough notice, got: "
            f"{[i.message for i in cross_tool_notices]}"
        )

    def test_dropped_keys_warn_uses_user_facing_wording(self, temp_project):
        """Regression: warn must not mention 'shared command transformer'.

        That phrase is internal implementation jargon; package authors
        have no mental model for it.  The warn must use target-name
        framing and a concrete supported-keys list.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project,
            {
                "x.prompt.md": ("---\ndescription: X\nauthor: alice\n---\nbody\n"),
            },
        )
        diag = DiagnosticCollector()
        CommandIntegrator().integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
            diagnostics=diag,
        )
        warnings = diag.by_category().get("warning", [])
        msgs = [w.message for w in warnings]
        assert msgs, "expected at least one dropped-keys warning"
        assert all("shared command transformer" not in m for m in msgs), (
            f"warn still uses internal jargon: {msgs}"
        )
        assert any("not supported for cursor commands" in m and "author" in m for m in msgs), (
            f"expected target-aware wording, got: {msgs}"
        )

    def test_critical_security_finding_blocks_write(self, temp_project):
        """Defense-in-depth: a critical post-transform finding must skip the write.

        Mirrors the pre-install BLOCK gate for the source files.  A
        package whose compiled command contains a critical hidden char
        (U+E041 tag char) must NOT be written to .cursor/commands/.
        The skip is also surfaced in result.files_skipped.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg_info = _make_package(
            temp_project,
            {
                "evil.prompt.md": ("---\ndescription: Evil\n---\nHidden tag\U000e0041char.\n"),
            },
        )
        diag = DiagnosticCollector()
        result = CommandIntegrator().integrate_commands_for_target(
            KNOWN_TARGETS["cursor"],
            pkg_info,
            temp_project,
            diagnostics=diag,
        )

        # File was NOT written -- defense-in-depth skip kicked in.
        assert not (temp_project / ".cursor" / "commands" / "evil.md").exists(), (
            "critical post-transform finding must skip the write"
        )
        assert result.files_integrated == 0
        assert result.files_skipped == 1

        # Critical security diagnostic was surfaced so the user sees why.
        sec_items = diag.by_category().get("security", [])
        assert any(i.severity == "critical" for i in sec_items), (
            f"expected critical security diagnostic, got: {[(i.severity, i.message) for i in sec_items]}"
        )
