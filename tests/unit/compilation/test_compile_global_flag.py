"""Unit tests for compile --global CLI flag.

Covers the _handle_global_flag function and --global integration in the compile command:

* _handle_global_flag: error when apm_modules missing
* _handle_global_flag: success when results present
* _handle_global_flag: result status printing (written, unchanged, would-write, etc.)
* _handle_global_flag: error accumulation and exit code
* compile command: --global with --watch rejected
* compile command: --global with --root rejected
* compile command: --global without errors exits 0
* _handle_global_flag: honors targets: declared in the user manifest (#2768)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    target: str,
    path: str | None,
    status: str,
    *,
    has_critical_security: bool = False,
) -> SimpleNamespace:
    """Create a result object as returned by compile_user_root_contexts."""
    return SimpleNamespace(
        target=target,
        path=Path(path) if path else None,
        status=status,
        has_critical_security=has_critical_security,
    )


# ---------------------------------------------------------------------------
# _handle_global_flag tests
# ---------------------------------------------------------------------------


class TestHandleGlobalFlag:
    """Tests for _handle_global_flag()."""

    def test_no_apm_modules_returns_error(self, tmp_path):
        """apm_modules missing -> returns 1 and prints error."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        # apm_modules does NOT exist

        logger = MagicMock()

        with patch(
            "apm_cli.core.scope.get_apm_dir",
            return_value=source_root,
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        logger.error.assert_called_once()
        assert "apm_modules not found" in str(logger.error.call_args).lower()

    def test_success_written_status(self, tmp_path):
        """Result with 'written' status -> prints [+] and returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "written")]

        logger = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 0
        calls_str = str(logger.success.call_args_list)
        assert "[+]" in calls_str or "claude" in calls_str.lower()

    def test_success_would_write_status(self, tmp_path):
        """Result with 'would-write' status -> prints [*] and returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "would-write")]

        logger = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=True, logger=logger)

        assert rc == 0
        calls_str = str(logger.info.call_args_list)
        assert "[*]" in calls_str or "would" in calls_str.lower()

    def test_success_unchanged_status(self, tmp_path):
        """Result with 'unchanged' status -> returns 0 without default detail."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "unchanged")]

        logger = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 0
        logger.info.assert_called_once_with(
            "No user-scope root context files changed.", symbol="info"
        )

    def test_success_skipped_no_instructions(self, tmp_path):
        """Result with 'skipped-no-instructions' -> returns 0 without default detail."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", None, "skipped-no-instructions")]

        logger = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 0
        logger.info.assert_called_once_with(
            "No user-scope root context files changed.", symbol="info"
        )

    def test_success_skipped_hand_authored(self, tmp_path):
        """Result with 'skipped-hand-authored' -> prints [i] and returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [
            _make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "skipped-hand-authored")
        ]

        logger = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 0
        logger.info.assert_any_call(
            f"claude: skipped (hand-authored) {tmp_path / '.claude/CLAUDE.md'}",
            symbol="info",
        )

    def test_error_status_returns_1(self, tmp_path):
        """Result with 'error:...' status -> prints [x] and returns 1."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "error:disk full")]

        logger = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        logger.error.assert_called()

    def test_critical_security_result_returns_1(self, tmp_path):
        """Critical security result -> returns 1 even when status is written."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [
            _make_result(
                "claude",
                str(tmp_path / ".claude/CLAUDE.md"),
                "written",
                has_critical_security=True,
            )
        ]

        logger = MagicMock()
        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1

    def test_multiple_results_mixed_status(self, tmp_path):
        """Multiple results with different status values."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [
            _make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "written"),
            _make_result("vscode", str(tmp_path / ".vscode/AGENTS.md"), "unchanged"),
        ]

        logger = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 0
        logger.success.assert_called()

    def test_no_results_returns_success(self, tmp_path):
        """Empty results list -> returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = []

        logger = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 0
        logger.info.assert_called_once()


# ---------------------------------------------------------------------------
# compile command --global integration tests
# ---------------------------------------------------------------------------


class TestCompileGlobalCommand:
    """Tests for compile command with --global flag."""

    def test_global_with_watch_rejected(self):
        """--global and --watch together -> Click usage error."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        runner = CliRunner()

        result = runner.invoke(compile_cmd, ["--global", "--watch"])

        assert result.exit_code == 2
        assert "global" in result.output.lower()
        assert "watch" in result.output.lower()
        assert "Usage:" in result.output

    def test_global_with_root_rejected(self):
        """--global and --root together -> Click usage error."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        runner = CliRunner()

        result = runner.invoke(compile_cmd, ["--global", "--root", "/nonexistent"])

        assert result.exit_code == 2
        assert "global" in result.output.lower()
        assert "root" in result.output.lower()
        assert "Usage:" in result.output

    def test_global_with_target_rejected(self):
        """--global and --target together -> Click usage error."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        result = CliRunner().invoke(compile_cmd, ["--global", "--target", "claude"])

        assert result.exit_code == 2
        assert "global" in result.output.lower()
        assert "target" in result.output.lower()
        assert "Usage:" in result.output

    def test_global_with_output_rejected(self):
        """--global and --output together -> Click usage error."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        result = CliRunner().invoke(compile_cmd, ["--global", "--output", "AGENTS.md"])

        assert result.exit_code == 2
        assert "global" in result.output.lower()
        assert "output" in result.output.lower()
        assert "Usage:" in result.output

    def test_global_success_no_exit(self, tmp_path):
        """--global with successful _handle_global_flag -> returns normally."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        runner = CliRunner()

        source_root = tmp_path / "source"
        source_root.mkdir()

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch(
                "apm_cli.commands.compile.cli._handle_global_flag",
                return_value=0,
            ),
        ):
            # Invoke with --global; should return 0 (success)
            result = runner.invoke(compile_cmd, ["--global"], standalone_mode=False)

            # Runner exit_code should be 0
            assert result.exit_code == 0

    def test_global_failure_exits_1(self, tmp_path):
        """--global with _handle_global_flag returning 1 -> sys.exit(1)."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        runner = CliRunner()

        source_root = tmp_path / "source"
        source_root.mkdir()

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch(
                "apm_cli.commands.compile.cli._handle_global_flag",
                return_value=1,
            ),
        ):
            # Invoke with --global; _handle_global_flag returns 1 -> sys.exit(1)
            result = runner.invoke(compile_cmd, ["--global"])

            # Runner exit_code should be 1
            assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Declared-target restriction (#2768)
# ---------------------------------------------------------------------------


class TestGlobalCompileHonorsDeclaredTargets:
    """`apm compile -g` must compile only the targets ~/.apm/apm.yml declares."""

    @staticmethod
    def _prepare(tmp_path: Path, manifest: str | None) -> Path:
        """Build a user-scope apm dir, optionally carrying an apm.yml."""
        source_root = tmp_path / "source"
        (source_root / "apm_modules").mkdir(parents=True)
        if manifest is not None:
            (source_root / "apm.yml").write_text(manifest)
        return source_root

    @staticmethod
    def _compiled_target_names(compile_mock: MagicMock) -> list[str]:
        """Extract the target names handed to compile_user_root_contexts."""
        profiles = compile_mock.call_args.args[0]
        return [p.name for p in profiles]

    def _run(self, source_root: Path, logger: MagicMock) -> MagicMock:
        from apm_cli.commands.compile.cli import _handle_global_flag

        compile_mock = MagicMock(return_value=[])
        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            assert _handle_global_flag(dry_run=False, logger=logger) == 0
        return compile_mock

    def test_declared_targets_restrict_compiled_set(self, tmp_path):
        """targets: [claude, codex] -> only those two are compiled."""
        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntargets: [claude, codex]\n",
        )

        names = self._compiled_target_names(self._run(source_root, MagicMock()))

        assert sorted(names) == ["claude", "codex"]

    def test_declared_targets_exclude_explicit_only_and_gated_targets(self, tmp_path):
        """A narrow targets: must not drag in antigravity or gated harnesses."""
        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntargets: [claude]\n",
        )

        names = self._compiled_target_names(self._run(source_root, MagicMock()))

        assert names == ["claude"]
        assert "antigravity" not in names
        assert "hermes" not in names

    def test_singular_target_key_is_honored(self, tmp_path):
        """The singular `target:` sugar restricts the set the same way."""
        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntarget: codex\n",
        )

        names = self._compiled_target_names(self._run(source_root, MagicMock()))

        assert names == ["codex"]

    def test_no_declared_targets_compiles_every_known_target(self, tmp_path):
        """No targets: key -> unchanged behavior, the full known set is passed."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        source_root = self._prepare(tmp_path, "name: h\nversion: 1.0.0\n")

        names = self._compiled_target_names(self._run(source_root, MagicMock()))

        assert sorted(names) == sorted(p.name for p in KNOWN_TARGETS.values())

    def test_absent_manifest_compiles_every_known_target(self, tmp_path):
        """No ~/.apm/apm.yml at all -> unchanged behavior."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        source_root = self._prepare(tmp_path, None)

        names = self._compiled_target_names(self._run(source_root, MagicMock()))

        assert sorted(names) == sorted(p.name for p in KNOWN_TARGETS.values())
