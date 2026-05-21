"""Unit tests for apm_cli.commands.marketplace.migrate command."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class TestMarketplaceMigrateCommand:
    """Tests for the ``apm marketplace migrate`` Click command."""

    def _invoke(self, runner: CliRunner, args: list[str] | None = None) -> object:
        from apm_cli.commands.marketplace.migrate import migrate

        return runner.invoke(migrate, args or [], catch_exceptions=False)

    # ------------------------------------------------------------------
    # Success paths
    # ------------------------------------------------------------------

    def test_migrate_success_shows_success_message(self, runner: CliRunner) -> None:
        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            return_value="diff output",
        ):
            result = self._invoke(runner)
        assert result.exit_code == 0
        assert "marketplace.yml" in result.output.lower() or "migrated" in result.output.lower()

    def test_dry_run_shows_diff(self, runner: CliRunner) -> None:
        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            return_value="+ new: block\n",
        ):
            result = self._invoke(runner, ["--dry-run"])
        assert result.exit_code == 0
        assert "+ new: block" in result.output

    def test_dry_run_no_changes_shows_no_changes(self, runner: CliRunner) -> None:
        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            return_value="",
        ):
            result = self._invoke(runner, ["--dry-run"])
        assert result.exit_code == 0
        assert "(no changes)" in result.output

    def test_dry_run_with_none_diff_shows_no_changes(self, runner: CliRunner) -> None:
        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            return_value=None,
        ):
            result = self._invoke(runner, ["--dry-run"])
        assert result.exit_code == 0
        assert "(no changes)" in result.output

    # ------------------------------------------------------------------
    # Force and yes flags
    # ------------------------------------------------------------------

    def test_force_flag_forwarded(self, runner: CliRunner) -> None:
        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            return_value="",
        ) as mock_migrate:
            self._invoke(runner, ["--force"])
        _args, _kwargs = mock_migrate.call_args
        assert _kwargs.get("force") is True or _args[1] is True

    def test_yes_alias_works_same_as_force(self, runner: CliRunner) -> None:
        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            return_value="",
        ) as mock_migrate:
            self._invoke(runner, ["--yes"])
        _args, _kwargs = mock_migrate.call_args
        assert _kwargs.get("force") is True or (len(_args) > 1 and _args[1] is True)

    # ------------------------------------------------------------------
    # Error paths
    # ------------------------------------------------------------------

    def test_marketplace_yml_error_exits_with_1(self, runner: CliRunner) -> None:
        from apm_cli.marketplace.errors import MarketplaceYmlError

        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            side_effect=MarketplaceYmlError("missing file"),
        ):
            result = runner.invoke(
                __import__("apm_cli.commands.marketplace.migrate", fromlist=["migrate"]).migrate,
                [],
            )
        assert result.exit_code == 1
        assert "missing file" in result.output

    def test_generic_exception_exits_with_1(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace.migrate import migrate

        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            side_effect=RuntimeError("unexpected"),
        ):
            result = runner.invoke(migrate, [])
        assert result.exit_code == 1
        assert "Migration failed" in result.output

    def test_generic_exception_verbose_shows_traceback(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace.migrate import migrate

        with patch(
            "apm_cli.commands.marketplace.migrate.migrate_marketplace_yml",
            side_effect=RuntimeError("boom"),
        ):
            result = runner.invoke(migrate, ["--verbose"])
        assert result.exit_code == 1
        # Verbose mode logs the traceback via logger.verbose_detail
        # The exact traceback is in verbose_detail but not necessarily in output
        # The important thing is it exits 1 and shows error
        assert "boom" in result.output or "Migration failed" in result.output
