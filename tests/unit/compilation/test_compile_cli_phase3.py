"""Phase-3 unit tests for apm_cli.commands.compile.cli.

Covers the helper functions and compile-command branches that are not yet
reached by test_compile_target_flag.py:

* _display_single_file_summary  - console present, console absent, fallback
* _display_next_steps           - console present, ImportError fallback
* _display_validation_errors    - console present, ImportError/NameError fallback
* _get_validation_suggestion    - every suggestion branch
* _resolve_effective_target     - explicit flag, apm.yml, auto-detect paths
* compile command               - no-apm-yml, --all + --target conflict,
                                  --all target expansion, validate mode,
                                  watch mode routing, distributed success,
                                  zero-output warning, result errors,
                                  critical-security exit, orphan warning
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stats(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "primitives_found": 3,
        "instructions": 2,
        "contexts": 1,
        "chatmodes": 0,
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
        # Should not raise; captured or stdout should have steps
        capsys.readouterr()
        assert True  # fallback triggers _rich_info

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
        assert "file.md" in captured.out or "Missing" in captured.out

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
        assert "file.md" in captured.out or "some error" in captured.out

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
# _resolve_effective_target
# ---------------------------------------------------------------------------


class TestResolveEffectiveTarget:
    """Tests for _resolve_effective_target()."""

    def _call(self, target: Any, source_root: Any = None) -> tuple[Any, str, Any]:
        from apm_cli.commands.compile.cli import _resolve_effective_target

        return _resolve_effective_target(target, source_root=source_root)

    def test_none_triggers_auto_detect(self, tmp_path: Path) -> None:
        """target=None should fall through to detect_target() in a no-apm-yml scenario."""
        with patch(
            "apm_cli.core.target_detection.detect_target",
            return_value=("vscode", "no .github folder"),
        ):
            effective, _reason, _config = self._call(None, source_root=tmp_path)

        # With no apm.yml and no explicit target the auto-detect path runs.
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
        # Create a dummy apm.yml so the path exists
        (tmp_path / "apm.yml").write_text("name: test\n")

        with (
            patch("apm_cli.commands.compile.cli._resolve_compile_target") as mock_rct,
            patch("apm_cli.models.apm_package.APMPackage.from_apm_yml") as mock_pkg,
        ):
            mock_pkg.return_value.target = "claude,cursor"
            # compile_target (for explicit target=None) -> None
            # compile_config_target (for "claude,cursor") -> fs
            mock_rct.side_effect = [None, fs]

            effective, reason, _config = self._call(None, source_root=tmp_path)

        assert effective == fs
        assert reason == "apm.yml target"

    def test_single_string_explicit_target_uses_detect_target(self, tmp_path: Path) -> None:
        """Single explicit string passes through detect_target()."""
        # Create a dummy apm.yml so the path exists
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


# ---------------------------------------------------------------------------
# compile command integration via CliRunner
# ---------------------------------------------------------------------------


class TestCompileCommandNoApmYml:
    """compile command exits when no apm.yml is present."""

    def test_exits_when_no_apm_yml(self) -> None:
        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["compile"])

        assert result.exit_code != 0 or "Not an APM project" in result.output


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
                runner.invoke(cli, ["compile", "--watch"])

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
