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

import pytest
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

        logger = MagicMock()
        names = self._compiled_target_names(self._run(source_root, logger))

        assert sorted(names) == ["claude", "codex"]
        logger.verbose_detail.assert_called_once_with(
            "Global targets from ~/.apm/apm.yml: claude, codex"
        )

    def test_declared_target_without_root_output_has_accurate_message(self, tmp_path):
        """A valid no-output target does not suggest reinstalling packages."""
        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntarget: agent-skills\n",
        )
        logger = MagicMock()

        self._run(source_root, logger)

        logger.info.assert_called_once_with(
            "Declared global targets (agent-skills) produce no user-scope "
            "root context output. No files changed.",
            symbol="info",
        )

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

    def test_vscode_alias_is_normalized_to_a_known_profile(self, tmp_path):
        """`targets: [vscode]` is manifest-legal but not a KNOWN_TARGETS key."""
        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntargets: [vscode]\n",
        )

        logger = MagicMock()
        names = self._compiled_target_names(self._run(source_root, logger))

        assert names == ["copilot"]
        logger.verbose_detail.assert_called_once_with(
            "Global targets from ~/.apm/apm.yml: vscode -> copilot"
        )

    def test_alias_and_canonical_name_collapse_to_one_profile(self, tmp_path):
        """vscode and copilot name the same target, so it is compiled once."""
        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntargets: [vscode, copilot]\n",
        )

        names = self._compiled_target_names(self._run(source_root, MagicMock()))

        assert names == ["copilot"]

    def test_malformed_manifest_fails_closed_without_compiling(self, tmp_path):
        """Broken YAML must not degrade to "nothing declared" and write all 11."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntargets: [claude\n  broken: [[[\n",
        )
        logger = MagicMock()
        compile_mock = MagicMock(return_value=[])

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        compile_mock.assert_not_called()
        error = logger.error.call_args.args[0].lower()
        assert "failed to parse" in error
        assert "fix the manifest and rerun 'apm compile -g'" in error

    @pytest.mark.windows_compat
    def test_invalid_target_name_has_global_manifest_recovery(self, tmp_path):
        """An unknown token identifies the manifest and global command."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntargets: [not-a-harness]\n",
        )
        logger = MagicMock()
        compile_mock = MagicMock(return_value=[])

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        compile_mock.assert_not_called()
        error = logger.error.call_args.args[0].lower()
        assert "unknown target 'not-a-harness'" in error
        assert str(source_root / "apm.yml").lower() in error
        assert "rerun 'apm compile -g'" in error
        assert "apm install" not in error

    def test_unreadable_manifest_fails_closed_without_compiling(self, tmp_path):
        """A manifest read error must not expand to every known target."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(
            tmp_path,
            "name: h\nversion: 1.0.0\ntargets: [claude]\n",
        )
        logger = MagicMock()
        compile_mock = MagicMock(return_value=[])

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
            patch(
                "apm_cli.utils.yaml_io.load_yaml",
                side_effect=PermissionError("permission denied"),
            ),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        compile_mock.assert_not_called()
        error = str(logger.error.call_args).lower()
        assert "failed to read" in error
        assert "permission denied" in error
        assert "ensure it is a readable yaml file" in error
        assert "rerun 'apm compile -g'" in error

    def test_non_mapping_manifest_fails_closed_without_compiling(self, tmp_path):
        """A YAML sequence parses cleanly but is not a usable manifest."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(tmp_path, "- claude\n- codex\n")
        logger = MagicMock()
        compile_mock = MagicMock(return_value=[])

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        compile_mock.assert_not_called()
        assert "must contain a yaml object" in str(logger.error.call_args).lower()

    def test_scalar_manifest_fails_closed_without_compiling(self, tmp_path):
        """A bare scalar document is rejected the same way a sequence is."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(tmp_path, "just-a-string\n")
        compile_mock = MagicMock(return_value=[])

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            rc = _handle_global_flag(dry_run=False, logger=MagicMock())

        assert rc == 1
        compile_mock.assert_not_called()

    def test_empty_manifest_fails_closed_without_compiling(self, tmp_path):
        """An empty existing manifest is invalid and must not widen output."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(tmp_path, "")
        logger = MagicMock()
        compile_mock = MagicMock(return_value=[])

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        compile_mock.assert_not_called()
        assert "empty document" in str(logger.error.call_args).lower()

    def test_dangling_manifest_symlink_fails_closed_without_compiling(self, tmp_path):
        """A present dangling manifest link is not mistaken for no manifest."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(tmp_path, None)
        manifest_path = source_root / "apm.yml"
        try:
            manifest_path.symlink_to(source_root / "missing-apm.yml")
        except OSError:
            pytest.skip("symbolic links are unavailable")
        compile_mock = MagicMock(return_value=[])
        logger = MagicMock()

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        compile_mock.assert_not_called()
        assert "failed to read" in str(logger.error.call_args).lower()

    def test_valid_manifest_symlink_fails_closed_without_compiling(self, tmp_path):
        """A manifest link is rejected instead of following its target."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(tmp_path, None)
        external_manifest = tmp_path / "external-apm.yml"
        external_manifest.write_text("targets: [claude]\n", encoding="utf-8")
        try:
            (source_root / "apm.yml").symlink_to(external_manifest)
        except OSError:
            pytest.skip("symbolic links are unavailable")
        compile_mock = MagicMock(return_value=[])
        logger = MagicMock()

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        compile_mock.assert_not_called()
        assert "regular, non-symlink file" in str(logger.error.call_args).lower()

    def test_undecodable_manifest_fails_closed_without_compiling(self, tmp_path):
        """Invalid UTF-8 reaches the handler as a YAMLError, not a raw decode error.

        ``load_yaml`` opens the manifest as UTF-8, so bad bytes raise
        ``UnicodeDecodeError``; ``_bounded_load`` normalizes that ``ValueError``
        into ``yaml.YAMLError``. This pins that normalization, because without it
        the decode failure would escape the handler uncaught.
        """
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = self._prepare(tmp_path, None)
        (source_root / "apm.yml").write_bytes(b"\xff\xfename: h\n")
        logger = MagicMock()
        compile_mock = MagicMock(return_value=[])

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch("apm_cli.compilation.compile_user_root_contexts", compile_mock),
        ):
            rc = _handle_global_flag(dry_run=False, logger=logger)

        assert rc == 1
        compile_mock.assert_not_called()
        assert "unicodedecodeerror" in str(logger.error.call_args).lower()
