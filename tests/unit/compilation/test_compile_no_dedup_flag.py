"""Unit tests for the --no-dedup / --force-instructions flag on apm compile.

Acceptance criteria from issue #1463:
- New CLI flag accepted by `apm compile`
- When set, instructions section is always included in CLAUDE.md regardless
  of .claude/rules/ contents
- CompilationConfig carries the flag as no_dedup: bool
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.compilation.agents_compiler import (
    AgentsCompiler,
    CompilationConfig,
    _build_expected_rule_filenames,
)
from apm_cli.primitives.models import Instruction, PrimitiveCollection

pytestmark = pytest.mark.component


class TestNoDedupFlagCLI:
    """CLI accepts --no-dedup and --force-instructions."""

    @pytest.fixture
    def temp_project(self, tmp_path):
        """Minimal APM project with a .claude/rules/ rule file pre-populated."""
        (tmp_path / "apm.yml").write_text(
            "name: test-no-dedup\nversion: 1.0.0\ntargets:\n  - claude\n",
            encoding="utf-8",
        )
        instr_dir = tmp_path / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "style.instructions.md").write_text(
            "---\ndescription: Style rule\napplyTo: '**/*.py'\n---\n# Style\nUse type hints.\n",
            encoding="utf-8",
        )
        # Pre-populate .claude/rules/ so dedup would normally fire
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "style.md").write_text(
            "---\ndescription: Style rule\n---\n# Style\nUse type hints.\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_no_dedup_flag_accepted_by_compile(self, temp_project):
        """--no-dedup is a recognised flag (no 'no such option' error)."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(temp_project)):
            import os

            os.chdir(str(temp_project))
            result = runner.invoke(cli, ["compile", "--target", "claude", "--no-dedup"])
        # Must not error with "No such option"
        assert "no such option" not in result.output.lower(), result.output
        assert result.exit_code != 2, result.output  # 2 = Click usage error

    def test_force_instructions_alias_accepted(self, temp_project):
        """--force-instructions is accepted as an alias for --no-dedup."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(temp_project)):
            import os

            os.chdir(str(temp_project))
            result = runner.invoke(cli, ["compile", "--target", "claude", "--force-instructions"])
        assert "no such option" not in result.output.lower(), result.output
        assert result.exit_code != 2, result.output


class TestNoDedupSkipsDeduplicationLogic:
    """When no_dedup=True, _compile_claude_md must not omit instructions even
    when .claude/rules/ contains .md files."""

    @pytest.fixture
    def project_with_rules(self, tmp_path):
        """Temporary project with both primitives and pre-existing .claude/rules/."""
        # Pre-populate rules dir
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "existing.md").write_text("# existing rule\n", encoding="utf-8")

        instruction = Instruction(
            name="python-style",
            file_path=tmp_path / ".apm/instructions/python.instructions.md",
            description="Python coding standards",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local",
        )
        primitives = PrimitiveCollection()
        primitives.add_primitive(instruction)
        return tmp_path, primitives

    @staticmethod
    def _compile(tmp_path, primitives, *, no_dedup):
        # The rule install deploys for this instruction carries its applyTo as paths:.
        (tmp_path / ".claude" / "rules" / "python.md").write_text(
            '---\npaths:\n  - "**/*.py"\n---\n# deployed\n', encoding="utf-8"
        )
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(target="claude", no_dedup=no_dedup, resolve_links=False)
        result = compiler.compile(config, primitives)
        assert result.success, result.errors
        agents_md = tmp_path / "AGENTS.md"
        return agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""

    def test_without_no_dedup_instructions_are_skipped(self, project_with_rules):
        """A deployed .claude/rules/ file keeps its instruction out of AGENTS.md."""
        tmp_path, primitives = project_with_rules
        assert "Use type hints." not in self._compile(tmp_path, primitives, no_dedup=False)

    def test_with_no_dedup_instructions_are_included(self, project_with_rules):
        """no_dedup=True keeps every instruction in the AGENTS.md CLAUDE.md imports."""
        tmp_path, primitives = project_with_rules

        body = self._compile(tmp_path, primitives, no_dedup=True)

        assert "Use type hints." in body
        assert "@AGENTS.md" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


class TestAgentsMdInstructionDedup:
    """Regression tests: AGENTS.md dedup must be target-aware."""

    @staticmethod
    def _project_with_instruction(
        tmp_path,
        *,
        rules_dir: str | None = None,
        rules_filename: str | None = None,
    ):
        """Create a project with one style instruction and an optional rule file."""
        (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\n", encoding="utf-8")
        apm_instr_dir = tmp_path / ".apm" / "instructions"
        apm_instr_dir.mkdir(parents=True)
        (apm_instr_dir / "style.instructions.md").write_text(
            "---\ndescription: Style rule\napplyTo: '**/*.py'\n---\n# Style\nUse type hints.\n",
            encoding="utf-8",
        )

        if rules_dir and rules_filename:
            deployed_dir = tmp_path / rules_dir
            deployed_dir.mkdir(parents=True)
            (deployed_dir / rules_filename).write_text(
                "# Unrelated\nThis file should not trigger dedup.\n",
                encoding="utf-8",
            )

        instruction = Instruction(
            name="style",
            file_path=tmp_path / ".apm/instructions/style.instructions.md",
            description="Style rule",
            apply_to="**/*.py",
            content="Use type hints.",
            author="test",
            source="local",
        )
        primitives = PrimitiveCollection()
        primitives.add_primitive(instruction)
        return tmp_path, primitives

    @pytest.fixture
    def project_with_github_instructions(self, tmp_path):
        """Project with .github/instructions/ populated + primitives."""
        # Pre-populate .github/instructions/ so dedup would normally fire
        instr_dir = tmp_path / ".github" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "style.instructions.md").write_text(
            "# Style\nUse type hints.\n",
            encoding="utf-8",
        )

        return self._project_with_instruction(tmp_path)

    @pytest.mark.parametrize(
        ("target_key", "expected"),
        [
            ("copilot", {"style.instructions.md"}),
            ("claude", {"style.md"}),
            ("antigravity", {"style.md"}),
        ],
    )
    def test_expected_rule_filenames_match_target_extensions(self, tmp_path, target_key, expected):
        """Filename matching must use the deployed extension for each target."""
        _, primitives = self._project_with_instruction(tmp_path)

        assert _build_expected_rule_filenames(target_key, primitives) == expected

    def test_codex_target_preserves_instructions(self, project_with_github_instructions):
        """Codex does not read .github/instructions/ -- AGENTS.md must keep
        instruction content (issue #1678 regression)."""
        tmp_path, primitives = project_with_github_instructions
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(target="codex", dry_run=False)

        result = compiler._compile_distributed(config, primitives)

        # Instructions must be present in the generated AGENTS.md content
        assert result.success
        # Check via the distributed config that skip_instructions was NOT set
        # by verifying instruction content appears in the output
        agents_md = tmp_path / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md must be generated for codex target"
        body = agents_md.read_text(encoding="utf-8")
        assert "Use type hints" in body, (
            "Codex target must include instructions in AGENTS.md even when "
            ".github/instructions/ exists. Got:\n" + body
        )

    def test_copilot_only_deduplicates_instructions(self, project_with_github_instructions):
        """Copilot (vscode) reads both locations -- dedup should fire."""
        tmp_path, primitives = project_with_github_instructions
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(target="vscode", dry_run=True)

        result = compiler._compile_distributed(config, primitives)

        # With dedup active, the compiled content should not contain the
        # instruction text because it is already in .github/instructions/.
        assert result.success
        assert "Use type hints" not in result.content, (
            "Copilot-only dedup should omit instructions from AGENTS.md "
            "when .github/instructions/ is populated"
        )

    def test_copilot_does_not_dedup_for_unrelated_md(self, tmp_path):
        """An unrelated .md in .github/instructions/ must not trigger dedup."""
        tmp_path, primitives = self._project_with_instruction(
            tmp_path,
            rules_dir=".github/instructions",
            rules_filename="unrelated.md",
        )
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(target="vscode", dry_run=False)

        result = compiler._compile_distributed(config, primitives)

        assert result.success
        agents_md = tmp_path / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md must be generated without a matching rule file"
        body = agents_md.read_text(encoding="utf-8")
        assert "Use type hints" in body, (
            "Copilot dedup must require the expected instruction filename, "
            "not any unrelated .md file in .github/instructions/"
        )

    def test_claude_does_not_dedup_for_unrelated_md(self, tmp_path):
        """An unrelated .md in .claude/rules/ must not trigger dedup."""
        tmp_path, primitives = self._project_with_instruction(
            tmp_path,
            rules_dir=".claude/rules",
            rules_filename="unrelated.md",
        )
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(target="claude", dry_run=False, resolve_links=False)

        result = compiler.compile(config, primitives)

        assert result.success
        assert "@AGENTS.md" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "Use type hints" in body, (
            "Claude dedup must require the expected instruction filename, "
            "not any unrelated .md file in .claude/rules/"
        )

    def test_multi_target_copilot_codex_preserves_instructions(
        self, project_with_github_instructions
    ):
        """[copilot, codex] -> frozenset: must NOT dedup because Codex needs
        instructions in AGENTS.md."""
        tmp_path, primitives = project_with_github_instructions
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(
            target=frozenset({"vscode", "agents"}),
            dry_run=False,
        )

        compiler._compile_distributed(config, primitives)

        agents_md = tmp_path / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md must be generated for mixed copilot+codex target"
        body = agents_md.read_text(encoding="utf-8")
        assert "Use type hints" in body, (
            "Mixed copilot+codex target must include instructions in AGENTS.md. Got:\n" + body
        )

    def test_multi_target_antigravity_codex_preserves_instructions(self, tmp_path):
        """[antigravity, codex] must not dedup AGENTS.md against Antigravity rules."""
        tmp_path, primitives = self._project_with_instruction(
            tmp_path,
            rules_dir=".agents/rules",
            rules_filename="style.md",
        )
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(target=frozenset({"agents"}), dry_run=False)

        result = compiler._compile_distributed(config, primitives)

        assert result.success
        agents_md = tmp_path / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md must be generated for mixed antigravity+codex target"
        body = agents_md.read_text(encoding="utf-8")
        assert "Use type hints" in body, (
            "Mixed antigravity+codex target must include instructions in AGENTS.md. Got:\n" + body
        )

    def test_no_dedup_flag_overrides_agents_md_dedup(self, project_with_github_instructions):
        """--no-dedup on vscode target must include instructions despite dedup
        conditions being met."""
        tmp_path, primitives = project_with_github_instructions
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(
            target="vscode",
            no_dedup=True,
            dry_run=False,
        )

        compiler._compile_distributed(config, primitives)

        agents_md = tmp_path / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md must be generated with --no-dedup"
        body = agents_md.read_text(encoding="utf-8")
        assert "Use type hints" in body, (
            "--no-dedup must force instructions into AGENTS.md. Got:\n" + body
        )

    def test_all_target_preserves_instructions(self, project_with_github_instructions):
        """target='all' includes Codex -- must NOT dedup."""
        tmp_path, primitives = project_with_github_instructions
        compiler = AgentsCompiler(str(tmp_path))
        config = CompilationConfig(target="all", dry_run=False)

        compiler._compile_distributed(config, primitives)

        agents_md = tmp_path / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md must be generated for target='all'"
        body = agents_md.read_text(encoding="utf-8")
        assert "Use type hints" in body, (
            "target='all' must include instructions in AGENTS.md. Got:\n" + body
        )
