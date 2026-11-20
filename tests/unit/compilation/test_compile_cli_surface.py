"""Unit tests for pure helpers behind ``apm compile``.

* _display_single_file_summary  - console present, console absent, fallback
* _display_next_steps           - console present, ImportError fallback
* _display_validation_errors    - console present, ImportError/NameError fallback
* _get_validation_suggestion    - every suggestion branch
* _resolve_compile_target       - single and multi-target mapping
* CompilationConfig target and dedup fields, AgentsCompiler result merging
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.compilation.agents_compiler import (
    AgentsCompiler,
    CompilationConfig,
    CompilationResult,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stats(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "primitives_found": 3,
        "instructions": 2,
        "contexts": 1,
        "agents": 0,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# _display_single_file_summary
# ---------------------------------------------------------------------------


class TestDisplaySingleFileSummary:
    """Tests for _display_single_file_summary()."""

    def _call(
        self,
        stats: dict[str, Any],
        c_status: str,
        c_hash: str | None,
        output_path: Any,
        dry_run: bool,
    ) -> None:
        from apm_cli.commands.compile.cli import _display_single_file_summary

        _display_single_file_summary(stats, c_status, c_hash, output_path, dry_run)

    def test_no_console_falls_back_to_rich_info(self) -> None:
        """When _get_console() returns None, output via _rich_info."""
        mock_output: list[str] = []

        def fake_rich_info(msg: str, **_: Any) -> None:
            mock_output.append(msg)

        with (
            patch("apm_cli.commands.compile.cli._get_console", return_value=None),
            patch("apm_cli.commands.compile.cli._rich_info", side_effect=fake_rich_info),
        ):
            self._call(_make_stats(), "CREATED", "abc123", Path("AGENTS.md"), False)

        assert any("3" in m or "primitives" in m.lower() for m in mock_output)
        assert any("2" in m or "instructions" in m.lower() for m in mock_output)

    def test_no_console_hash_none_renders_dash(self) -> None:
        """c_hash=None should render as '-' in the fallback path."""
        captured: list[str] = []
        with (
            patch("apm_cli.commands.compile.cli._get_console", return_value=None),
            patch(
                "apm_cli.commands.compile.cli._rich_info",
                side_effect=lambda m, **_: captured.append(m),
            ),
        ):
            self._call(_make_stats(), "PRESERVED", None, Path("AGENTS.md"), False)

        constitution_lines = [m for m in captured if "Constitution" in m or "hash" in m.lower()]
        assert any("-" in m for m in constitution_lines)

    def test_with_console_builds_rich_table(self) -> None:
        """When console is present, Rich Table is printed (no exception)."""
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console),
            patch("os.path.getsize", return_value=2048),
        ):
            self._call(_make_stats(), "UPDATED", "deadbeef", Path("AGENTS.md"), False)

        mock_console.print.assert_called_once()
        table_arg = mock_console.print.call_args[0][0]
        # Verify it is a Rich Table-like object (has add_row attribute).
        assert hasattr(table_arg, "add_row")

    def test_with_console_dry_run_shows_preview_size(self) -> None:
        """dry_run=True should render file_size as 0 → 'Preview' in output details."""
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console),
            patch("os.path.getsize", return_value=0),
        ):
            self._call(_make_stats(), "CREATED", "hash1", Path("AGENTS.md"), True)

        mock_console.print.assert_called_once()

    def test_oserror_on_getsize_falls_back_gracefully(self) -> None:
        """OSError from os.path.getsize should be caught and the table still printed."""
        mock_console = MagicMock()

        with (
            patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console),
            patch("os.path.getsize", side_effect=OSError("permission denied")),
        ):
            self._call(_make_stats(), "CREATED", "abc", Path("AGENTS.md"), False)

        # Table row should still be rendered (output_details falls back to name only).
        mock_console.print.assert_called_once()

    def test_console_exception_falls_back_to_rich_info(self) -> None:
        """If the Rich table path raises unexpectedly, fall back to _rich_info."""
        captured: list[str] = []
        broken_console = MagicMock()
        broken_console.print.side_effect = RuntimeError("boom")

        with (
            patch("apm_cli.commands.compile.cli._get_console", return_value=broken_console),
            patch(
                "apm_cli.commands.compile.cli._rich_info",
                side_effect=lambda m, **_: captured.append(m),
            ),
        ):
            # Should not raise
            self._call(_make_stats(), "CREATED", "abc", Path("AGENTS.md"), False)

        assert any("primitives" in m.lower() for m in captured)


# ---------------------------------------------------------------------------
# _display_next_steps
# ---------------------------------------------------------------------------


class TestDisplayNextSteps:
    """Tests for _display_next_steps()."""

    def _call(self, output: str = "AGENTS.md") -> None:
        from apm_cli.commands.compile.cli import _display_next_steps

        _display_next_steps(output)

    def test_no_console_prints_via_click_echo(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When console is None, steps are echoed via click.echo."""
        with patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            self._call("AGENTS.md")

        captured = capsys.readouterr()
        assert "apm install" in captured.out or "apm run" in captured.out

    def test_with_console_prints_panel(self) -> None:
        """When console is available, a Rich Panel is printed."""
        mock_console = MagicMock()
        with patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console):
            self._call("AGENTS.md")

        mock_console.print.assert_called_once()

    def test_import_error_falls_back_to_rich_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        """ImportError inside the console block falls back to _rich_info + click.echo."""
        broken_console = MagicMock()
        broken_console.print.side_effect = ImportError("rich not here")

        with patch("apm_cli.commands.compile.cli._get_console", return_value=broken_console):
            self._call("AGENTS.md")
        out = "".join(capsys.readouterr())
        assert "* Review the generated AGENTS.md file" in out
        assert "* Install MCP dependencies: apm install" in out

    def test_next_steps_mention_output_filename(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The next-steps text should reference the provided output file."""
        with patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            self._call("MY_AGENTS.md")

        captured = capsys.readouterr()
        assert "MY_AGENTS.md" in captured.out


# ---------------------------------------------------------------------------
# _display_validation_errors
# ---------------------------------------------------------------------------


class TestDisplayValidationErrors:
    """Tests for _display_validation_errors()."""

    def _call(self, errors: list[Any]) -> None:
        from apm_cli.commands.compile.cli import _display_validation_errors

        _display_validation_errors(errors)

    def test_no_console_falls_back_to_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When console is None, errors are printed via click.echo."""
        errors = ["file.md: Missing 'description' in frontmatter"]
        with (
            patch("apm_cli.commands.compile.cli._get_console", return_value=None),
            patch("apm_cli.commands.compile.cli._rich_error", return_value=None),
        ):
            self._call(errors)

        captured = capsys.readouterr()
        assert "file.md" in captured.err or "Missing" in captured.err

    def test_with_console_renders_rich_table(self) -> None:
        """When console is present, a Rich Table is printed."""
        mock_console = MagicMock()
        errors = ["path/to/file.md: Missing 'description' in frontmatter"]

        with patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console):
            self._call(errors)

        mock_console.print.assert_called_once()
        table_arg = mock_console.print.call_args[0][0]
        assert hasattr(table_arg, "add_row")

    def test_error_without_colon_uses_unknown_filename(self) -> None:
        """Errors without ':' should assign 'Unknown' as the file name."""
        mock_console = MagicMock()
        rows_added: list[tuple[Any, ...]] = []

        def capture_row(*args: Any) -> None:
            rows_added.append(args)

        mock_console.print.return_value = None

        with patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console):
            with patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console):
                self._call(["Empty content in file"])

        mock_console.print.assert_called()

    def test_import_error_falls_back_to_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        """ImportError/NameError inside console block falls back to text output."""
        broken_console = MagicMock()
        broken_console.print.side_effect = NameError("Table")

        errors = ["file.md: some error"]
        with (
            patch("apm_cli.commands.compile.cli._get_console", return_value=broken_console),
            patch("apm_cli.commands.compile.cli._rich_error", return_value=None),
        ):
            self._call(errors)

        captured = capsys.readouterr()
        assert "file.md" in captured.err or "some error" in captured.err

    def test_empty_errors_list(self) -> None:
        """Empty errors list should not raise."""
        mock_console = MagicMock()
        with patch("apm_cli.commands.compile.cli._get_console", return_value=mock_console):
            self._call([])  # should not raise


# ---------------------------------------------------------------------------
# _get_validation_suggestion
# ---------------------------------------------------------------------------


class TestGetValidationSuggestion:
    """Tests for _get_validation_suggestion()."""

    def _call(self, error_msg: str) -> str:
        from apm_cli.commands.compile.cli import _get_validation_suggestion

        return _get_validation_suggestion(error_msg)

    def test_missing_description_suggestion(self) -> None:
        result = self._call("Missing 'description' in frontmatter")
        assert "description:" in result

    def test_apply_to_globally_suggestion(self) -> None:
        result = self._call("applyTo globally scoped is deprecated")
        assert "applyTo" in result

    def test_empty_content_suggestion(self) -> None:
        result = self._call("Empty content in file")
        assert "content" in result.lower() or "markdown" in result.lower()

    def test_unknown_error_returns_generic_suggestion(self) -> None:
        result = self._call("Unknown random error message")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_string_for_all_known_patterns(self) -> None:
        """All branches return non-empty strings."""
        cases = [
            "Missing 'description' here",
            "applyTo globally scoped value",
            "Empty content found",
            "Something completely different",
        ]
        from apm_cli.commands.compile.cli import _get_validation_suggestion

        for msg in cases:
            result = _get_validation_suggestion(msg)
            assert isinstance(result, str) and result, f"Empty suggestion for: {msg}"


# ---------------------------------------------------------------------------
# compile command integration via CliRunner
# ---------------------------------------------------------------------------


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


class TestMergeResults:
    """Tests for _merge_results() method."""

    @pytest.fixture
    def compiler(self):
        """Create a compiler instance for testing."""
        return AgentsCompiler(".")

    def test_merge_single_result_passes_through(self, compiler):
        single = CompilationResult(
            success=True, output_path="custom.md", content="hello", warnings=[], errors=[], stats={}
        )
        assert compiler._merge_results([single]) is single

    def test_merge_empty_paths_excluded(self, compiler):
        r1 = CompilationResult(
            success=True, output_path="", content="", warnings=[], errors=[], stats={}
        )
        r2 = CompilationResult(
            success=True, output_path="b.md", content="", warnings=[], errors=[], stats={}
        )
        assert compiler._merge_results([r1, r2]).output_path == "b.md"

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


class TestResolveCompileTarget:
    """Tests for _resolve_compile_target() multi-target list mapping."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("claude", "claude"),
            ("vscode", "vscode"),
            ("all", "all"),
            ("copilot", "vscode"),
        ],
    )
    def test_scalar_target_resolution(self, value, expected):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(value) == expected

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
        """opencode/codex/windsurf must NOT collapse to 'vscode' (which would
        wrongly route copilot-instructions.md). Single-element lists keep the
        bare target name; multi-element lists pick the first present."""
        from apm_cli.commands.compile.cli import _resolve_compile_target

        assert _resolve_compile_target(["opencode"]) == "opencode"
        assert _resolve_compile_target(["codex"]) == "codex"
        assert _resolve_compile_target(["windsurf"]) == "windsurf"
        # Multi-element AGENTS.md-only list collapses to a representative bare
        # target according to deterministic catalog ordering (alphabetical).
        assert _resolve_compile_target(["opencode", "codex"]) == "codex"
        assert _resolve_compile_target(["codex", "windsurf"]) == "codex"

    @pytest.mark.parametrize(
        "target",
        [
            "cursor",
            ["cursor"],
            "agent-skills",
            ["agent-skills"],
            "intellij",
            ["intellij"],
            "hermes",
            ["hermes"],
            "grok-build",
            ["grok-build"],
            "grok-cloud",
            ["grok-cloud"],
        ],
    )
    def test_accepted_target_scalar_and_list_forms_resolve_identically(self, target):
        """Every accepted target uses the same resolver path for both input shapes."""
        from apm_cli.commands.compile.cli import _resolve_compile_target

        target_name = target if isinstance(target, str) else target[0]
        assert _resolve_compile_target(target) == _resolve_compile_target(target_name)

    def test_windsurf_routes_via_agents_family(self):
        """Regression: windsurf must route through the 'agents' compile_family
        the same way opencode/codex do.  Before the registry-driven
        refactor, windsurf was missing from agents_md_family and would have
        silently collapsed to the 'vscode' fallback (emitting copilot-
        instructions.md by mistake)."""
        from apm_cli.commands.compile.cli import _resolve_compile_target

        # Combined with claude/gemini -> frozenset of families
        assert _resolve_compile_target(["windsurf", "claude"]) == frozenset({"agents", "claude"})
        assert _resolve_compile_target(["windsurf", "gemini"]) == frozenset({"agents", "gemini"})
        # Combined with copilot/vscode -> frozenset preserved because windsurf
        # is a non-Copilot agents-family target that needs instructions in
        # AGENTS.md (issue #1678 -- dedup must not fire for mixed targets).
        assert _resolve_compile_target(["windsurf", "copilot"]) == frozenset({"agents", "vscode"})

    def test_list_codex_and_claude_returns_agents_claude_set(self):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        # No 'vscode' family because copilot/vscode/agents was not requested;
        # codex contributes only the 'agents' (AGENTS.md) family.
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
        assert _resolve_compile_target(["claude", "vscode", "codex"]) == frozenset(
            {"vscode", "agents", "claude"}
        )

    def test_all_plus_explicit_compile_target_excludes_non_compile_expansion(self):
        """Generic all expansion must not inject install-only targets into compile."""
        from apm_cli.commands.compile.cli import _resolve_compile_target
        from apm_cli.core.target_detection import parse_target_field

        parsed = parse_target_field("all,antigravity")

        assert _resolve_compile_target(parsed) == frozenset(
            {"vscode", "agents", "claude", "gemini"}
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


class TestCompilationConfigNoDedup:
    """CompilationConfig carries the no_dedup field."""

    def test_no_dedup_default_is_false(self):
        """no_dedup must default to False (opt-in, preserves backward-compat)."""
        config = CompilationConfig()
        assert config.no_dedup is False

    def test_no_dedup_can_be_set_true(self):
        """no_dedup can be toggled on via constructor."""
        config = CompilationConfig(no_dedup=True)
        assert config.no_dedup is True

    def test_from_apm_yml_passes_no_dedup_override(self):
        """from_apm_yml propagates the no_dedup kwarg."""
        config = CompilationConfig.from_apm_yml(no_dedup=True)
        assert config.no_dedup is True


class TestCompilationConfigPostInit(unittest.TestCase):
    def test_single_agents_true_sets_strategy(self) -> None:
        config = CompilationConfig(single_agents=True)
        self.assertEqual(config.strategy, "single-file")

    def test_single_agents_false_keeps_default_strategy(self) -> None:
        config = CompilationConfig(single_agents=False)
        self.assertEqual(config.strategy, "distributed")

    def test_exclude_defaults_to_empty_list(self) -> None:
        config = CompilationConfig()
        self.assertEqual(config.exclude, [])

    def test_exclude_none_becomes_empty_list(self) -> None:
        config = CompilationConfig(exclude=None)
        self.assertEqual(config.exclude, [])


class TestLogDelegation(unittest.TestCase):
    def test_log_delegates_to_logger(self) -> None:
        compiler = AgentsCompiler("/tmp")
        logger = MagicMock()
        compiler._logger = logger
        compiler._log("progress", "test message", symbol="info")
        logger.progress.assert_called_once_with("test message", symbol="info")

    def test_log_noop_when_no_logger(self) -> None:
        compiler = AgentsCompiler("/tmp")
        compiler._logger = None
        # Should not raise
        compiler._log("progress", "test")
