"""Unit tests for --target flag and compilation routing in apm compile command.

Tests cover:
- CompilationConfig.target defaults to "all"
- --target vscode only generates AGENTS.md
- --target agents is alias for vscode
- --target claude only generates CLAUDE.md
- --target all generates both files (default)
- Invalid target value raises error
- _merge_results() correctly combines results
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.compilation.agents_compiler import (
    AgentsCompiler,
    CompilationConfig,
    CompilationResult,
)
from apm_cli.primitives.models import Instruction, PrimitiveCollection


class TestCompilationConfigTarget:
    """Tests for CompilationConfig.target field."""

    def test_target_default_is_all(self):
        """Test that CompilationConfig.target defaults to 'all'."""
        config = CompilationConfig()
        assert config.target == "all"

    def test_target_can_be_set_to_vscode(self):
        """Test that target can be set to 'vscode'."""
        config = CompilationConfig(target="vscode")
        assert config.target == "vscode"

    def test_target_can_be_set_to_agents(self):
        """Test that target can be set to 'agents'."""
        config = CompilationConfig(target="agents")
        assert config.target == "agents"

    def test_target_can_be_set_to_claude(self):
        """Test that target can be set to 'claude'."""
        config = CompilationConfig(target="claude")
        assert config.target == "claude"

    def test_from_apm_yml_applies_target_override(self):
        """Test that from_apm_yml correctly applies target override."""
        config = CompilationConfig.from_apm_yml(target="claude")
        assert config.target == "claude"

    def test_from_apm_yml_uses_default_when_no_override(self):
        """Test that from_apm_yml uses default target when not overridden."""
        config = CompilationConfig.from_apm_yml()
        assert config.target == "all"


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

    def test_target_claude_generates_claude_md(self, temp_project, sample_primitives):
        """Test that target='claude' generates CLAUDE.md files."""
        config = CompilationConfig(target="claude", dry_run=True)

        compiler = AgentsCompiler(str(temp_project))
        result = compiler.compile(config, sample_primitives)

        assert result.success
        # Output path should reference CLAUDE.md
        assert "CLAUDE" in result.output_path

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

    def test_frozenset_cursor_opencode_codex_does_not_emit_copilot_instructions(self, temp_project):
        """Round-3 regression: -t cursor,opencode,codex (no copilot family)
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
        # Simulate what _resolve_compile_target(["cursor","opencode","codex"]) returns
        # under the new semantics: a single 'agents'-only family collapses to
        # the bare cursor target, which routes correctly via the string path.
        result = compiler.compile(
            CompilationConfig(target="cursor", dry_run=False, single_agents=True),
            primitives,
        )

        assert result.success
        root_file = temp_project / ".github" / "copilot-instructions.md"
        assert not root_file.exists(), (
            "copilot-instructions.md must NOT be generated for cursor/opencode/codex"
        )
        assert result.stats.get("copilot_root_instructions_generated", 0) == 0

    def test_resolve_compile_target_cursor_opencode_codex_does_not_route_to_vscode(self):
        """Round-3 regression at the resolver: cursor/opencode/codex multi-target
        list must not collapse to 'vscode' (which would wrongly route
        copilot-instructions.md generation)."""
        from apm_cli.commands.compile.cli import _resolve_compile_target
        from apm_cli.core.target_detection import should_compile_copilot_instructions_md

        for target_list in (
            ["cursor"],
            ["opencode"],
            ["codex"],
            ["cursor", "opencode"],
            ["cursor", "opencode", "codex"],
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
            ["cursor", "vscode"],
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
        assert "apm compile" in warning, "warning must tell the user the next command to run"
        assert "delete or rename" in warning, "warning must give a concrete action"
        assert "<!-- Generated by APM CLI from .apm/ primitives -->" in warning, (
            "warning must disclose the literal marker string so users can self-serve recovery"
        )

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
        result = compiler.compile(
            CompilationConfig(target="vscode", dry_run=False, single_agents=True),
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


class TestMergeResults:
    """Tests for _merge_results() method."""

    @pytest.fixture
    def compiler(self):
        """Create a compiler instance for testing."""
        return AgentsCompiler(".")

    def test_merge_empty_results_list(self, compiler):
        """Test merging an empty results list."""
        result = compiler._merge_results([])

        assert result.success is True
        assert result.output_path == ""
        assert result.content == ""
        assert result.warnings == []
        assert result.errors == []
        assert result.stats == {}

    def test_merge_single_result(self, compiler):
        """Test that single result is returned as-is."""
        single_result = CompilationResult(
            success=True,
            output_path="AGENTS.md",
            content="# Test content",
            warnings=["warning1"],
            errors=[],
            stats={"test": 1},
        )

        result = compiler._merge_results([single_result])

        assert result.success is True
        assert result.output_path == "AGENTS.md"
        assert result.content == "# Test content"
        assert result.warnings == ["warning1"]
        assert result.stats == {"test": 1}

    def test_merge_multiple_results_success(self, compiler):
        """Test merging multiple successful results."""
        result1 = CompilationResult(
            success=True,
            output_path="AGENTS.md",
            content="AGENTS content",
            warnings=["agents warning"],
            errors=[],
            stats={"agents_files_generated": 2},
        )

        result2 = CompilationResult(
            success=True,
            output_path="CLAUDE.md: 1 files",
            content="CLAUDE content",
            warnings=["claude warning"],
            errors=[],
            stats={"claude_files_written": 1},
        )

        merged = compiler._merge_results([result1, result2])

        assert merged.success is True
        assert "AGENTS.md" in merged.output_path
        assert "CLAUDE" in merged.output_path
        assert "agents warning" in merged.warnings
        assert "claude warning" in merged.warnings
        assert "AGENTS content" in merged.content
        assert "CLAUDE content" in merged.content

    def test_merge_results_with_one_failure(self, compiler):
        """Test that merged result is failure if any result fails."""
        result1 = CompilationResult(
            success=True,
            output_path="AGENTS.md",
            content="Success",
            warnings=[],
            errors=[],
            stats={},
        )

        result2 = CompilationResult(
            success=False,
            output_path="CLAUDE.md",
            content="",
            warnings=[],
            errors=["Failed to compile"],
            stats={},
        )

        merged = compiler._merge_results([result1, result2])

        assert merged.success is False
        assert "Failed to compile" in merged.errors

    def test_merge_results_combines_numeric_stats(self, compiler):
        """Test that numeric stats are summed when merging."""
        result1 = CompilationResult(
            success=True,
            output_path="A",
            content="",
            warnings=[],
            errors=[],
            stats={"primitives_found": 5, "instructions": 3},
        )

        result2 = CompilationResult(
            success=True,
            output_path="B",
            content="",
            warnings=[],
            errors=[],
            stats={"primitives_found": 2, "claude_files_written": 1},
        )

        merged = compiler._merge_results([result1, result2])

        # Same key should be summed
        assert merged.stats["primitives_found"] == 7
        # Different keys should be kept
        assert merged.stats["instructions"] == 3
        assert merged.stats["claude_files_written"] == 1

    def test_merge_results_preserves_all_warnings_and_errors(self, compiler):
        """Test that all warnings and errors are preserved."""
        result1 = CompilationResult(
            success=True,
            output_path="A",
            content="",
            warnings=["warn1", "warn2"],
            errors=[],
            stats={},
        )

        result2 = CompilationResult(
            success=True,
            output_path="B",
            content="",
            warnings=["warn3"],
            errors=["error1"],
            stats={},
        )

        merged = compiler._merge_results([result1, result2])

        assert len(merged.warnings) == 3
        assert "warn1" in merged.warnings
        assert "warn2" in merged.warnings
        assert "warn3" in merged.warnings
        assert len(merged.errors) == 1
        assert "error1" in merged.errors

    def test_merge_results_joins_output_paths(self, compiler):
        """Test that output paths are joined with ' | '."""
        result1 = CompilationResult(
            success=True,
            output_path="Distributed: 3 AGENTS.md files",
            content="",
            warnings=[],
            errors=[],
            stats={},
        )

        result2 = CompilationResult(
            success=True,
            output_path="CLAUDE.md: 2 files",
            content="",
            warnings=[],
            errors=[],
            stats={},
        )

        merged = compiler._merge_results([result1, result2])

        assert " | " in merged.output_path
        assert "Distributed" in merged.output_path
        assert "CLAUDE.md" in merged.output_path

    def test_merge_results_joins_content_with_separator(self, compiler):
        """Test that content is joined with separator."""
        result1 = CompilationResult(
            success=True, output_path="A", content="Content A", warnings=[], errors=[], stats={}
        )

        result2 = CompilationResult(
            success=True, output_path="B", content="Content B", warnings=[], errors=[], stats={}
        )

        merged = compiler._merge_results([result1, result2])

        assert "---" in merged.content
        assert "Content A" in merged.content
        assert "Content B" in merged.content


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


class TestResolveCompileTarget:
    """Tests for _resolve_compile_target() multi-target list mapping."""

    def test_none_returns_none(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(None) is None

    def test_single_string_passthrough(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target("claude") == "claude"
        assert _resolve_compile_target("vscode") == "vscode"
        assert _resolve_compile_target("all") == "all"
        assert _resolve_compile_target("copilot") == "copilot"

    def test_list_claude_and_copilot_returns_full_set(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        # 'vscode' family member is added because copilot/vscode/agents triggers
        # copilot-instructions.md generation; 'agents' covers AGENTS.md.
        assert _resolve_compile_target(["claude", "vscode"]) == frozenset(
            {"vscode", "agents", "claude"}
        )
        assert _resolve_compile_target(["claude", "copilot"]) == frozenset(
            {"vscode", "agents", "claude"}
        )

    def test_list_claude_only_returns_claude(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["claude"]) == "claude"

    def test_list_copilot_only_returns_vscode(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["vscode"]) == "vscode"
        assert _resolve_compile_target(["copilot"]) == "vscode"

    def test_list_agents_md_only_family_preserves_bare_target(self):
        """cursor/opencode/codex/windsurf must NOT collapse to 'vscode' (which would
        wrongly route copilot-instructions.md). Single-element lists keep the
        bare target name; multi-element lists pick the first present."""
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["cursor"]) == "cursor"
        assert _resolve_compile_target(["opencode"]) == "opencode"
        assert _resolve_compile_target(["codex"]) == "codex"
        assert _resolve_compile_target(["windsurf"]) == "windsurf"
        # Multi-element AGENTS.md-only list collapses to a representative bare
        # target (cursor wins by deterministic ordering).
        assert _resolve_compile_target(["cursor", "opencode"]) == "cursor"
        assert _resolve_compile_target(["opencode", "codex"]) == "opencode"
        assert _resolve_compile_target(["codex", "windsurf"]) == "codex"

    def test_windsurf_routes_via_agents_family(self):
        """Regression: windsurf must route through the 'agents' compile_family
        the same way cursor/opencode/codex do.  Before the registry-driven
        refactor, windsurf was missing from agents_md_family and would have
        silently collapsed to the 'vscode' fallback (emitting copilot-
        instructions.md by mistake)."""
        from apm_cli.commands.compile.cli import _resolve_compile_target

        # Combined with claude/gemini -> frozenset of families
        assert _resolve_compile_target(["windsurf", "claude"]) == frozenset({"agents", "claude"})
        assert _resolve_compile_target(["windsurf", "gemini"]) == frozenset({"agents", "gemini"})
        # Combined with copilot/vscode -> 'vscode' wins (which already includes agents)
        assert _resolve_compile_target(["windsurf", "copilot"]) == "vscode"

    def test_list_cursor_and_claude_returns_agents_claude_set(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        # No 'vscode' family because copilot/vscode/agents was not requested;
        # cursor/codex contribute only the 'agents' (AGENTS.md) family.
        assert _resolve_compile_target(["cursor", "claude"]) == frozenset({"agents", "claude"})
        assert _resolve_compile_target(["codex", "claude"]) == frozenset({"agents", "claude"})

    def test_list_gemini_only_returns_gemini(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["gemini"]) == "gemini"

    def test_list_gemini_and_claude_returns_claude_gemini_set(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["gemini", "claude"]) == frozenset({"claude", "gemini"})

    def test_list_gemini_and_copilot_returns_full_set(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["gemini", "vscode"]) == frozenset(
            {"vscode", "agents", "gemini"}
        )

    def test_list_all_three_families_returns_full_set(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["claude", "vscode", "gemini"]) == frozenset(
            {"vscode", "agents", "claude", "gemini"}
        )
        assert _resolve_compile_target(["claude", "vscode", "cursor"]) == frozenset(
            {"vscode", "agents", "claude"}
        )


class TestMultiTargetDoesNotGenerateUnrequestedFiles:
    """Regression tests: multi-target lists must not generate files for families not requested."""

    def test_claude_codex_does_not_compile_gemini(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target
        from apm_cli.core.target_detection import (
            should_compile_agents_md,
            should_compile_claude_md,
            should_compile_gemini_md,
        )

        resolved = _resolve_compile_target(["claude", "codex"])
        assert should_compile_agents_md(resolved) is True
        assert should_compile_claude_md(resolved) is True
        assert should_compile_gemini_md(resolved) is False

    def test_claude_cursor_does_not_compile_gemini(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target
        from apm_cli.core.target_detection import (
            should_compile_agents_md,
            should_compile_claude_md,
            should_compile_gemini_md,
        )

        resolved = _resolve_compile_target(["claude", "cursor"])
        assert should_compile_agents_md(resolved) is True
        assert should_compile_claude_md(resolved) is True
        assert should_compile_gemini_md(resolved) is False

    def test_gemini_codex_does_not_compile_claude(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target
        from apm_cli.core.target_detection import (
            should_compile_agents_md,
            should_compile_claude_md,
            should_compile_gemini_md,
        )

        resolved = _resolve_compile_target(["gemini", "codex"])
        assert should_compile_agents_md(resolved) is True
        assert should_compile_claude_md(resolved) is False
        assert should_compile_gemini_md(resolved) is True

    def test_all_string_still_compiles_everything(self):
        from apm_cli.core.target_detection import (
            should_compile_agents_md,
            should_compile_claude_md,
            should_compile_gemini_md,
        )

        assert should_compile_agents_md("all") is True
        assert should_compile_claude_md("all") is True
        assert should_compile_gemini_md("all") is True

    def test_single_target_strings_unchanged(self):
        from apm_cli.core.target_detection import (
            should_compile_agents_md,
            should_compile_claude_md,
            should_compile_gemini_md,
        )

        assert should_compile_agents_md("vscode") is True
        assert should_compile_claude_md("vscode") is False
        assert should_compile_gemini_md("vscode") is False

        assert should_compile_agents_md("claude") is False
        assert should_compile_claude_md("claude") is True
        assert should_compile_gemini_md("claude") is False

        assert should_compile_agents_md("gemini") is True
        assert should_compile_claude_md("gemini") is False
        assert should_compile_gemini_md("gemini") is True


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
