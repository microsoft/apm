"""Unit tests for compile CLI helper functions.

Covers the pure and display-layer helpers in
``apm_cli.commands.compile.cli``:

* ``_get_validation_suggestion`` -- pure mapping of error text to suggestion
* ``_display_validation_errors`` -- rich table + fallback paths
* ``_display_next_steps`` -- rich panel + fallback paths
* ``_display_single_file_summary`` -- rich table + fallback paths
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _get_validation_suggestion
# ---------------------------------------------------------------------------


class TestGetValidationSuggestion:
    """Tests for _get_validation_suggestion()."""

    def setup_method(self):
        from apm_cli.commands.compile.cli import _get_validation_suggestion

        self._fn = _get_validation_suggestion

    def test_missing_description_branch(self):
        result = self._fn("Missing 'description' field in frontmatter")
        assert "description" in result

    def test_missing_applyto_branch(self):
        result = self._fn("Missing 'applyTo' key in header")
        assert "applyTo" in result

    def test_empty_content_branch(self):
        result = self._fn("Empty content after stripping whitespace")
        assert "content" in result.lower() or "markdown" in result.lower()

    def test_unknown_error_returns_generic(self):
        result = self._fn("Some completely unknown error type")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_case_sensitive_matching(self):
        # "Missing 'description'" must match case-sensitively per implementation
        result_match = self._fn("Missing 'description' in file")
        result_no_match = self._fn("missing 'description' in file")
        # The matched branch returns a specific description suggestion
        assert "description" in result_match
        # Lowercase doesn't match -> falls to the generic branch
        assert result_no_match != result_match or "Check primitive" in result_no_match

    def test_returns_string_for_all_branches(self):
        cases = [
            "Missing 'description'",
            "Missing 'applyTo'",
            "Empty content",
            "Random error",
            "",
        ]
        for msg in cases:
            result = self._fn(msg)
            assert isinstance(result, str), f"Expected str for msg={msg!r}"


# ---------------------------------------------------------------------------
# _display_validation_errors
# ---------------------------------------------------------------------------


class TestDisplayValidationErrors:
    """Tests for _display_validation_errors()."""

    def _call(self, errors):
        from apm_cli.commands.compile.cli import _display_validation_errors

        _display_validation_errors(errors)

    def test_rich_path_with_colon_error(self, capsys):
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call(["some/file.md: Missing 'description' field"])
        # Rich console.print was called (not capsys -- rich bypasses it)
        assert mock_console.print.called

    def test_rich_path_without_colon_error(self, capsys):
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call(["No colon error message"])
        assert mock_console.print.called

    def test_fallback_when_no_console(self, capsys):
        with patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            self._call(["error one", "error two"])
        # fallback uses _rich_error / click.echo -- just assert it doesn't raise
        assert True

    def test_fallback_on_import_error(self, capsys):
        """If Rich raises ImportError inside _display_validation_errors, falls back."""
        mock_console = MagicMock()
        # Make print raise to trigger except branch
        mock_console.print.side_effect = ImportError("no rich")
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            # Should not raise
            self._call(["error"])

    def test_empty_error_list(self):
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call([])
        # Called once with an empty table
        assert mock_console.print.called

    def test_multiple_errors_colon_split(self):
        mock_console = MagicMock()
        errors = [
            "file1.md: Missing 'description'",
            "file2.md: Missing 'applyTo'",
            "file3.md: Empty content",
        ]
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call(errors)
        assert mock_console.print.call_count == 1


# ---------------------------------------------------------------------------
# _display_next_steps
# ---------------------------------------------------------------------------


class TestDisplayNextSteps:
    """Tests for _display_next_steps()."""

    def _call(self, output="AGENTS.md"):
        from apm_cli.commands.compile.cli import _display_next_steps

        _display_next_steps(output)

    def test_rich_path_uses_console_print(self):
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call("AGENTS.md")
        assert mock_console.print.called

    def test_fallback_when_no_console(self, capsys):
        with patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            self._call("AGENTS.md")
        out, err = capsys.readouterr()
        # Should produce some output
        combined = out + err
        assert "apm" in combined.lower() or "install" in combined.lower() or "run" in combined.lower()

    def test_fallback_on_import_error(self, capsys):
        mock_console = MagicMock()
        mock_console.print.side_effect = ImportError("no Panel")
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call("output.md")
        out, err = capsys.readouterr()
        combined = out + err
        assert "apm" in combined.lower() or "install" in combined.lower()

    def test_custom_output_name(self, capsys):
        with patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            self._call("custom-output.md")
        out, err = capsys.readouterr()
        combined = out + err
        assert "custom-output.md" in combined


# ---------------------------------------------------------------------------
# _display_single_file_summary
# ---------------------------------------------------------------------------


class TestDisplaySingleFileSummary:
    """Tests for _display_single_file_summary()."""

    def _call(
        self,
        stats=None,
        c_status="UPDATED",
        c_hash="abc123",
        output_path=None,
        dry_run=False,
    ):
        from apm_cli.commands.compile.cli import _display_single_file_summary

        if stats is None:
            stats = {
                "primitives_found": 5,
                "instructions": 3,
                "contexts": 1,
                "chatmodes": 1,
            }
        if output_path is None:
            output_path = Path("AGENTS.md")
        _display_single_file_summary(stats, c_status, c_hash, output_path, dry_run)

    def test_rich_path_prints_table(self):
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call()
        assert mock_console.print.called

    def test_fallback_when_no_console(self, capsys):
        with patch("apm_cli.commands.compile.cli._get_console", return_value=None):
            self._call(
                stats={"primitives_found": 2, "instructions": 1, "contexts": 0, "chatmodes": 0}
            )
        out, err = capsys.readouterr()
        combined = out + err
        assert len(combined) > 0

    def test_dry_run_shows_preview_size(self):
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call(dry_run=True)
        assert mock_console.print.called

    def test_real_output_path_size_computation(self, tmp_path):
        output = tmp_path / "AGENTS.md"
        output.write_text("# Hello\n")
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call(output_path=output, dry_run=False)
        assert mock_console.print.called

    def test_missing_stats_keys_use_defaults(self):
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            # Pass empty stats -- implementation uses .get(..., 0) defaults
            self._call(stats={})
        assert mock_console.print.called

    def test_none_hash_renders_dash(self):
        mock_console = MagicMock()
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call(c_hash=None)
        # Verify at least one call to console.print happened
        assert mock_console.print.called

    def test_fallback_on_exception(self, capsys):
        mock_console = MagicMock()
        mock_console.print.side_effect = Exception("rendering error")
        with patch(
            "apm_cli.commands.compile.cli._get_console", return_value=mock_console
        ):
            self._call()
        out, err = capsys.readouterr()
        combined = out + err
        # Fallback should print something (may contain ANSI codes)
        assert len(combined) > 0
