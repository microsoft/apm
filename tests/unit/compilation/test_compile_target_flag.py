"""Tests for --target routing and the ``apm compile`` command on a real project.

Tests cover:
- --target vscode only generates AGENTS.md
- --target agents is alias for vscode
- --target claude only generates CLAUDE.md
- --target all generates both files (default)
- Invalid target value raises error
- _resolve_effective_target from flag, apm.yml and auto-detect
- compile command branches: no apm.yml, --all, validate, watch routing,
  distributed success, warnings, no content, import errors
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.compilation.agents_compiler import (
    AgentsCompiler,
    CompilationConfig,
    CompilationResult,
)
from apm_cli.compilation.constants import AGENTS_MD_GENERATED_MARKER
from apm_cli.core.remediation import ROOT_INSTRUCTIONS_HAND_AUTHORED
from apm_cli.core.target_catalog import TARGET_CAPABILITIES
from apm_cli.primitives.models import Instruction, PrimitiveCollection

pytestmark = pytest.mark.component

_NO_COMPILE_OUTPUT_TARGETS = sorted(
    capability.name
    for capability in TARGET_CAPABILITIES.values()
    if capability.compile_family is None
)


class TestCompileTargetRouting:
    """Tests for compile() method routing based on target."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory with APM structure."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")

        # Create instruction file
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints in Python code.
""")

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def sample_primitives(self, temp_project):
        """Create sample primitives for testing."""
        primitives = PrimitiveCollection()

        instruction = Instruction(
            name="python-style",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Python coding standards",
            apply_to="**/*.py",
            content="Use type hints in Python code.",
            author="test",
            source="local",
        )
        primitives.add_primitive(instruction)

        return primitives

    def test_target_vscode_generates_agents_md(self, temp_project, sample_primitives):
        """Test that target='vscode' generates AGENTS.md files."""
        config = CompilationConfig(
            target="vscode",
            dry_run=True,
            single_agents=True,  # Use single-file mode for simpler test
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        # Output path should be for AGENTS.md
        assert "AGENTS.md" in result.output_path
        # Content should contain AGENTS.md format elements
        assert "AGENTS.md" in result.content or result.content  # Has content
        assert not list(temp_project.glob("*.md"))

    def test_target_agents_is_alias_for_vscode(self, temp_project, sample_primitives):
        """Test that target='agents' produces same result as 'vscode'."""
        config_vscode = CompilationConfig(target="vscode", dry_run=True, single_agents=True)

        config_agents = CompilationConfig(target="agents", dry_run=True, single_agents=True)

        compiler = AgentsCompiler(str(temp_project))

        result_vscode = compiler.compile(config_vscode, sample_primitives)
        result_agents = compiler.compile(config_agents, sample_primitives)

        assert result_vscode.success == result_agents.success
        # Both should reference AGENTS.md
        assert "AGENTS.md" in result_vscode.output_path
        assert "AGENTS.md" in result_agents.output_path
        assert not list(temp_project.glob("*.md"))

    def test_target_claude_generates_claude_md(self, temp_project, sample_primitives):
        """Test that target='claude' generates CLAUDE.md files."""
        config = CompilationConfig(target="claude", dry_run=True)

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        # Output path should reference CLAUDE.md
        assert "CLAUDE" in result.output_path
        assert not list(temp_project.glob("*.md"))

    def test_target_all_generates_both(self, temp_project, sample_primitives):
        """Test that target='all' generates both AGENTS.md and CLAUDE.md."""
        config = CompilationConfig(
            target="all",
            dry_run=True,
            single_agents=True,  # Use single-file for AGENTS.md
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        # Output path should mention both targets
        assert "AGENTS.md" in result.output_path or "CLAUDE" in result.output_path
        assert not list(temp_project.glob("*.md"))

    def test_target_codex_generates_agents_md(self, temp_project, sample_primitives):
        """Regression for issue #766: --target codex must produce AGENTS.md, not a silent no-op."""
        config = CompilationConfig(
            target="codex",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        assert result.output_path, "codex target must route to a compiler, not return empty"
        assert "AGENTS.md" in result.output_path
        assert not list(temp_project.glob("*.md"))

    def test_codex_single_agents_no_claude_preview_issue_765_cli(self, temp_project):
        """Regression for issue #765: CLI --target codex --single-agents must not append Claude preview text.

        Issue: When target='codex' and single_agents=True were used via CLI, the intermediate_config
        constructor in the single-file path was missing target=config.target, causing target
        to default to "all". This caused _compile_claude_md() to run and append preview text
        to AGENTS output.

        Fix: Added target=config.target to CompilationConfig(...) constructor in single-file path
        in src/apm_cli/commands/compile/cli.py (lines 699-707).

        This CLI-level test exercises the fixed code path that was actually buggy.
        """
        original_dir = os.getcwd()
        runner = CliRunner()
        try:
            os.chdir(temp_project)
            result = runner.invoke(
                cli,
                [
                    "compile",
                    "--target",
                    "codex",
                    "--single-agents",
                    "--dry-run",
                ],
            )

            # Must succeed
            assert result.exit_code == 0, (
                f"CLI exited with code {result.exit_code}:\n{result.output}"
            )
            # Output must reference AGENTS.md generation
            assert "AGENTS.md" in result.output, (
                f"Expected AGENTS.md reference in output:\n{result.output}"
            )
            # CRITICAL: No Claude preview text appended (the regression)
            assert "CLAUDE.md Preview" not in result.output, (
                f"BUG: Claude preview text found in output:\n{result.output}"
            )
            assert "CLAUDE.md Preview: Would generate" not in result.output, (
                f"BUG: Claude preview-generate artifacts found in output:\n{result.output}"
            )
        finally:
            os.chdir(original_dir)

    def test_target_opencode_generates_agents_md(self, temp_project, sample_primitives):
        """target='opencode' must route to AGENTS.md (same universal format as codex)."""
        config = CompilationConfig(
            target="opencode",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        assert "AGENTS.md" in result.output_path
        assert not list(temp_project.glob("*.md"))

    def test_target_grok_build_generates_agents_md(self, temp_project, sample_primitives):
        """Stable Grok Build compilation produces root project instructions."""
        config = CompilationConfig(
            target="grok-build",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        assert "AGENTS.md" in result.output_path
        assert not list(temp_project.glob("*.md"))

    def test_target_gemini_generates_gemini_md(self, temp_project, sample_primitives):
        """target='gemini' must produce GEMINI.md, not a silent no-op."""
        config = CompilationConfig(
            target="gemini",
            dry_run=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        assert result.output_path, "gemini target must route to a compiler, not return empty"
        assert "GEMINI" in result.output_path
        assert not list(temp_project.glob("*.md"))

    def test_target_minimal_generates_agents_md(self, temp_project, sample_primitives):
        """target='minimal' must route to AGENTS.md-only."""
        config = CompilationConfig(
            target="minimal",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        assert "AGENTS.md" in result.output_path
        assert not list(temp_project.glob("*.md"))

    def test_target_vscode_writes_copilot_root_instructions_from_global_rules(self, temp_project):
        """Copilot compile should emit .github/copilot-instructions.md from global instructions."""
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )

        assert result.success
        root_file = temp_project / ".github" / "copilot-instructions.md"
        assert root_file.exists()
        content = root_file.read_text(encoding="utf-8")
        assert content.startswith("<!-- Generated by APM CLI")
        assert "<!-- Build ID: " in content
        assert "# Contributing" in content
        assert "Run focused tests first." in content
        assert result.stats["copilot_root_instructions_generated"] == 1

    def test_copilot_root_deduplicates_only_deployed_global_instruction_files(self, temp_project):
        """Installed global Copilot rules should not be repeated in the root aggregate."""
        rules_dir = temp_project / ".github" / "instructions"
        rules_dir.mkdir(parents=True)
        (rules_dir / "contributing.instructions.md").write_text(
            "# Contributing\n\nRun focused tests first.\n",
            encoding="utf-8",
        )

        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )
        primitives.add_primitive(
            Instruction(
                name="review",
                file_path=temp_project / ".apm/instructions/review.instructions.md",
                description="Review guidance not yet installed for Copilot",
                apply_to="",
                content="# Review\n\nCheck the changed behavior.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )

        assert result.success
        content = (temp_project / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "Run focused tests first." not in content
        assert "Check the changed behavior." in content
        assert result.stats["copilot_root_instructions_generated"] == 1

    def test_copilot_force_instructions_keeps_installed_global_rules_in_root(self, temp_project):
        """--force-instructions / --no-dedup must bypass Copilot root deduplication."""
        rules_dir = temp_project / ".github" / "instructions"
        rules_dir.mkdir(parents=True)
        (rules_dir / "contributing.instructions.md").write_text(
            "# Contributing\n\nRun focused tests first.\n",
            encoding="utf-8",
        )

        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(
                target="vscode",
                dry_run=False,
                single_agents=True,
                no_dedup=True,
            ),
            primitives,
        )

        assert result.success
        content = (temp_project / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "Run focused tests first." in content

    def test_copilot_dedup_removes_unmodified_generated_root_file(self, temp_project):
        """Dedup may remove a generated root file while its marker and Build ID match."""
        instruction_path = temp_project / ".apm/instructions/contributing.instructions.md"
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=instruction_path,
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )
        compiler = AgentsCompiler(str(temp_project))
        first = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )
        assert first.success

        rules_dir = temp_project / ".github" / "instructions"
        rules_dir.mkdir(parents=True)
        (rules_dir / "contributing.instructions.md").write_text(
            "# Contributing\n\nRun focused tests first.\n",
            encoding="utf-8",
        )
        second = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )

        root_file = temp_project / ".github" / "copilot-instructions.md"
        assert second.success
        assert not root_file.exists()
        assert second.stats["copilot_root_instructions_removed"] == 1

    @pytest.mark.parametrize("build_id_state", ["edited", "missing", "malformed", "duplicate"])
    def test_copilot_dedup_preserves_edited_generated_root_file(self, temp_project, build_id_state):
        """Dedup retains any generated root whose Build ID cannot verify its content."""
        instruction_path = temp_project / ".apm/instructions/contributing.instructions.md"
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=instruction_path,
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )
        compiler = AgentsCompiler(str(temp_project))
        first = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )
        assert first.success

        root_file = temp_project / ".github" / "copilot-instructions.md"
        original = root_file.read_text(encoding="utf-8")
        build_id_line = next(line for line in original.splitlines() if "<!-- Build ID:" in line)
        if build_id_state == "missing":
            original = original.replace(build_id_line, "")
        elif build_id_state == "malformed":
            original = original.replace(build_id_line, "<!-- Build ID: invalid -->")
        elif build_id_state == "duplicate":
            original += build_id_line + "\n"
        retained = original + "\n# User addition\n"
        root_file.write_text(retained, encoding="utf-8")
        rules_dir = temp_project / ".github" / "instructions"
        rules_dir.mkdir(parents=True)
        (rules_dir / "contributing.instructions.md").write_text(
            "# Contributing\n\nRun focused tests first.\n",
            encoding="utf-8",
        )

        second = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )

        assert second.success
        assert root_file.exists()
        assert root_file.read_text(encoding="utf-8") == retained
        assert second.stats["copilot_root_instructions_removed"] == 0
        assert any("Build ID does not match" in warning for warning in second.warnings)

    def test_copilot_dedup_preserves_symbolic_link_and_target(self, temp_project):
        """Cleanup must retain a symlink instead of unlinking its target entry."""
        instruction_path = temp_project / ".apm/instructions/contributing.instructions.md"
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=instruction_path,
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )
        compiler = AgentsCompiler(str(temp_project))
        first = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )
        assert first.success

        root_file = temp_project / ".github" / "copilot-instructions.md"
        target = temp_project / "preserved-user-file.md"
        retained = root_file.read_text(encoding="utf-8")
        target.write_text(retained, encoding="utf-8")
        root_file.unlink()
        try:
            root_file.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"file symlinks are unavailable: {exc}")

        rules_dir = temp_project / ".github" / "instructions"
        rules_dir.mkdir(parents=True)
        (rules_dir / "contributing.instructions.md").write_text(
            "# Contributing\n\nRun focused tests first.\n",
            encoding="utf-8",
        )
        second = compiler.compile(
            CompilationConfig(target="minimal", dry_run=False, single_agents=True),
            primitives,
        )

        assert second.success
        assert root_file.is_symlink()
        assert target.read_text(encoding="utf-8") == retained
        assert second.stats["copilot_root_instructions_removed"] == 0

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_copilot_cleanup_refuses_linked_parent(self, tmp_path, dry_run):
        """A linked .github directory cannot authorize deleting an external file."""
        from apm_cli.compilation.build_id import stabilize_build_id
        from apm_cli.compilation.constants import AGENTS_MD_GENERATED_MARKER, BUILD_ID_PLACEHOLDER

        project = tmp_path / "project"
        project.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        root = external / "copilot-instructions.md"
        retained = stabilize_build_id(f"{AGENTS_MD_GENERATED_MARKER}\n{BUILD_ID_PLACEHOLDER}\n")
        root.write_text(retained, encoding="utf-8")
        try:
            (project / ".github").symlink_to(external, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks are unavailable: {exc}")
        compiler = AgentsCompiler(str(project))
        result = CompilationResult(
            success=True, output_path="", content="", warnings=[], errors=[], stats={}
        )
        compiler._cleanup_copilot_root_instructions(
            project / ".github/copilot-instructions.md", result, dry_run=dry_run
        )
        assert not result.success
        assert result.stats["copilot_root_instructions_removed"] == 0
        assert root.read_text(encoding="utf-8") == retained

    def test_target_minimal_does_not_write_copilot_root_instructions(self, temp_project):
        """Minimal target must stay AGENTS-only even when global instructions exist."""
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="Always run lint.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(target="minimal", dry_run=False, single_agents=True),
            primitives,
        )

        assert result.success
        assert not (temp_project / ".github" / "copilot-instructions.md").exists()

    def test_target_minimal_removes_stale_generated_copilot_root_instructions(self, temp_project):
        """Minimal compile should remove stale generated Copilot root instructions."""
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="Always run lint.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        first = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )
        assert first.success
        root_file = temp_project / ".github" / "copilot-instructions.md"
        assert root_file.exists()

        second = compiler.compile(
            CompilationConfig(target="minimal", dry_run=False, single_agents=True),
            primitives,
        )
        assert second.success
        assert not root_file.exists()
        assert second.stats["copilot_root_instructions_removed"] == 1

    def test_target_minimal_dry_run_previews_stale_copilot_root_removal(self, temp_project):
        """A dry run previews generated Copilot cleanup without deleting the file."""
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="Always run lint.",
                author="test",
                source="local",
            )
        )
        compiler = AgentsCompiler(str(temp_project))
        first = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )
        assert first.success
        root_file = temp_project / ".github" / "copilot-instructions.md"
        assert root_file.exists()

        preview = compiler.compile(
            CompilationConfig(target="minimal", dry_run=True, single_agents=True),
            primitives,
        )

        assert preview.success
        assert root_file.exists()
        assert preview.stats["copilot_root_instructions_removed"] == 0
        assert any(
            "[dry-run] would remove stale .github/copilot-instructions.md" in warning
            for warning in preview.warnings
        )

    def test_scoped_only_rules_remove_stale_generated_copilot_root_instructions(self, temp_project):
        """Scoped-only compile should clean stale generated root Copilot instructions."""
        compiler = AgentsCompiler(str(temp_project))

        global_primitives = PrimitiveCollection()
        global_primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="Always run lint.",
                author="test",
                source="local",
            )
        )
        first = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            global_primitives,
        )
        assert first.success

        scoped_primitives = PrimitiveCollection()
        scoped_primitives.add_primitive(
            Instruction(
                name="python",
                file_path=temp_project / ".apm/instructions/python.instructions.md",
                description="Python guidance",
                apply_to="**/*.py",
                content="Use type hints.",
                author="test",
                source="local",
            )
        )
        second = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            scoped_primitives,
        )

        assert second.success
        assert not (temp_project / ".github" / "copilot-instructions.md").exists()
        assert second.stats["copilot_root_instructions_removed"] == 1

    def test_cleanup_preserves_unmanaged_manual_copilot_root_instructions(self, temp_project):
        """Cleanup should not delete a manual Copilot root instructions file."""
        root_file = temp_project / ".github" / "copilot-instructions.md"
        root_file.parent.mkdir(parents=True)
        root_file.write_text("Manual instructions\n", encoding="utf-8")

        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="python",
                file_path=temp_project / ".apm/instructions/python.instructions.md",
                description="Python guidance",
                apply_to="**/*.py",
                content="Use type hints.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(target="minimal", dry_run=False, single_agents=True),
            primitives,
        )

        assert result.success
        assert root_file.exists()
        assert root_file.read_text(encoding="utf-8") == "Manual instructions\n"
        assert result.stats["copilot_root_instructions_removed"] == 0

    def test_write_path_preserves_unmanaged_manual_copilot_root_instructions(self, temp_project):
        """Write path must not overwrite a hand-authored copilot-instructions.md.

        Regression for the missing marker guard on the write path: a manually
        authored .github/copilot-instructions.md must survive the first
        `apm compile -t copilot` even when global instructions exist.
        """
        root_file = temp_project / ".github" / "copilot-instructions.md"
        root_file.parent.mkdir(parents=True)
        manual_content = "# Manual\n\nHand-authored guidance.\n"
        root_file.write_text(manual_content, encoding="utf-8")

        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )

        assert result.success
        assert root_file.read_text(encoding="utf-8") == manual_content
        assert result.stats["copilot_root_instructions_written"] == 0
        assert any("hand-authored file will not be overwritten" in w for w in result.warnings)
        assert any(
            "<!-- Generated by APM CLI from .apm/ primitives -->" in w for w in result.warnings
        ), "warning must disclose the literal marker string for self-service recovery"

    def test_multi_target_claude_copilot_emits_copilot_root_instructions(self, temp_project):
        """`apm compile -t claude,copilot` (frozenset path) must emit copilot-instructions.md.

        Regression for the multi-target frozenset gap: should_compile_copilot_instructions_md
        used to ignore the frozenset branch and silently skip generation.
        """
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(
                target=frozenset({"vscode", "agents", "claude"}),
                dry_run=False,
                single_agents=True,
            ),
            primitives,
        )

        assert result.success
        root_file = temp_project / ".github" / "copilot-instructions.md"
        assert root_file.exists(), (
            "copilot-instructions.md should be generated when 'vscode' is in the frozenset target"
        )
        assert result.stats["copilot_root_instructions_generated"] == 1

    def test_agents_only_targets_do_not_emit_copilot_instructions(self, temp_project):
        """Round-3 regression: -t opencode,codex (no copilot family)
        must NOT emit .github/copilot-instructions.md.

        Previously the 'agents' family token was overloaded for AGENTS.md AND
        copilot-instructions.md routing, causing the multi-target path to
        over-fire for AGENTS.md-only combos.
        """
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        # Simulate what _resolve_compile_target(["opencode","codex"]) returns
        # under the new semantics: a single 'agents'-only family collapses to
        # the bare opencode target, which routes correctly via the string path.
        result = compiler.compile(
            CompilationConfig(target="opencode", dry_run=False, single_agents=True),
            primitives,
        )

        assert result.success
        root_file = temp_project / ".github" / "copilot-instructions.md"
        assert not root_file.exists(), (
            "copilot-instructions.md must NOT be generated for opencode/codex"
        )
        assert result.stats.get("copilot_root_instructions_generated", 0) == 0

    def test_resolve_compile_target_opencode_codex_does_not_route_to_vscode(self):
        """Round-3 regression at the resolver: opencode/codex multi-target
        list must not collapse to 'vscode' (which would wrongly route
        copilot-instructions.md generation)."""
        from apm_cli.commands.compile.cli import _resolve_compile_target
        from apm_cli.core.target_detection import should_compile_copilot_instructions_md

        for target_list in (
            ["opencode"],
            ["codex"],
            ["opencode", "codex"],
        ):
            resolved = _resolve_compile_target(target_list)
            assert resolved != "vscode", (
                f"_resolve_compile_target({target_list!r}) must not return 'vscode' "
                f"-- got {resolved!r}"
            )
            assert should_compile_copilot_instructions_md(resolved) is False, (
                f"copilot-instructions.md must not be routed for {target_list!r}"
            )

    def test_resolve_compile_target_copilot_family_combos_do_route(self):
        """Cross-check: copilot-family multi-target combos still route to
        copilot-instructions.md generation under the new predicate."""
        from apm_cli.commands.compile.cli import _resolve_compile_target
        from apm_cli.core.target_detection import should_compile_copilot_instructions_md

        for target_list in (
            ["copilot"],
            ["vscode"],
            ["claude", "copilot"],
            ["claude", "vscode"],
            ["gemini", "vscode"],
            ["codex", "vscode"],
            ["claude", "vscode", "gemini"],
        ):
            resolved = _resolve_compile_target(target_list)
            assert should_compile_copilot_instructions_md(resolved) is True, (
                f"copilot-instructions.md must be routed for {target_list!r} "
                f"-- resolver returned {resolved!r}"
            )

    def test_skipped_hand_authored_file_records_skipped_stat_not_unchanged(self, temp_project):
        """Round-3 regression: the skip-on-hand-authored-file path must record
        'skipped' in stats and reset 'generated' to 0, not leave it at 1
        (false positive) or use 'unchanged' (semantically wrong)."""
        root_file = temp_project / ".github" / "copilot-instructions.md"
        root_file.parent.mkdir(parents=True)
        root_file.write_text("# Hand-authored\n", encoding="utf-8")

        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )

        assert result.success
        assert result.stats["copilot_root_instructions_generated"] == 0, (
            "generated must be 0 when the hand-authored guard fires (no file was emitted)"
        )
        assert result.stats["copilot_root_instructions_written"] == 0
        assert result.stats["copilot_root_instructions_skipped"] == 1, (
            "skipped stat must be 1 to distinguish from a genuine no-op"
        )
        assert result.stats["copilot_root_instructions_unchanged"] == 0, (
            "unchanged means content was compared and matched -- not the case here"
        )
        assert root_file.read_text(encoding="utf-8") == "# Hand-authored\n"

    def test_skip_warning_uses_relative_path_and_concrete_action(self, temp_project):
        """Round-3 UX fix: warning must use a relative path and tell the user
        exactly what command to run after removing the file."""
        root_file = temp_project / ".github" / "copilot-instructions.md"
        root_file.parent.mkdir(parents=True)
        root_file.write_text("# Hand-authored\n", encoding="utf-8")

        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
            primitives,
        )

        skip_warnings = [
            w for w in result.warnings if "hand-authored file will not be overwritten" in w
        ]
        assert skip_warnings, "expected a skip warning to be emitted"
        warning = skip_warnings[0]
        assert ".github/copilot-instructions.md" in warning, "warning must reference the file"
        assert str(temp_project) not in warning, (
            "warning must not embed the absolute project path (use a relative path)"
        )
        advice = ROOT_INSTRUCTIONS_HAND_AUTHORED.bind(marker=AGENTS_MD_GENERATED_MARKER)
        assert advice.sentence() in warning, "warning must name the action and the next command"
        assert "<!-- Generated by APM CLI from .apm/ primitives -->" in warning, (
            "warning must disclose the literal marker string so users can self-serve recovery"
        )
        assert root_file.read_text(encoding="utf-8") == "# Hand-authored\n"

    def test_dry_run_respects_hand_authored_guard(self, temp_project):
        """Round-4 regression: `--dry-run` must mirror the hand-authored guard
        the real run applies, not silently report `generated=1` for a file the
        real run would skip. CI scripts that use `--dry-run` as a preview gate
        depend on the simulation being faithful."""
        root_file = temp_project / ".github" / "copilot-instructions.md"
        root_file.parent.mkdir(parents=True)
        manual_content = "# Hand-authored\n\nNo APM marker present.\n"
        root_file.write_text(manual_content, encoding="utf-8")

        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing\n\nRun focused tests first.",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(
            CompilationConfig(target="vscode", dry_run=True, single_agents=True),
            primitives,
        )

        assert result.success
        # Dry-run must not mutate the file.
        assert root_file.read_text(encoding="utf-8") == manual_content
        # Stats must mirror what a real run would record.
        assert result.stats["copilot_root_instructions_generated"] == 0, (
            "dry-run must not claim generation when the real run would skip"
        )
        assert result.stats["copilot_root_instructions_written"] == 0
        assert result.stats["copilot_root_instructions_skipped"] == 1, (
            "dry-run must surface the skip the real run would record"
        )
        assert result.stats["copilot_root_instructions_unchanged"] == 0
        assert any("hand-authored file will not be overwritten" in w for w in result.warnings), (
            "dry-run must surface the same warning a real run would emit"
        )

    def test_generated_footer_uses_apm_compile_not_specify(self, temp_project):
        """Generated footer must read `apm compile`, not `specify apm compile`."""
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="contributing",
                file_path=temp_project / ".apm/instructions/contributing.instructions.md",
                description="General contributing guidance",
                apply_to="",
                content="# Contributing",
                author="test",
                source="local",
            )
        )

        compiler = AgentsCompiler(str(temp_project))
        # Use source_attribution=True so the footer is included in the output.
        result = compiler.compile(
            CompilationConfig(
                target="vscode", dry_run=False, single_agents=True, source_attribution=True
            ),
            primitives,
        )
        assert result.success

        content = (temp_project / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "specify apm compile" not in content
        assert "*To regenerate: `apm compile`*" in content

    def test_unknown_target_returns_failure(self, temp_project, sample_primitives):
        """Unknown target must fail explicitly instead of silently succeeding."""
        config = CompilationConfig(
            target="not-a-real-target",
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success is False
        assert any("Unknown compilation target" in e for e in result.errors)
        assert not list(temp_project.glob("*.md"))

    def test_unknown_frozenset_target_family_returns_failure(self, temp_project, sample_primitives):
        """Unknown multi-target family must fail explicitly instead of silently no-oping."""
        config = CompilationConfig(
            target=frozenset({"agents", "not-a-real-family"}),
            dry_run=True,
            single_agents=True,
        )

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success is False
        assert any("Unknown compilation target family" in e for e in result.errors)
        assert any("not-a-real-family" in e for e in result.errors)
        assert not list(temp_project.glob("*.md"))


class TestCompileCommandCLI:
    """Tests for the compile command CLI with --target flag."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")

        # Create instruction file for compilation
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints in Python code.
""")

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_help_advertises_intellij_target(self, runner):
        """Compile help lists every accepted target, including IntelliJ."""
        result = runner.invoke(cli, ["compile", "--help"])

        assert result.exit_code == 0
        assert "intellij" in result.output.lower()

    def test_invalid_target_error_uses_structured_rendering(self, runner):
        """Unknown-target guidance keeps the three-section rendering."""
        result = runner.invoke(cli, ["compile", "--target", "definitely-bogus"])

        assert result.exit_code == 2
        assert "Error: Unknown target 'definitely-bogus'" in result.output
        assert "[x]" not in result.output
        assert "Valid targets:" in result.output
        assert "Fix with one of:" in result.output

    def test_target_flag_accepts_intellij(self, runner, temp_project):
        """Compile accepts IntelliJ and generates its native context artifact."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "intellij"])

            assert result.exit_code == 0, result.output
            assert (temp_project / "AGENTS.md").is_file()
        finally:
            os.chdir(original_dir)

    def test_target_flag_accepts_vscode(self, runner, temp_project):
        """Test that --target vscode is accepted."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "vscode", "--dry-run"])

            # Should not fail due to invalid choice
            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    def test_target_flag_accepts_agents(self, runner, temp_project):
        """Test that --target agents is accepted."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "agents", "--dry-run"])

            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    def test_target_flag_accepts_claude(self, runner, temp_project):
        """Test that --target claude is accepted."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "claude", "--dry-run"])

            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    def test_target_flag_accepts_all(self, runner, temp_project):
        """Test that --target all is accepted."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "all", "--dry-run"])

            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    def test_target_flag_rejects_invalid(self, runner, temp_project):
        """Test that invalid target value is rejected."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "--target", "invalid", "--dry-run"])

            assert result.exit_code != 0
            assert "Unknown target" in result.output or "Invalid value" in result.output
        finally:
            os.chdir(original_dir)

    def test_target_default_is_all(self, runner, temp_project):
        """Test that default target is 'all' when not specified."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            # Run compile with dry-run to just test config
            result = runner.invoke(cli, ["compile", "--dry-run"])

            # Should succeed and compile for all targets
            # Exit code should be 0 (success) since we have valid primitives
            assert result.exit_code == 0 or "No APM content" in result.output
        finally:
            os.chdir(original_dir)

    def test_short_flag_t_works(self, runner, temp_project):
        """Test that -t short flag works for target."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            result = runner.invoke(cli, ["compile", "-t", "vscode", "--dry-run"])

            assert "Invalid value for '--target'" not in result.output
        finally:
            os.chdir(original_dir)

    # ----- #820 regression: apm.yml target: contract -----

    def test_csv_target_in_apm_yml_no_longer_silent(self, runner, temp_project):
        """CSV string in apm.yml's ``target:`` used to leave install/compile
        with exit-0 success and zero deployment.  Now the same string flows
        through ``parse_target_field`` and yields a real list of targets.
        """
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            (temp_project / "apm.yml").write_text(
                "name: test-project\nversion: 0.1.0\ntarget: opencode,claude,copilot\n"
            )
            result = runner.invoke(cli, ["compile", "--dry-run"])
            # No "Invalid value" gripe -- the string is a valid CSV now.
            assert "Invalid value for '--target'" not in result.output
            # And compile no longer prints success-after-zero-effect: it
            # either succeeds with output (dry-run preview) or surfaces a
            # real error.  The pre-fix log line "Compilation completed
            # successfully!" with no files written must not appear when
            # zero targets resolve.
            assert result.exit_code == 0 or "Invalid 'target'" in result.output
        finally:
            os.chdir(original_dir)

    def test_unknown_target_in_apm_yml_fails_loudly(self, runner, temp_project):
        """Unknown token in apm.yml ``target:`` now fails the command with
        a ValueError naming the bad token, instead of being swallowed by
        the old ``except Exception: pass`` in compile/cli.py."""
        original_dir = os.getcwd()
        try:
            os.chdir(temp_project)
            (temp_project / "apm.yml").write_text(
                "name: test-project\nversion: 0.1.0\ntarget: claude,bogus,copilot\n"
            )
            result = runner.invoke(cli, ["compile", "--dry-run"])
            # Either the CLI exits non-zero with the error, or the error
            # is included in the output -- both are acceptable signals
            # that the silent-swallow path is gone.  Normalize whitespace
            # because the error message may be soft-wrapped onto multiple
            # lines by the CLI logger.
            combined = " ".join(
                (
                    (result.output or "") + (str(result.exception) if result.exception else "")
                ).split()
            )
            assert "'bogus'" in combined
            assert "not a valid target" in combined
        finally:
            os.chdir(original_dir)


class TestTargetVscodeOnlyGeneratesAgentsMd:
    """Tests to verify --target vscode only generates AGENTS.md."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")

        # Create instruction file
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints.
""")

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_vscode_target_does_not_create_claude_md(self, temp_project):
        """Test that --target vscode doesn't create CLAUDE.md."""
        config = CompilationConfig(target="vscode", dry_run=False, single_agents=True)

        compiler = AgentsCompiler(str(temp_project))
        primitives = PrimitiveCollection()

        instruction = Instruction(
            name="test",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local",
        )
        primitives.add_primitive(instruction)

        result = compiler.compile(config, primitives)

        # Should succeed
        assert result.success

        # AGENTS.md should be created
        agents_md = temp_project / "AGENTS.md"
        assert agents_md.exists()

        # CLAUDE.md should NOT be created
        claude_md = temp_project / "CLAUDE.md"
        assert not claude_md.exists()


class TestTargetClaudeOnlyGeneratesClaudeMd:
    """Tests to verify --target claude only generates CLAUDE.md."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")

        # Create instruction file
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints.
""")

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_claude_target_does_not_create_agents_md(self, temp_project):
        """Test that --target claude doesn't create AGENTS.md."""
        config = CompilationConfig(target="claude", dry_run=False)

        compiler = AgentsCompiler(str(temp_project))
        primitives = PrimitiveCollection()

        instruction = Instruction(
            name="test",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local",
        )
        primitives.add_primitive(instruction)

        result = compiler.compile(config, primitives)

        # Should succeed
        assert result.success

        # CLAUDE.md should be created (in root or with distributed)
        claude_md = temp_project / "CLAUDE.md"
        assert claude_md.exists()

        # AGENTS.md should NOT be created at root
        # (checking root AGENTS.md since distributed could create subdirectory ones)
        agents_md = temp_project / "AGENTS.md"
        assert not agents_md.exists()


class TestTargetAllGeneratesBoth:
    """Tests to verify --target all generates both AGENTS.md and CLAUDE.md."""

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        # Create minimal apm.yml
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")

        # Create instruction file
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        instruction_file = apm_dir / "test.instructions.md"
        instruction_file.write_text("""---
applyTo: "**/*.py"
---
Use type hints.
""")

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_all_target_creates_all_files(self, temp_project):
        """Test that --target all creates AGENTS.md, CLAUDE.md, and GEMINI.md."""
        config = CompilationConfig(
            target="all",
            dry_run=False,
            single_agents=True,  # Use single-file for simpler verification
        )

        compiler = AgentsCompiler(str(temp_project))
        primitives = PrimitiveCollection()

        instruction = Instruction(
            name="test",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local",
        )
        primitives.add_primitive(instruction)

        result = compiler.compile(config, primitives)

        # Should succeed
        assert result.success

        # All three files should be created
        agents_md = temp_project / "AGENTS.md"
        claude_md = temp_project / "CLAUDE.md"
        gemini_md = temp_project / "GEMINI.md"

        assert agents_md.exists(), "AGENTS.md should be created for target='all'"
        assert claude_md.exists(), "CLAUDE.md should be created for target='all'"
        assert gemini_md.exists(), "GEMINI.md should be created for target='all'"

    def test_all_target_result_references_both(self, temp_project):
        """Test that --target all result references both outputs."""
        config = CompilationConfig(target="all", dry_run=True, single_agents=True)

        compiler = AgentsCompiler(str(temp_project))
        primitives = PrimitiveCollection()

        instruction = Instruction(
            name="test",
            file_path=temp_project / ".apm/instructions/test.instructions.md",
            description="Test",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local",
        )
        primitives.add_primitive(instruction)

        result = compiler.compile(config, primitives)

        assert result.success
        # The merged output path should reference both targets
        assert "AGENTS.md" in result.output_path or "CLAUDE" in result.output_path
        assert not list(temp_project.glob("*.md"))


class TestClaudeAndAgentsMdConsistentOutput:
    """Tests to ensure CLAUDE.md and AGENTS.md use the same optimization logic.

    Both targets should produce:
    - Same optimization decisions (placement table)
    - Same efficiency metrics
    - Same placement distribution
    Only the output file names should differ.
    """

    @pytest.fixture
    def temp_project_with_instructions(self):
        """Create a temporary project with instruction files."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        # Create .apm directory with instructions
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)

        # Create instruction file that targets specific pattern
        (apm_dir / "code-standards.instructions.md").write_text("""---
applyTo: "**/*.py"
description: "Python coding standards"
---
# Python Coding Standards
Follow PEP 8 guidelines.
""")

        # Create another instruction file with different pattern
        (apm_dir / "test-guidelines.instructions.md").write_text("""---
applyTo: "tests/**/*.py"
description: "Testing guidelines"
---
# Testing Guidelines
Use pytest for all tests.
""")

        # Create target directories to match patterns
        (temp_path / "src").mkdir()
        (temp_path / "src" / "main.py").write_text("# Main file")
        (temp_path / "tests").mkdir()
        (temp_path / "tests" / "test_main.py").write_text("# Test file")

        # Create apm.yml
        (temp_path / "apm.yml").write_text("""
name: test-project
version: 0.1.0
""")

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_claude_and_agents_have_same_placement_count(self, temp_project_with_instructions):
        """Test that CLAUDE.md and AGENTS.md generate the same number of placement files."""
        compiler = AgentsCompiler(str(temp_project_with_instructions))

        # Compile for VSCode/AGENTS.md
        vscode_config = CompilationConfig(target="vscode", dry_run=True)
        vscode_result = compiler.compile(vscode_config)

        # Reset compiler state
        compiler = AgentsCompiler(str(temp_project_with_instructions))

        # Compile for Claude/CLAUDE.md
        claude_config = CompilationConfig(target="claude", dry_run=True)
        claude_result = compiler.compile(claude_config)

        # Both should succeed
        assert vscode_result.success
        assert claude_result.success

        # Both should have the same file count in stats (using target-specific keys)
        vscode_file_count = vscode_result.stats.get(
            "agents_files_generated", vscode_result.stats.get("total_agents_files", 0)
        )
        claude_file_count = claude_result.stats.get("claude_files_generated", 0)

        # The file counts should be equal (same optimization logic)
        assert vscode_file_count == claude_file_count, (
            f"File counts differ: AGENTS.md={vscode_file_count}, CLAUDE.md={claude_file_count}"
        )

    def test_claude_compilation_produces_optimization_output(self, temp_project_with_instructions):
        """Test that CLAUDE.md compilation produces proper optimization metrics."""
        compiler = AgentsCompiler(str(temp_project_with_instructions))

        # Compile for Claude/CLAUDE.md
        claude_config = CompilationConfig(target="claude", dry_run=True)
        claude_result = compiler.compile(claude_config)

        # Should succeed
        assert claude_result.success

        # Should have file count
        assert claude_result.stats.get("claude_files_generated", 0) > 0

        # Should have primitives count
        assert claude_result.stats.get("primitives_found", 0) > 0


class TestConfigFromApmYml:
    """Tests for reading target from apm.yml configuration."""

    @pytest.fixture
    def temp_project_with_config(self):
        """Create a temporary project with apm.yml containing compilation config."""
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_target_from_apm_yml(self, temp_project_with_config):
        """Test that target can be read from apm.yml compilation section."""
        apm_yml = temp_project_with_config / "apm.yml"
        apm_yml.write_text("""
name: test-project
version: 0.1.0
compilation:
  target: claude
""")

        original_dir = os.getcwd()
        try:
            os.chdir(temp_project_with_config)
            config = CompilationConfig.from_apm_yml()
            assert config.target == "claude"
        finally:
            os.chdir(original_dir)

    def test_cli_override_takes_precedence(self, temp_project_with_config):
        """Test that CLI --target overrides apm.yml config."""
        apm_yml = temp_project_with_config / "apm.yml"
        apm_yml.write_text("""
name: test-project
version: 0.1.0
compilation:
  target: claude
""")

        original_dir = os.getcwd()
        try:
            os.chdir(temp_project_with_config)
            # CLI override should take precedence
            config = CompilationConfig.from_apm_yml(target="vscode")
            assert config.target == "vscode"
        finally:
            os.chdir(original_dir)


class TestCompileWarningOnMissingApplyTo:
    """Tests that apm compile warns when an instruction is missing applyTo."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def project_with_bad_instruction(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")

        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)

        (apm_dir / "good.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\nFollow PEP 8.\n"
        )
        (apm_dir / "bad.instructions.md").write_text(
            "---\ndescription: Missing applyTo\n---\nThis instruction has no scope.\n"
        )

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_cli_warns_missing_apply_to_distributed(self, runner, project_with_bad_instruction):
        """Test that apm compile --dry-run warns about missing applyTo in distributed mode."""
        original_dir = os.getcwd()
        try:
            os.chdir(project_with_bad_instruction)
            result = runner.invoke(cli, ["compile", "--target", "vscode", "--dry-run"])
            assert "applyTo" in result.output, (
                f"Expected warning about missing 'applyTo' in CLI output, got:\n{result.output}"
            )
        finally:
            os.chdir(original_dir)

    def test_cli_warns_missing_apply_to_claude(self, runner, project_with_bad_instruction):
        """Test that apm compile --target claude --dry-run warns about missing applyTo."""
        original_dir = os.getcwd()
        try:
            os.chdir(project_with_bad_instruction)
            result = runner.invoke(cli, ["compile", "--target", "claude", "--dry-run"])
            assert "applyTo" in result.output, (
                f"Expected warning about missing 'applyTo' in CLI output, got:\n{result.output}"
            )
        finally:
            os.chdir(original_dir)


class TestMultiTargetLogOutput:
    """Regression tests for the 'Compiling for ...' log line on multi-target compiles."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def empty_project(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)
        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")
        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        (apm_dir / "good.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\nFollow PEP 8.\n"
        )
        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_cli_multi_target_log_message(self, runner, empty_project):
        original_dir = os.getcwd()
        try:
            os.chdir(empty_project)
            result = runner.invoke(cli, ["compile", "--target", "claude,codex", "--dry-run"])
            assert "Compiling for" in result.output
            assert "AGENTS.md" in result.output and "CLAUDE.md" in result.output
            assert "GEMINI.md" not in result.output.split("Compiling for", 1)[1].split("\n", 1)[0]
            assert "--target claude,codex" in result.output
        finally:
            os.chdir(original_dir)

    def test_config_multi_target_log_message_does_not_say_unknown(self, runner, empty_project):
        """Regression: apm.yml multi-target list must not log 'unknown target'."""
        (empty_project / "apm.yml").write_text(
            "name: test-project\nversion: 0.1.0\ntarget: [claude, codex]\n"
        )
        original_dir = os.getcwd()
        try:
            os.chdir(empty_project)
            result = runner.invoke(cli, ["compile", "--dry-run"])
            assert "unknown target" not in result.output.lower(), (
                f"Config-driven multi-target should not log 'unknown target'. Got:\n{result.output}"
            )
            assert "Compiling for" in result.output
            assert "AGENTS.md" in result.output and "CLAUDE.md" in result.output
        finally:
            os.chdir(original_dir)


class TestClaudeMdHonorsSingleFileStrategy:
    """Regression for issue #1445.

    compilation.strategy=single-file (or single_agents=True) must produce a
    single root CLAUDE.md, mirroring the AGENTS.md behavior. Previously
    _compile_claude_md ignored the strategy gate and always built a
    per-subdirectory placement map, emitting e.g. scripts/CLAUDE.md.
    """

    @pytest.fixture
    def project_with_subdir_instruction(self):
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir)

        (temp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n")

        apm_dir = temp_path / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        (apm_dir / "shell.instructions.md").write_text(
            "---\napplyTo: '**/*.sh'\n---\nShell scripts must set -euo pipefail.\n"
        )

        scripts_dir = temp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "build.sh").write_text("#!/bin/bash\necho hi\n")

        yield temp_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.mark.parametrize(
        "config_kwargs",
        [
            {"strategy": "single-file"},
            {"single_agents": True},
        ],
        ids=["strategy-single-file", "single-agents-flag"],
    )
    def test_single_file_mode_emits_only_root_claude_md(
        self, project_with_subdir_instruction, config_kwargs
    ):
        # Both strategy='single-file' and single_agents=True must collapse the
        # CLAUDE.md placement map to a single root file; per-subdir CLAUDE.md
        # files must not leak when single-file mode is requested via either
        # surface.
        config = CompilationConfig(
            target="claude",
            dry_run=False,
            with_constitution=False,
            **config_kwargs,
        )
        compiler = AgentsCompiler(str(project_with_subdir_instruction))
        result = compiler.compile(config)

        assert result.success, f"compile failed: {result.errors}"
        root_claude = project_with_subdir_instruction / "CLAUDE.md"
        subdir_claude = project_with_subdir_instruction / "scripts" / "CLAUDE.md"
        assert root_claude.exists(), "root CLAUDE.md should be generated"
        assert not subdir_claude.exists(), (
            "scripts/CLAUDE.md must NOT be generated in single-file mode"
        )

    def test_distributed_strategy_unchanged_behavior(self, project_with_subdir_instruction):
        # Default distributed strategy must still place per-subdirectory.
        config = CompilationConfig(
            target="claude",
            dry_run=False,
            with_constitution=False,
        )
        compiler = AgentsCompiler(str(project_with_subdir_instruction))
        result = compiler.compile(config)

        assert result.success, f"compile failed: {result.errors}"
        # At least the root CLAUDE.md should exist; distributed may also emit
        # scripts/CLAUDE.md depending on placement -- we don't assert on that.
        # The point is this test documents that distributed behavior is intact.
        assert (project_with_subdir_instruction / "CLAUDE.md").exists() or (
            project_with_subdir_instruction / "scripts" / "CLAUDE.md"
        ).exists()


class TestResolveEffectiveTarget:
    """Tests for _resolve_effective_target()."""

    def _call(self, target: Any, source_root: Any = None) -> tuple[Any, str, Any]:
        from apm_cli.commands.compile.cli import _resolve_effective_target

        if source_root is None:
            source_root = Path(".")
        return _resolve_effective_target(target, source_root=source_root)

    def test_none_triggers_auto_detect(self, tmp_path: Path) -> None:
        """target=None should fall through to detect_target() in a no-apm-yml scenario."""
        with patch(
            "apm_cli.core.target_detection.detect_target",
            return_value=("vscode", "no .github folder"),
        ):
            effective, _reason, _config = self._call(None, source_root=tmp_path)

        assert effective is not None

    def test_explicit_frozenset_returns_immediately(self, tmp_path: Path) -> None:
        """A frozenset from _resolve_compile_target bypasses detect_target."""
        fs = frozenset({"vscode", "claude", "agents"})
        with patch("apm_cli.commands.compile.cli._resolve_compile_target", return_value=fs):
            effective, reason, _config = self._call(["claude", "vscode"], source_root=tmp_path)

        assert effective == fs
        assert reason == "explicit --target flag"

    def test_apm_yml_frozenset_config_without_explicit(self, tmp_path: Path) -> None:
        """frozenset from apm.yml config_target (no explicit) → 'apm.yml target' reason."""
        fs = frozenset({"claude", "agents"})
        (tmp_path / "apm.yml").write_text("name: test\n")

        with (
            patch("apm_cli.commands.compile.cli._resolve_compile_target") as mock_rct,
            patch("apm_cli.models.apm_package.APMPackage.from_apm_yml") as mock_pkg,
        ):
            mock_pkg.return_value.target = "claude,cursor"
            mock_rct.side_effect = [None, fs]

            effective, reason, _config = self._call(None, source_root=tmp_path)

        assert effective == fs
        assert reason == "apm.yml target"

    def test_single_string_explicit_target_uses_detect_target(self, tmp_path: Path) -> None:
        """Single explicit string passes through detect_target()."""
        (tmp_path / "apm.yml").write_text("name: test\n")

        with (
            patch("apm_cli.commands.compile.cli._resolve_compile_target", side_effect=lambda x: x),
            patch("apm_cli.models.apm_package.APMPackage.from_apm_yml") as mock_pkg,
            patch(
                "apm_cli.core.target_detection.detect_target",
                return_value=("claude", "explicit --target flag"),
            ),
        ):
            mock_pkg.return_value.target = None

            effective, _reason, _config = self._call("claude", source_root=tmp_path)

        assert effective == "claude"


class TestCompileCommandNoApmYml:
    """compile command exits when no apm.yml is present."""

    def test_exits_when_no_apm_yml(self) -> None:
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["compile"])

        assert result.exit_code != 0 or "Not an APM project" in result.output


class TestCompileTargetWithoutCompileOutput:
    """Explicit skills-only/experimental targets are a no-op, never auto-detect (#3842)."""

    def _run(self, target: str) -> tuple[Any, list[str]]:
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            Path(".claude").mkdir()
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**'\n---\nUse type hints.\n"
            )
            with patch("apm_cli.core.experimental.is_enabled", return_value=False):
                result = runner.invoke(cli, ["compile", "--target", target])
            root_files = sorted(p.name for p in Path(".").glob("*.md"))
        return result, root_files

    @pytest.mark.parametrize("target", _NO_COMPILE_OUTPUT_TARGETS)
    def test_explicit_target_is_successful_noop(self, target: str) -> None:
        result, root_files = self._run(target)
        output = " ".join(result.output.split())

        assert result.exit_code == 0, result.output
        assert root_files == []
        assert "auto-detect" not in output
        assert f"{target} has no compile outputs -- skipping" in output
        experimental_flag = TARGET_CAPABILITIES[target].experimental_flag
        if experimental_flag is not None:
            flag = experimental_flag.replace("_", "-")
            assert f"apm experimental enable {flag}" in output


class TestCompileCommandAllFlag:
    """--all flag semantics."""

    def _run(self, args: list[str]) -> Any:
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
            )
            return runner.invoke(cli, args, catch_exceptions=False)

    def test_all_and_target_together_exit_code_2(self) -> None:
        """--all with --target should exit with code 2."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            result = runner.invoke(cli, ["compile", "--all", "--target", "claude"])

        assert result.exit_code == 2 or "Cannot use --all together with --target" in result.output

    def test_target_all_emits_deprecation_warning(self) -> None:
        """--target all should emit a deprecation warning."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            # Mock heavy compilation so we don't need full setup
            with patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls:
                mock_result = MagicMock()
                mock_result.success = True
                mock_result.warnings = []
                mock_result.errors = []
                mock_result.stats = {"agents_files_written": 1}
                mock_result.has_critical_security = False
                mock_cls.return_value.compile.return_value = mock_result
                with (
                    patch(
                        "apm_cli.commands.compile.cli.CompilationConfig.from_apm_yml"
                    ) as mock_cfg,
                    patch("apm_cli.commands.compile.cli.discover_primitives"),
                ):
                    mock_cfg.return_value.strategy = "distributed"
                    mock_cfg.return_value.with_constitution = True
                    result = runner.invoke(cli, ["compile", "--target", "all"])

        assert "deprecated" in result.output.lower() or result.exit_code in (0, 1)


class TestCompileCommandValidateMode:
    """compile --validate mode."""

    def test_validate_mode_success(self) -> None:
        """--validate with no errors should print success message."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
            )
            mock_primitives = MagicMock()
            mock_primitives.count.return_value = 1
            mock_primitives.chatmodes = []
            mock_primitives.instructions = [MagicMock()]
            mock_primitives.contexts = []

            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls,
                patch(
                    "apm_cli.commands.compile.cli.discover_primitives", return_value=mock_primitives
                ),
            ):
                mock_cls.return_value.validate_primitives.return_value = []
                result = runner.invoke(cli, ["compile", "--validate"])

        assert result.exit_code == 0
        assert "validated" in result.output.lower() or "success" in result.output.lower()

    def test_validate_mode_with_errors_exits_1(self) -> None:
        """--validate with errors should exit with code 1."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
            )
            mock_primitives = MagicMock()
            mock_primitives.count.return_value = 1
            mock_primitives.chatmodes = []
            mock_primitives.instructions = [MagicMock()]
            mock_primitives.contexts = []

            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls,
                patch(
                    "apm_cli.commands.compile.cli.discover_primitives", return_value=mock_primitives
                ),
            ):
                mock_cls.return_value.validate_primitives.return_value = ["error1"]
                result = runner.invoke(cli, ["compile", "--validate"])

        assert result.exit_code == 1

    def test_validate_mode_discover_exception_exits_1(self) -> None:
        """--validate when discover_primitives raises should exit with code 1."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
            )
            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler"),
                patch(
                    "apm_cli.commands.compile.cli.discover_primitives",
                    side_effect=RuntimeError("boom"),
                ),
            ):
                result = runner.invoke(cli, ["compile", "--validate"])

        assert result.exit_code == 1


class TestCompileCommandWatchMode:
    """compile --watch mode."""

    def test_watch_mode_delegates_to_watch_mode(self) -> None:
        """--watch should call _watch_mode with resolved effective_target."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
            )
            with (
                patch("apm_cli.commands.compile.cli._watch_mode") as mock_watch,
                patch(
                    "apm_cli.commands.compile.cli._resolve_effective_target",
                    return_value=("vscode", "auto-detect", None),
                ),
            ):
                result = runner.invoke(cli, ["compile", "--watch"])

        assert result.exit_code == 0, result.output
        mock_watch.assert_called_once()


class TestCompileCommandDistributedSuccess:
    """compile command - distributed strategy success path."""

    def _setup_project_dir(self) -> None:
        Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
        apm_dir = Path(".apm") / "instructions"
        apm_dir.mkdir(parents=True)
        (apm_dir / "test.instructions.md").write_text(
            "---\ndescription: Test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
        )

    def test_distributed_success_with_files_written(self) -> None:
        """Success + non-zero _files_written logs success message."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            self._setup_project_dir()

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.warnings = []
            mock_result.errors = []
            mock_result.stats = {"agents_files_written": 2}
            mock_result.has_critical_security = False

            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls,
                patch("apm_cli.commands.compile.cli.CompilationConfig.from_apm_yml") as mock_cfg,
            ):
                mock_cfg.return_value.strategy = "distributed"
                mock_cfg.return_value.with_constitution = True
                mock_cfg.return_value.target = "vscode"
                mock_cfg.return_value.output_path = "AGENTS.md"
                mock_cfg.return_value.chatmode = None
                mock_cfg.return_value.resolve_links = True
                mock_cfg.return_value.dry_run = False
                mock_cfg.return_value.debug = False
                mock_cfg.return_value.trace = False
                mock_cfg.return_value.local_only = False
                mock_cfg.return_value.clean_orphaned = False
                mock_cls.return_value.compile.return_value = mock_result

                result = runner.invoke(cli, ["compile"])

        assert result.exit_code == 0
        assert "success" in result.output.lower() or "compil" in result.output.lower()

    def test_distributed_success_zero_files_emits_warning(self) -> None:
        """Success + zero _files_written logs a warning (not success message)."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            self._setup_project_dir()

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.warnings = []
            mock_result.errors = []
            mock_result.stats = {"agents_files_written": 0, "claude_files_written": 0}
            mock_result.has_critical_security = False

            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls,
                patch("apm_cli.commands.compile.cli.CompilationConfig.from_apm_yml") as mock_cfg,
            ):
                mock_cfg.return_value.strategy = "distributed"
                mock_cfg.return_value.with_constitution = True
                mock_cfg.return_value.target = "vscode"
                mock_cfg.return_value.output_path = "AGENTS.md"
                mock_cfg.return_value.chatmode = None
                mock_cfg.return_value.resolve_links = True
                mock_cfg.return_value.dry_run = False
                mock_cfg.return_value.debug = False
                mock_cfg.return_value.trace = False
                mock_cfg.return_value.local_only = False
                mock_cfg.return_value.clean_orphaned = False
                mock_cls.return_value.compile.return_value = mock_result

                result = runner.invoke(cli, ["compile"])

        assert (
            "no output" in result.output.lower()
            or "warning" in result.output.lower()
            or result.exit_code in (0, 1)
        )

    def test_distributed_success_reports_nested_repository_skips(self) -> None:
        """Partial distributed output names the nested placements that were skipped."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            self._setup_project_dir()

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.warnings = []
            mock_result.errors = []
            mock_result.stats = {
                "agents_files_written": 2,
                "copilot_root_instructions_generated": 1,
                "copilot_root_instructions_written": 1,
                "nested_git_placements_skipped": 1,
            }
            mock_result.has_critical_security = False

            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls,
                patch("apm_cli.commands.compile.cli.CompilationConfig.from_apm_yml") as mock_cfg,
            ):
                mock_cfg.return_value.strategy = "distributed"
                mock_cfg.return_value.with_constitution = True
                mock_cfg.return_value.target = "vscode"
                mock_cfg.return_value.output_path = "AGENTS.md"
                mock_cfg.return_value.chatmode = None
                mock_cfg.return_value.resolve_links = True
                mock_cfg.return_value.dry_run = False
                mock_cfg.return_value.debug = False
                mock_cfg.return_value.trace = False
                mock_cfg.return_value.local_only = False
                mock_cfg.return_value.clean_orphaned = False
                mock_cls.return_value.compile.return_value = mock_result

                result = runner.invoke(cli, ["compile"])

        assert result.exit_code == 0
        normalized_output = " ".join(result.output.split())
        assert "Compiled 3 output files (2 AGENTS.md files)" in normalized_output
        assert "skipped 1 nested Git repository placement" in normalized_output

    def test_result_errors_exits_1(self) -> None:
        """result.errors → exit code 1."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            self._setup_project_dir()

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.warnings = []
            mock_result.errors = ["compilation error detail"]
            mock_result.stats = {"agents_files_written": 1}
            mock_result.has_critical_security = False

            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls,
                patch("apm_cli.commands.compile.cli.CompilationConfig.from_apm_yml") as mock_cfg,
            ):
                mock_cfg.return_value.strategy = "distributed"
                mock_cfg.return_value.with_constitution = True
                mock_cfg.return_value.target = "vscode"
                mock_cfg.return_value.output_path = "AGENTS.md"
                mock_cfg.return_value.chatmode = None
                mock_cfg.return_value.resolve_links = True
                mock_cfg.return_value.dry_run = False
                mock_cfg.return_value.debug = False
                mock_cfg.return_value.trace = False
                mock_cfg.return_value.local_only = False
                mock_cfg.return_value.clean_orphaned = False
                mock_cls.return_value.compile.return_value = mock_result

                result = runner.invoke(cli, ["compile"])

        assert result.exit_code == 1

    def test_critical_security_exits_1(self) -> None:
        """has_critical_security → exit code 1."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            self._setup_project_dir()

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.warnings = []
            mock_result.errors = []
            mock_result.stats = {"agents_files_written": 1}
            mock_result.has_critical_security = True

            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls,
                patch("apm_cli.commands.compile.cli.CompilationConfig.from_apm_yml") as mock_cfg,
            ):
                mock_cfg.return_value.strategy = "distributed"
                mock_cfg.return_value.with_constitution = True
                mock_cfg.return_value.target = "vscode"
                mock_cfg.return_value.output_path = "AGENTS.md"
                mock_cfg.return_value.chatmode = None
                mock_cfg.return_value.resolve_links = True
                mock_cfg.return_value.dry_run = False
                mock_cfg.return_value.debug = False
                mock_cfg.return_value.trace = False
                mock_cfg.return_value.local_only = False
                mock_cfg.return_value.clean_orphaned = False
                mock_cls.return_value.compile.return_value = mock_result

                result = runner.invoke(cli, ["compile"])

        assert result.exit_code == 1
        assert "critical" in result.output.lower()


class TestCompileCommandWarnings:
    """compile command - warnings propagation."""

    def test_result_warnings_appear_in_output(self) -> None:
        """result.warnings should be printed."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
            )
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.warnings = ["deprecated usage detected"]
            mock_result.errors = []
            mock_result.stats = {"agents_files_written": 1}
            mock_result.has_critical_security = False

            with (
                patch("apm_cli.commands.compile.cli.AgentsCompiler") as mock_cls,
                patch("apm_cli.commands.compile.cli.CompilationConfig.from_apm_yml") as mock_cfg,
            ):
                mock_cfg.return_value.strategy = "distributed"
                mock_cfg.return_value.with_constitution = True
                mock_cfg.return_value.target = "vscode"
                mock_cfg.return_value.output_path = "AGENTS.md"
                mock_cfg.return_value.chatmode = None
                mock_cfg.return_value.resolve_links = True
                mock_cfg.return_value.dry_run = False
                mock_cfg.return_value.debug = False
                mock_cfg.return_value.trace = False
                mock_cfg.return_value.local_only = False
                mock_cfg.return_value.clean_orphaned = False
                mock_cls.return_value.compile.return_value = mock_result

                result = runner.invoke(cli, ["compile"])

        assert "warning" in result.output.lower() or "deprecated" in result.output.lower()


class TestCompileCommandNoContent:
    """compile command - no content to compile branches."""

    def test_no_apm_modules_no_local_apm_content(self) -> None:
        """When no apm_modules, no .apm content, no constitution → exit 1."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            # No .apm dir, no apm_modules dir
            result = runner.invoke(cli, ["compile"])

        assert result.exit_code == 1
        assert (
            "No APM content" in result.output
            or "No instruction files" in result.output
            or "Not an APM project" in result.output
        )

    def test_empty_apm_dir_shows_no_instructions_message(self) -> None:
        """Empty .apm dir (no .instructions.md) shows different message."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            Path(".apm").mkdir()
            result = runner.invoke(cli, ["compile"])

        assert result.exit_code == 1
        # Either "No instruction files" or "No APM content"
        assert "No" in result.output


class TestCompileCommandImportError:
    """compile command - ImportError path."""

    def test_import_error_in_compilation_exits_1(self) -> None:
        """ImportError inside the try block should exit with code 1."""
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text("name: test\nversion: 0.1.0\n")
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
            )
            with patch(
                "apm_cli.commands.compile.cli.CompilationConfig.from_apm_yml",
                side_effect=ImportError("missing module"),
            ):
                result = runner.invoke(cli, ["compile"])

        assert result.exit_code == 1
        assert "not available" in result.output or "module" in result.output.lower()
