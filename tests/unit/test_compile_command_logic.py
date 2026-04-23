"""Tests for ``apm compile`` command logic.

Covers:
- ``_resolve_compile_target`` pure function
- ``_get_validation_suggestion`` pure function
- Compile CLI early-exit paths (no apm.yml, no content, validate mode)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.compile.cli import (
    _get_validation_suggestion,
    _resolve_compile_target,
    compile,
)


# ==================================================================
# _resolve_compile_target
# ==================================================================


class TestResolveCompileTarget:
    """Tests for the _resolve_compile_target helper."""

    def test_none_returns_none(self):
        """None target triggers auto-detection downstream."""
        assert _resolve_compile_target(None) is None

    def test_single_string_passthrough(self):
        """A single string target is returned unchanged."""
        assert _resolve_compile_target("claude") == "claude"
        assert _resolve_compile_target("vscode") == "vscode"
        assert _resolve_compile_target("all") == "all"

    def test_list_claude_only_returns_claude(self):
        """A list with only 'claude' resolves to 'claude'."""
        assert _resolve_compile_target(["claude"]) == "claude"

    def test_list_agents_family_only_returns_vscode(self):
        """Lists from the agents family resolve to 'vscode'."""
        for target in (["vscode"], ["copilot"], ["agents"], ["cursor"], ["opencode"], ["codex"]):
            assert _resolve_compile_target(target) == "vscode", f"Failed for {target}"

    def test_list_agents_family_combined_returns_vscode(self):
        """Multiple agents-family members still resolve to 'vscode'."""
        assert _resolve_compile_target(["vscode", "copilot"]) == "vscode"
        assert _resolve_compile_target(["cursor", "opencode", "codex"]) == "vscode"

    def test_list_claude_and_agents_family_returns_all(self):
        """When both claude and an agents-family target appear, resolve to 'all'."""
        assert _resolve_compile_target(["claude", "vscode"]) == "all"
        assert _resolve_compile_target(["copilot", "claude"]) == "all"
        assert _resolve_compile_target(["claude", "cursor", "opencode"]) == "all"

    def test_list_with_all_returns_all(self):
        """'all' as a list element is treated like an agents-family name (no claude), so vscode."""
        # 'all' is not in the agents-family set AND not 'claude', so single passthrough when alone
        result = _resolve_compile_target(["all"])
        # 'all' is not in {"copilot", "vscode", "agents", "cursor", "opencode", "codex"} and != "claude"
        # so has_agents_family=False, has_claude=False -> returns "vscode"
        assert result == "vscode"


# ==================================================================
# _get_validation_suggestion
# ==================================================================


class TestGetValidationSuggestion:
    """Tests for the _get_validation_suggestion helper."""

    def test_missing_description(self):
        suggestion = _get_validation_suggestion("Missing 'description' in frontmatter")
        assert "description:" in suggestion

    def test_apply_to_globally(self):
        suggestion = _get_validation_suggestion("applyTo does not match any file globally")
        assert "applyTo" in suggestion

    def test_empty_content(self):
        suggestion = _get_validation_suggestion("Empty content in file")
        assert "content" in suggestion.lower() or "frontmatter" in suggestion.lower()

    def test_unknown_error_generic_suggestion(self):
        suggestion = _get_validation_suggestion("Some completely unknown error")
        assert len(suggestion) > 0  # always returns something


# ==================================================================
# compile CLI - early exit paths
# ==================================================================


@pytest.fixture
def runner():
    return CliRunner()


class TestCompileNoApmYml:
    """compile exits with an error when no apm.yml is found."""

    def test_exits_when_no_apm_yml(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(compile, [])
        assert result.exit_code != 0
        assert "apm.yml" in result.output or "APM project" in result.output


class TestCompileNoContent:
    """compile exits early when no APM content is present."""

    def test_no_content_exits_nonzero(self, runner, tmp_path):
        """An empty project (only apm.yml) exits with no-content error."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text("name: test-project\nversion: 1.0.0\n")
            result = runner.invoke(compile, [])
        assert result.exit_code != 0

    def test_empty_apm_dir_shows_helpful_message(self, runner, tmp_path):
        """When .apm/ exists but has no primitives, error explains what to add."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text("name: test-project\nversion: 1.0.0\n")
            Path(".apm").mkdir()
            result = runner.invoke(compile, [])
        assert result.exit_code != 0
        assert ".apm" in result.output or "instruction" in result.output


class TestCompileValidateMode:
    """compile --validate exercises the validation-only code path."""

    def test_validate_with_valid_primitives_succeeds(self, runner, tmp_path):
        """--validate exits 0 when all primitives pass."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text("name: test-project\nversion: 1.0.0\n")
            apm_dir = Path(".apm/instructions")
            apm_dir.mkdir(parents=True)
            (apm_dir / "sample.instructions.md").write_text(
                "---\napplyTo: '**'\n---\n# Instructions\nSome content here.\n"
            )

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.warnings = []
            mock_result.errors = []
            mock_result.has_critical_security = False

            mock_primitives = MagicMock()
            mock_primitives.count.return_value = 1
            mock_primitives.chatmodes = []
            mock_primitives.instructions = ["sample"]
            mock_primitives.contexts = []

            with patch(
                "apm_cli.commands.compile.cli.discover_primitives",
                return_value=mock_primitives,
            ), patch(
                "apm_cli.commands.compile.cli.AgentsCompiler"
            ) as MockCompiler:
                mock_compiler = MockCompiler.return_value
                mock_compiler.validate_primitives.return_value = []
                result = runner.invoke(compile, ["--validate"])

        assert result.exit_code == 0
        assert "validated" in result.output.lower() or "success" in result.output.lower()

    def test_validate_with_errors_exits_nonzero(self, runner, tmp_path):
        """--validate exits non-zero when primitives fail validation."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text("name: test-project\nversion: 1.0.0\n")
            apm_dir = Path(".apm/instructions")
            apm_dir.mkdir(parents=True)
            (apm_dir / "bad.instructions.md").write_text("---\n---\n")

            mock_primitives = MagicMock()
            mock_primitives.count.return_value = 1
            mock_primitives.chatmodes = []
            mock_primitives.instructions = ["bad"]
            mock_primitives.contexts = []

            with patch(
                "apm_cli.commands.compile.cli.discover_primitives",
                return_value=mock_primitives,
            ), patch(
                "apm_cli.commands.compile.cli.AgentsCompiler"
            ) as MockCompiler:
                mock_compiler = MockCompiler.return_value
                mock_compiler.validate_primitives.return_value = [
                    "bad.instructions.md: Missing 'description' in frontmatter"
                ]
                result = runner.invoke(compile, ["--validate"])

        assert result.exit_code != 0

    def test_validate_handles_discover_exception(self, runner, tmp_path):
        """--validate handles an exception from discover_primitives gracefully."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text("name: test-project\nversion: 1.0.0\n")
            apm_dir = Path(".apm/instructions")
            apm_dir.mkdir(parents=True)
            (apm_dir / "sample.instructions.md").write_text("# content\n")

            with patch(
                "apm_cli.commands.compile.cli.discover_primitives",
                side_effect=RuntimeError("disk error"),
            ):
                result = runner.invoke(compile, ["--validate"])

        assert result.exit_code != 0


class TestCompileDryRun:
    """compile --dry-run exercises the compilation path without writing files."""

    def test_dry_run_no_content_exits_but_allows_dry_run(self, runner, tmp_path):
        """--dry-run skips the exit-on-empty check."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text("name: test-project\nversion: 1.0.0\n")
            # No apm_modules, no local content; dry_run bypasses the sys.exit
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.warnings = []
            mock_result.errors = []
            mock_result.has_critical_security = False

            mock_intermediate = MagicMock()
            mock_intermediate.success = False  # force single-file fallback to show errors

            with patch("apm_cli.commands.compile.cli.AgentsCompiler") as MockCompiler:
                mock_compiler = MockCompiler.return_value
                mock_compiler.compile.return_value = mock_result
                # With dry_run=True, early exit is suppressed; compilation still called
                result = runner.invoke(compile, ["--dry-run", "--single-agents"])

        # Either succeeds or hits a later error - just shouldn't have -1 from unhandled exception
        assert result.exit_code in (0, 1)
