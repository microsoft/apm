"""Unit tests for the ``apm lock`` Click command.

Issue: https://github.com/microsoft/apm/issues/975

Focus areas:
* lockfile_only=True is forwarded to _install_apm_dependencies.
* No apm.yml -> error + exit 1.
* Empty deps -> pipeline called (no-op resolution), exits 0. Lockfile
  write is not asserted at the unit tier (integration test covers it).
* Auth errors and policy violations surface correctly.
* --global scope wires up the user-scope manifest path.
* --update forwarded as update_refs=True.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_apm_yml(project_dir: Path, *, with_deps: bool = True) -> None:
    if with_deps:
        content = "name: test\nversion: 1.0.0\ndependencies:\n  apm:\n    - microsoft/apm\n"
    else:
        content = "name: test\nversion: 1.0.0\n"
    (project_dir / "apm.yml").write_text(content)


def _make_install_result():
    from apm_cli.models.results import InstallResult

    return InstallResult(installed_count=1)


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------


class TestLockBasic:
    def test_lock_passes_lockfile_only_flag(self, runner, tmp_path):
        """_install_apm_dependencies must receive lockfile_only=True."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["lockfile_only"] = kwargs.get("lockfile_only")
                return _make_install_result()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=fake_install,
            ):
                result = runner.invoke(cli, ["lock"])

            assert result.exit_code == 0, result.output
            assert captured["lockfile_only"] is True

    def test_lock_success_prints_message(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                return_value=_make_install_result(),
            ):
                result = runner.invoke(cli, ["lock"])

            assert result.exit_code == 0, result.output
            assert "apm.lock.yaml" in result.output

    def test_lock_no_apm_yml_exits_with_error(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["lock"])
        assert result.exit_code == 1
        assert "No apm.yml" in result.output or "apm.yml" in result.output


# ---------------------------------------------------------------------------
# Flag forwarding
# ---------------------------------------------------------------------------


class TestLockFlagForwarding:
    def test_verbose_forwarded(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["verbose"] = kwargs.get("verbose")
                return _make_install_result()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=fake_install,
            ):
                result = runner.invoke(cli, ["lock", "--verbose"])

            assert result.exit_code == 0, result.output
            assert captured["verbose"] is True

    def test_update_forwarded_as_update_refs(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["update_refs"] = kwargs.get("update_refs")
                return _make_install_result()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=fake_install,
            ):
                result = runner.invoke(cli, ["lock", "--update"])

            assert result.exit_code == 0, result.output
            assert captured["update_refs"] is True

    def test_no_policy_forwarded(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["no_policy"] = kwargs.get("no_policy")
                return _make_install_result()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=fake_install,
            ):
                result = runner.invoke(cli, ["lock", "--no-policy"])

            assert result.exit_code == 0, result.output
            assert captured["no_policy"] is True

    def test_parallel_downloads_forwarded(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            captured = {}

            def fake_install(_apm, **kwargs):
                captured["parallel_downloads"] = kwargs.get("parallel_downloads")
                return _make_install_result()

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=fake_install,
            ):
                result = runner.invoke(cli, ["lock", "--parallel-downloads", "2"])

            assert result.exit_code == 0, result.output
            assert captured["parallel_downloads"] == 2


# ---------------------------------------------------------------------------
# Empty deps
# ---------------------------------------------------------------------------


class TestLockEmptyDeps:
    def test_no_deps_still_exits_zero(self, runner, tmp_path):
        """A project with no APM deps should write an empty lockfile and exit 0."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd(), with_deps=False)

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                return_value=_make_install_result(),
            ):
                result = runner.invoke(cli, ["lock"])

            assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestLockErrorHandling:
    def test_auth_error_exits_one(self, runner, tmp_path):
        from apm_cli.install.errors import AuthenticationError

        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())
            err = AuthenticationError("bad token")
            err.diagnostic_context = None

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=err,
            ):
                result = runner.invoke(cli, ["lock"])

        assert result.exit_code == 1

    def test_policy_violation_exits_one(self, runner, tmp_path):
        from apm_cli.install.errors import PolicyViolationError

        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=PolicyViolationError("blocked"),
            ):
                result = runner.invoke(cli, ["lock"])

        assert result.exit_code == 1

    def test_generic_error_exits_one(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            _make_apm_yml(Path.cwd())

            with patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=RuntimeError("network failure"),
            ):
                result = runner.invoke(cli, ["lock"])

        assert result.exit_code == 1
        assert "Error generating lockfile" in result.output


# ---------------------------------------------------------------------------
# Global scope
# ---------------------------------------------------------------------------


class TestLockGlobalScope:
    def test_global_no_apm_yml_exits_one(self, runner, tmp_path):
        """--global with no ~/.apm/apm.yml should exit 1."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("apm_cli.core.scope.get_apm_dir") as mock_dir:
                fake_dir = tmp_path / "dot-apm"
                fake_dir.mkdir()
                mock_dir.return_value = fake_dir
                result = runner.invoke(cli, ["lock", "--global"])

        assert result.exit_code == 1
        assert "apm.yml" in result.output
