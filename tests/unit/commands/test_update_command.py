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
from unittest.mock import patch

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


class TestUpdateNonTty:
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
