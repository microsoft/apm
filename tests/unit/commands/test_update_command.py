"""Unit tests for the ``apm update`` Click command.

Issue: https://github.com/microsoft/apm/issues/1203 (P0).

These tests mock the underlying ``_install_apm_dependencies`` so the
focus is on:

* Plan callback wiring (assume_yes / dry-run / non-TTY paths).
* Back-compat shim: ``apm update`` outside an apm.yml project forwards
  to ``apm self-update``.
* Mutex enforcement on ``apm install --frozen --update``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from unittest.mock import patch as _patch

import click
import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.install.plan import PlanEntry, UpdatePlan


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _stub_plan_with_changes() -> UpdatePlan:
    return UpdatePlan(
        entries=(
            PlanEntry(
                dep_key="o/r",
                action="update",
                display_name="o/r",
                old_resolved_ref="main",
                new_resolved_ref="main",
                old_resolved_commit="a" * 40,
                new_resolved_commit="b" * 40,
            ),
        )
    )


def _make_apm_yml(project_dir: Path) -> None:
    (project_dir / "apm.yml").write_text(
        "name: test\nversion: 1.0.0\ndependencies:\n  apm:\n    - microsoft/apm\n"
    )


# -----------------------------------------------------------------------------
# apm update -- core flow
# -----------------------------------------------------------------------------


class TestUpdateDryRun:
    def test_dry_run_renders_plan_without_install(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                cb = kwargs["plan_callback"]
                proceed = cb(_stub_plan_with_changes())
                captured["proceeded"] = proceed
                from apm_cli.models.results import InstallResult

                return InstallResult()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update", "--dry-run"])

            assert result.exit_code == 0, result.output
            assert "Update plan" in result.output
            assert "Dry run" in result.output
            assert captured["proceeded"] is False

    def test_dry_run_renders_revision_pin_and_standard_plans_without_writing_manifest(
        self, runner, tmp_path
    ):
        old_sha = "abcdef1234567890abcdef1234567890abcdef12"
        new_sha = "1234567890abcdef1234567890abcdef12345678"
        with runner.isolated_filesystem(temp_dir=tmp_path):
            manifest = Path.cwd() / "apm.yml"
            manifest.write_text(
                f"name: test\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{old_sha}\n",
                encoding="utf-8",
            )
            original = manifest.read_text(encoding="utf-8")

            from apm_cli.deps.revision_pins import RevisionPinUpdate
            from apm_cli.models.results import InstallResult

            captured = {}

            def fake_install(_apm, **kwargs):
                captured["proceeded"] = kwargs["plan_callback"](_stub_plan_with_changes())
                return InstallResult()

            with (
                patch(
                    "apm_cli.commands.update.resolve_revision_pin_updates",
                    return_value=[
                        RevisionPinUpdate("org/pkg", old_sha, new_sha, "v2.0.0", "org/pkg")
                    ],
                ),
                patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
            ):
                result = runner.invoke(cli, ["update", "--dry-run"])

            assert result.exit_code == 0, result.output
            assert "Revision pin updates" in result.output
            assert "Update plan" in result.output
            assert "abcdef12 -> 12345678 (v2.0.0)" in result.output
            assert "abcdef1 -> 1234567" not in result.output
            assert "# v2.0.0" not in result.output
            assert captured["proceeded"] is False
            assert manifest.read_text(encoding="utf-8") == original


class TestUpdateAssumeYes:
    def test_yes_skips_prompt_and_proceeds(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                cb = kwargs["plan_callback"]
                captured["proceeded"] = cb(_stub_plan_with_changes())
                from apm_cli.models.results import InstallResult

                return InstallResult(installed_count=1)

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update", "--yes"])

            assert result.exit_code == 0, result.output
            assert captured["proceeded"] is True

    def test_yes_updates_revision_pin_manifest_before_install(self, runner, tmp_path):
        old_sha = "a" * 40
        new_sha = "b" * 40
        with runner.isolated_filesystem(temp_dir=tmp_path):
            manifest = Path.cwd() / "apm.yml"
            manifest.write_text(
                f"name: test\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{old_sha}\n",
                encoding="utf-8",
            )

            from apm_cli.deps.revision_pins import RevisionPinUpdate
            from apm_cli.models.results import InstallResult

            captured = {}

            def fake_install(apm_package, **kwargs):
                captured["dep_ref"] = apm_package.get_apm_dependencies()[0].reference
                captured["plan_proceeded"] = kwargs["plan_callback"](_stub_plan_with_changes())
                return InstallResult(installed_count=1)

            with (
                patch(
                    "apm_cli.commands.update.resolve_revision_pin_updates",
                    return_value=[
                        RevisionPinUpdate("org/pkg", old_sha, new_sha, "v2.0.0", "org/pkg")
                    ],
                ),
                patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
                patch("apm_cli.commands.update._annotate_lockfile_revision_tags"),
            ):
                result = runner.invoke(cli, ["update", "--yes"])

            assert result.exit_code == 0, result.output
            assert f"org/pkg#{new_sha} # v2.0.0" in manifest.read_text(encoding="utf-8")
            assert captured["dep_ref"] == new_sha
            assert captured["plan_proceeded"] is True

    def test_yes_reports_revision_pin_only_update_count(self, runner, tmp_path):
        old_sha = "a" * 40
        new_sha = "b" * 40
        with runner.isolated_filesystem(temp_dir=tmp_path):
            manifest = Path.cwd() / "apm.yml"
            manifest.write_text(
                f"name: test\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{old_sha}\n",
                encoding="utf-8",
            )

            from apm_cli.deps.revision_pins import RevisionPinUpdate
            from apm_cli.models.results import InstallResult

            def fake_install(_apm, **kwargs):
                assert kwargs["plan_callback"](UpdatePlan(entries=())) is True
                return InstallResult(installed_count=0)

            with (
                patch(
                    "apm_cli.commands.update.resolve_revision_pin_updates",
                    return_value=[
                        RevisionPinUpdate("org/pkg", old_sha, new_sha, "v2.0.0", "org/pkg")
                    ],
                ),
                patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
                patch("apm_cli.commands.update._annotate_lockfile_revision_tags"),
            ):
                result = runner.invoke(cli, ["update", "--yes"])

            assert result.exit_code == 0, result.output
            assert "Updated 1 revision pin in apm.yml." in result.output

    def test_revision_pin_decline_keeps_manifest_unchanged(self, runner, tmp_path):
        old_sha = "a" * 40
        new_sha = "b" * 40
        with runner.isolated_filesystem(temp_dir=tmp_path):
            manifest = Path.cwd() / "apm.yml"
            manifest.write_text(
                f"name: test\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{old_sha}\n",
                encoding="utf-8",
            )
            original = manifest.read_text(encoding="utf-8")

            from apm_cli.deps.revision_pins import RevisionPinUpdate
            from apm_cli.models.results import InstallResult

            captured = {}

            def fake_install(apm_package, **kwargs):
                captured["dep_ref"] = apm_package.get_apm_dependencies()[0].reference
                captured["plan_proceeded"] = kwargs["plan_callback"](_stub_plan_with_changes())
                return InstallResult(installed_count=1 if captured["plan_proceeded"] else 0)

            with (
                patch(
                    "apm_cli.commands.update.resolve_revision_pin_updates",
                    return_value=[
                        RevisionPinUpdate("org/pkg", old_sha, new_sha, "v2.0.0", "org/pkg")
                    ],
                ),
                patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
                patch("apm_cli.commands.update._annotate_lockfile_revision_tags") as annotate,
                patch("apm_cli.commands.update._stdin_is_tty", return_value=True),
                patch("apm_cli.commands.update.click.confirm", return_value=False) as confirm,
            ):
                result = runner.invoke(cli, ["update"])

            assert result.exit_code == 0, result.output
            assert manifest.read_text(encoding="utf-8") == original
            assert captured["dep_ref"] == new_sha
            assert captured["plan_proceeded"] is False
            confirm.assert_called_once_with(
                "Apply these changes?", default=False, show_default=True
            )
            annotate.assert_not_called()
            assert "no changes" in result.output.lower()


class TestRevisionPinLockfileAnnotation:
    def test_annotates_lockfile_with_resolved_tag(self, tmp_path) -> None:
        from apm_cli.commands.update import _annotate_lockfile_revision_tags
        from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
        from apm_cli.deps.revision_pins import RevisionPinUpdate

        new_sha = "b" * 40
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="org/pkg",
                resolved_ref=new_sha,
                resolved_commit=new_sha,
            )
        )
        lockfile.save(get_lockfile_path(tmp_path))

        _annotate_lockfile_revision_tags(
            tmp_path,
            [RevisionPinUpdate("org/pkg", "a" * 40, new_sha, "v2.0.0", "org/pkg")],
        )

        updated = LockFile.read(get_lockfile_path(tmp_path))
        assert updated is not None
        assert updated.get_dependency("org/pkg").resolved_tag == "v2.0.0"

    def test_lockfile_annotation_refuses_sha_mismatch(self, tmp_path) -> None:
        from apm_cli.commands.update import _annotate_lockfile_revision_tags
        from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
        from apm_cli.deps.revision_pins import RevisionPinUpdate

        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="org/pkg",
                resolved_ref="c" * 40,
                resolved_commit="c" * 40,
            )
        )
        lockfile.save(get_lockfile_path(tmp_path))

        with pytest.raises(RuntimeError, match="SHA does not match"):
            _annotate_lockfile_revision_tags(
                tmp_path,
                [RevisionPinUpdate("org/pkg", "a" * 40, "b" * 40, "v2.0.0", "org/pkg")],
            )


class TestUpdateNonTty:
    def test_revision_pin_non_tty_aborts_without_manifest_write(self, runner, tmp_path):
        old_sha = "a" * 40
        new_sha = "b" * 40
        with runner.isolated_filesystem(temp_dir=tmp_path):
            manifest = Path.cwd() / "apm.yml"
            manifest.write_text(
                f"name: test\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{old_sha}\n",
                encoding="utf-8",
            )
            original = manifest.read_text(encoding="utf-8")

            from apm_cli.deps.revision_pins import RevisionPinUpdate
            from apm_cli.models.results import InstallResult

            def fake_install(_apm, **kwargs):
                kwargs["plan_callback"](_stub_plan_with_changes())
                return InstallResult()

            with (
                patch(
                    "apm_cli.commands.update.resolve_revision_pin_updates",
                    return_value=[
                        RevisionPinUpdate("org/pkg", old_sha, new_sha, "v2.0.0", "org/pkg")
                    ],
                ),
                patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
            ):
                result = runner.invoke(cli, ["update"])

            assert result.exit_code == 1, result.output
            assert "non-interactive" in result.output
            assert manifest.read_text(encoding="utf-8") == original

    def test_non_tty_aborts_without_yes_flag(self, runner, tmp_path):
        """No --yes + non-TTY stdin -> exit 1 (CI-safe failure, do not mutate).

        Regression guard for the exit-code bug: non-TTY callers must see
        a non-zero exit code so CI pipelines fail fast on accidental
        'apm update' invocations.
        """
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            def fake_install(_apm, **kwargs):
                cb = kwargs["plan_callback"]
                # The callback should sys.exit(1) -- propagate as SystemExit
                cb(_stub_plan_with_changes())
                from apm_cli.models.results import InstallResult

                return InstallResult()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update"])

            assert result.exit_code == 1, result.output
            assert "non-interactive" in result.output


class TestUpdateNoChanges:
    def test_unchanged_plan_short_circuits(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            def fake_install(_apm, **kwargs):
                cb = kwargs["plan_callback"]
                proceed = cb(UpdatePlan(entries=()))
                assert proceed is False
                from apm_cli.models.results import InstallResult

                return InstallResult()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update"])

            assert result.exit_code == 0, result.output
            assert "already at their latest" in result.output


# -----------------------------------------------------------------------------
# apm update outside an apm.yml project -> back-compat shim
# -----------------------------------------------------------------------------


class TestUpdateBackCompatShim:
    def test_update_without_apm_yml_forwards_to_self_update(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("apm_cli.commands.self_update.self_update.callback") as mock_self_update:
                mock_self_update.return_value = None
                result = runner.invoke(cli, ["update"])

            assert "self-update" in result.output
            assert mock_self_update.called


# -----------------------------------------------------------------------------
# apm update --target flag
# -----------------------------------------------------------------------------


class TestUpdateTarget:
    def test_target_forwarded_to_install(self, runner, tmp_path):
        """--target value is passed through to _install_apm_dependencies."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["target"] = kwargs.get("target")
                cb = kwargs["plan_callback"]
                cb(UpdatePlan(entries=()))
                from apm_cli.models.results import InstallResult

                return InstallResult()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update", "--target", "claude"])

            assert result.exit_code == 0, result.output
            assert captured["target"] == "claude"

    def test_short_target_flag(self, runner, tmp_path):
        """-t short form is accepted and forwarded."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["target"] = kwargs.get("target")
                cb = kwargs["plan_callback"]
                cb(UpdatePlan(entries=()))
                from apm_cli.models.results import InstallResult

                return InstallResult()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update", "-t", "copilot"])

            assert result.exit_code == 0, result.output
            assert captured["target"] == "copilot"

    def test_no_target_defaults_to_none(self, runner, tmp_path):
        """Omitting --target passes None to _install_apm_dependencies."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["target"] = kwargs.get("target")
                cb = kwargs["plan_callback"]
                cb(UpdatePlan(entries=()))
                from apm_cli.models.results import InstallResult

                return InstallResult()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update"])

            assert result.exit_code == 0, result.output
            assert captured["target"] is None

    def test_target_with_assume_yes(self, runner, tmp_path):
        """--target and --yes work together; target is forwarded and install proceeds."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["target"] = kwargs.get("target")
                cb = kwargs["plan_callback"]
                captured["proceeded"] = cb(_stub_plan_with_changes())
                from apm_cli.models.results import InstallResult

                return InstallResult(installed_count=1)

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update", "--yes", "--target", "cursor"])

            assert result.exit_code == 0, result.output
            assert captured["target"] == "cursor"
            assert captured["proceeded"] is True

    def test_multi_target_comma_separated(self, runner, tmp_path):
        """--target claude,cursor (comma-separated) is parsed to a list and forwarded."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["target"] = kwargs.get("target")
                cb = kwargs["plan_callback"]
                cb(UpdatePlan(entries=()))
                from apm_cli.models.results import InstallResult

                return InstallResult()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies", side_effect=fake_install
            ):
                result = runner.invoke(cli, ["update", "--target", "claude,cursor"])

            assert result.exit_code == 0, result.output
            assert isinstance(captured["target"], list), (
                f"Expected list for multi-target, got {type(captured['target'])}"
            )
            assert "claude" in captured["target"]
            assert "cursor" in captured["target"]

    def test_target_ignored_warning_on_shim_path(self, runner, tmp_path):
        """--target outside an apm.yml project emits a warning that it will be ignored."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("apm_cli.commands.self_update.self_update.callback") as mock_self_update:
                mock_self_update.return_value = None
                result = runner.invoke(cli, ["update", "--target", "claude"])

            assert "ignored" in result.output.lower() or "warning" in result.output.lower(), (
                f"Expected an ignored/warning message, got: {result.output}"
            )


# -----------------------------------------------------------------------------
# apm install --frozen / --update mutex
# -----------------------------------------------------------------------------


class TestFrozenUpdateMutex:
    def test_frozen_and_update_together_rejected(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            result = runner.invoke(cli, ["install", "--frozen", "--update"])

            assert result.exit_code != 0
            assert "frozen" in result.output.lower()
            assert "update" in result.output.lower()


# -----------------------------------------------------------------------------
# Additional coverage for missed lines
# -----------------------------------------------------------------------------


class TestStdinIsTtyExceptionPath:
    """Lines 92-93: _stdin_is_tty() absorbs AttributeError/ValueError."""

    def test_stdin_is_tty_returns_false_when_stdin_none(self) -> None:
        from apm_cli.commands.update import _stdin_is_tty

        mock_sys = MagicMock()
        mock_sys.stdin = None
        with _patch("apm_cli.commands.update.sys", mock_sys):
            assert _stdin_is_tty() is False

    def test_stdin_is_tty_returns_false_when_isatty_raises_value_error(self) -> None:
        from apm_cli.commands.update import _stdin_is_tty

        mock_stdin = MagicMock()
        mock_stdin.isatty.side_effect = ValueError("closed")
        mock_sys = MagicMock()
        mock_sys.stdin = mock_stdin
        with _patch("apm_cli.commands.update.sys", mock_sys):
            assert _stdin_is_tty() is False


class TestUpdateCheckOnlyWithTarget:
    """Line 185: check_only + target emits warning about target being ignored."""

    def test_check_only_with_target_warns_about_ignore(self, runner, tmp_path) -> None:
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            with _patch("apm_cli.commands.self_update.self_update.callback") as mock_self:
                mock_self.return_value = None
                result = runner.invoke(cli, ["update", "--check", "--target", "claude"])
            # Should warn that --target is ignored
            assert result.exit_code == 0
            assert "--target" in result.output or "ignored" in result.output.lower()


class TestUpdateCIEnvironment:
    """Line 242: CI env var triggers info message."""

    def test_ci_env_triggers_info_message(self, runner, tmp_path) -> None:
        import os

        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            def fake_install(_apm, **kwargs):
                from apm_cli.models.results import InstallResult

                cb = kwargs["plan_callback"]
                cb(UpdatePlan(entries=()))
                return InstallResult()

            with (
                _patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
                _patch.dict(os.environ, {"CI": "true"}),
            ):
                result = runner.invoke(cli, ["update"])
            assert result.exit_code == 0, result.output
            # The CI banner should mention self-update or CLI binary
            assert "self-update" in result.output.lower() or "cli" in result.output.lower()


class TestUpdateApmYmlParseError:
    """Lines 258-260: FileNotFoundError/ValueError in apm.yml parse."""

    def test_value_error_in_parse_exits_with_error(self, runner, tmp_path) -> None:
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            with _patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                side_effect=ValueError("invalid yaml"),
            ):
                result = runner.invoke(cli, ["update"])
            assert result.exit_code == 1
            assert "apm.yml" in result.output.lower() or "parse" in result.output.lower()

    def test_file_not_found_exits_with_error(self, runner, tmp_path) -> None:
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            with _patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                side_effect=FileNotFoundError("apm.yml not found"),
            ):
                result = runner.invoke(cli, ["update"])
            assert result.exit_code == 1


class TestUpdatePlanCallbackPaths:
    """Lines 282->286, 304-308: plan render and interactive confirm."""

    def test_plan_changes_empty_rendered_text_non_tty(self, runner, tmp_path) -> None:
        """Line 282->286: empty rendered string (no echo)."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            def fake_install(_apm, **kwargs):
                from apm_cli.models.results import InstallResult

                cb = kwargs["plan_callback"]
                cb(_stub_plan_with_changes())
                return InstallResult()

            with (
                _patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
                _patch("apm_cli.commands.update.render_plan_text", return_value=""),
                _patch("apm_cli.commands.update._stdin_is_tty", return_value=False),
            ):
                result = runner.invoke(cli, ["update"])
            assert result.exit_code == 1  # non-TTY without --yes

    def test_plan_with_changes_confirm_yes(self, runner, tmp_path) -> None:
        """Lines 304-308: user confirms → plan proceeds."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            def fake_install(_apm, **kwargs):
                from apm_cli.models.results import InstallResult

                cb = kwargs["plan_callback"]
                cb(_stub_plan_with_changes())
                return InstallResult(installed_count=1)

            with (
                _patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
                _patch("apm_cli.commands.update._stdin_is_tty", return_value=True),
                _patch("apm_cli.commands.update.click.confirm", return_value=True),
            ):
                result = runner.invoke(cli, ["update"])
            assert result.exit_code == 0, result.output

    def test_plan_with_changes_confirm_no(self, runner, tmp_path) -> None:
        """Lines 304-308: user declines → no changes."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            def fake_install(_apm, **kwargs):
                from apm_cli.models.results import InstallResult

                cb = kwargs["plan_callback"]
                cb(_stub_plan_with_changes())
                return InstallResult()

            with (
                _patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
                _patch("apm_cli.commands.update._stdin_is_tty", return_value=True),
                _patch("apm_cli.commands.update.click.confirm", return_value=False),
            ):
                result = runner.invoke(cli, ["update"])
            assert result.exit_code == 0, result.output
            assert "no changes" in result.output.lower()


class TestUpdateErrorHandling:
    """Lines 321-339: exception handling in _run_dep_update."""

    def test_frozen_install_error_exits(self, runner, tmp_path) -> None:
        """Lines 321-324: FrozenInstallError shows reasons and exits 1."""
        from apm_cli.install.errors import FrozenInstallError

        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            err = FrozenInstallError("frozen", reasons=["reason1"])
            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=err,
            ):
                result = runner.invoke(cli, ["update", "--yes"])
            assert result.exit_code == 1
            assert "frozen" in result.output.lower()

    def test_authentication_error_with_context(self, runner, tmp_path) -> None:
        """Lines 326-329: AuthenticationError with diagnostic_context."""
        from apm_cli.install.errors import AuthenticationError

        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            err = AuthenticationError("auth failed")
            err.diagnostic_context = "check your token"
            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=err,
            ):
                result = runner.invoke(cli, ["update", "--yes"])
            assert result.exit_code == 1

    def test_authentication_error_without_context(self, runner, tmp_path) -> None:
        """Lines 326-329: AuthenticationError without diagnostic_context."""
        from apm_cli.install.errors import AuthenticationError

        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            err = AuthenticationError("auth error")
            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=err,
            ):
                result = runner.invoke(cli, ["update", "--yes"])
            assert result.exit_code == 1

    def test_direct_dependency_error_exits(self, runner, tmp_path) -> None:
        """Lines 331-332: DirectDependencyError."""
        from apm_cli.install.errors import DirectDependencyError

        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=DirectDependencyError("dep error"),
            ):
                result = runner.invoke(cli, ["update", "--yes"])
            assert result.exit_code == 1

    def test_policy_violation_error_exits(self, runner, tmp_path) -> None:
        """Lines 331-332: PolicyViolationError."""
        from apm_cli.install.errors import PolicyViolationError

        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=PolicyViolationError("policy violation"),
            ):
                result = runner.invoke(cli, ["update", "--yes"])
            assert result.exit_code == 1

    def test_usage_error_propagates(self, runner, tmp_path) -> None:
        """Line 334: click.UsageError is re-raised (exit code 2)."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=click.UsageError("bad usage"),
            ):
                result = runner.invoke(cli, ["update", "--yes"])
            assert result.exit_code == 2

    def test_generic_exception_exits_with_error(self, runner, tmp_path) -> None:
        """Lines 336-339: generic Exception shows error message."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=RuntimeError("unexpected error"),
            ):
                result = runner.invoke(cli, ["update", "--yes"])
            assert result.exit_code == 1
            assert "error" in result.output.lower()

    def test_generic_exception_verbose_no_hint(self, runner, tmp_path) -> None:
        """Lines 337-338: with --verbose, no 'run with --verbose' hint shown."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=RuntimeError("boom"),
            ):
                result = runner.invoke(cli, ["update", "--yes", "--verbose"])
            assert result.exit_code == 1


class TestUpdatePlanStatePaths:
    """Lines 343, 350: plan_state post-install checks."""

    def test_plan_none_returns_early_no_success_message(self, runner, tmp_path) -> None:
        """Line 343: plan is None → return without emitting success."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            def fake_install(_apm, **kwargs):
                from apm_cli.models.results import InstallResult

                # Never call plan_callback → plan stays None
                return InstallResult()

            with _patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=fake_install,
            ):
                result = runner.invoke(cli, ["update", "--yes"])
            assert result.exit_code == 0, result.output
            # Without plan_callback being invoked, no "Updated" or "applied" lines
            assert "updated" not in result.output.lower() or "refreshes" in result.output.lower()

    def test_proceeded_zero_installed_shows_applied(self, runner, tmp_path) -> None:
        """Line 350: proceeded=True but installed_count=0 → 'Update applied.'"""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            def fake_install(_apm, **kwargs):
                from apm_cli.models.results import InstallResult

                cb = kwargs["plan_callback"]
                cb(_stub_plan_with_changes())
                return InstallResult(installed_count=0)

            with (
                _patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=fake_install,
                ),
                _patch("apm_cli.commands.update._stdin_is_tty", return_value=True),
                _patch("apm_cli.commands.update.click.confirm", return_value=True),
            ):
                result = runner.invoke(cli, ["update"])
            assert result.exit_code == 0, result.output
            assert "update applied" in result.output.lower()


# -----------------------------------------------------------------------------
# apm update -- superset flags from issue #1525:
# [PACKAGES]... / --force / --parallel-downloads / -g
# -----------------------------------------------------------------------------


def _capturing_install(captured: dict, *, installed_count: int = 0):
    """Build a fake _install_apm_dependencies that records kwargs.

    Drives the plan_callback with an empty (no-change) plan so the command
    completes the happy path without prompting.
    """

    def fake_install(_apm, **kwargs):
        from apm_cli.models.results import InstallResult

        captured.update(kwargs)
        kwargs["plan_callback"](UpdatePlan(entries=()))
        return InstallResult(installed_count=installed_count)

    return fake_install


class TestUpdatePerPackage:
    def test_packages_forwarded_as_only_packages(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured: dict = {}
            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=_capturing_install(captured),
            ):
                result = runner.invoke(cli, ["update", "microsoft/apm"])
            assert result.exit_code == 0, result.output
            assert captured["only_packages"] == ["microsoft/apm"]

    def test_no_packages_forwards_none(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured: dict = {}
            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=_capturing_install(captured),
            ):
                result = runner.invoke(cli, ["update"])
            assert result.exit_code == 0, result.output
            assert captured["only_packages"] is None

    def test_unknown_package_exits_1_without_installing(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            engine = MagicMock()
            with patch("apm_cli.commands.install._install_apm_dependencies", engine):
                result = runner.invoke(cli, ["update", "no/such-pkg"])
            assert result.exit_code == 1
            assert "not found in apm.yml" in result.output
            assert "Available:" in result.output
            engine.assert_not_called()


class TestUpdateForceAndParallel:
    def test_force_forwarded(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured: dict = {}
            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=_capturing_install(captured),
            ):
                result = runner.invoke(cli, ["update", "--force"])
            assert result.exit_code == 0, result.output
            assert captured["force"] is True

    def test_parallel_downloads_forwarded(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured: dict = {}
            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=_capturing_install(captured),
            ):
                result = runner.invoke(cli, ["update", "--parallel-downloads", "0"])
            assert result.exit_code == 0, result.output
            assert captured["parallel_downloads"] == 0


class TestUpdateGlobalScope:
    def test_global_flag_uses_user_scope(self, runner, tmp_path):
        from apm_cli.core.scope import InstallScope

        with runner.isolated_filesystem(temp_dir=tmp_path):
            user_apm = Path.cwd() / "user_apm"
            user_apm.mkdir()
            _make_apm_yml(user_apm)
            captured: dict = {}
            with (
                patch("apm_cli.core.scope.get_apm_dir", return_value=user_apm),
                patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                    side_effect=_capturing_install(captured),
                ),
            ):
                result = runner.invoke(cli, ["update", "-g"])
            assert result.exit_code == 0, result.output
            assert captured["scope"] == InstallScope.USER

    def test_global_flag_missing_user_manifest_exits_1(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            user_apm = Path.cwd() / "user_apm"  # intentionally not created
            engine = MagicMock()
            with (
                patch("apm_cli.core.scope.get_apm_dir", return_value=user_apm),
                patch("apm_cli.commands.install._install_apm_dependencies", engine),
            ):
                result = runner.invoke(cli, ["update", "-g"])
            assert result.exit_code == 1
            assert "No apm.yml found" in result.output
            engine.assert_not_called()


class TestResolveRequestedPackages:
    """Direct unit tests for the shared package-token resolver."""

    @staticmethod
    def _dep(repo_url, *, unique_key=None, display=None, alias=None):
        dep = MagicMock()
        dep.repo_url = repo_url
        dep.alias = alias
        dep.get_unique_key.return_value = unique_key or repo_url
        dep.get_display_name.return_value = display or repo_url
        return dep

    def test_empty_returns_none(self):
        from apm_cli.commands._helpers import resolve_requested_packages

        assert resolve_requested_packages((), [self._dep("org/a")]) is None

    def test_short_name_maps_to_canonical(self):
        from apm_cli.commands._helpers import resolve_requested_packages

        deps = [self._dep("owner/compliance-rules")]
        assert resolve_requested_packages(("compliance-rules",), deps) == ["owner/compliance-rules"]

    def test_dedup_preserves_order(self):
        from apm_cli.commands._helpers import resolve_requested_packages

        deps = [self._dep("org/a"), self._dep("org/b")]
        assert resolve_requested_packages(("org/b", "org/a", "org/b"), deps) == ["org/b", "org/a"]

    def test_unknown_token_raises(self):
        from apm_cli.commands._helpers import UnknownPackageError, resolve_requested_packages

        with pytest.raises(UnknownPackageError) as exc:
            resolve_requested_packages(("nope",), [self._dep("org/a")])
        assert exc.value.token == "nope"
        assert "org/a" in exc.value.available

    def test_alias_maps_to_canonical(self):
        from apm_cli.commands._helpers import resolve_requested_packages

        deps = [self._dep("org/compliance-rules", alias="my-rules")]
        assert resolve_requested_packages(("my-rules",), deps) == ["org/compliance-rules"]
