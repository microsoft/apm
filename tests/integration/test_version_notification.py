"""Integration tests for version update notification in CLI."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import click
from click.testing import CliRunner

# One HOME is shared by every test in a process or xdist worker (tests/conftest.py).
_WORKER_HOME = Path.home()


def _stamp_path(home: Path) -> Path:
    """The daily-check stamp under *home*, as version_checker.get_update_cache_path lays it out."""
    cache = ("AppData", "Local", "apm", "cache") if sys.platform == "win32" else (".cache", "apm")
    return home.joinpath(*cache, "last_version_check")


class TestVersionNotificationIntegration(unittest.TestCase):
    """Test version check notification in CLI commands."""

    def setUp(self):
        """Give each test its own HOME, so no daily-check stamp leaks in from the worker."""
        self.runner = CliRunner()
        self.home = Path(tempfile.mkdtemp(prefix="apm-notice-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        # HOME for the pytest Path.home override, USERPROFILE for native Windows resolution.
        home = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        home_env = patch.dict("os.environ", home, clear=False)
        home_env.start()
        self.addCleanup(home_env.stop)

    @patch("apm_cli.commands._helpers.check_for_updates")
    @patch.dict("os.environ", {"APM_E2E_TESTS": ""}, clear=False)
    def test_version_notification_on_init(self, mock_check):
        """Test that version notification appears on init command."""
        # Mock that an update is available
        mock_check.return_value = "0.7.0"

        from apm_cli.cli import cli

        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli, ["init", "test-project", "--yes"])

            # Check that update notification appears in output
            self.assertIn("A new version", result.output)
            self.assertIn("0.7.0", result.output)
            self.assertIn("apm self-update", result.output)

    @patch("apm_cli.commands._helpers.check_for_updates")
    @patch.dict("os.environ", {"APM_E2E_TESTS": ""}, clear=False)
    def test_notification_ignores_a_daily_stamp_another_test_left(self, mock_check):
        """A daily-check stamp an earlier test on this worker wrote must not hide the notice."""
        mock_check.return_value = "0.7.0"
        stamp = _stamp_path(_WORKER_HOME)
        times = (stamp.stat().st_atime_ns, stamp.stat().st_mtime_ns) if stamp.exists() else None
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        try:
            from apm_cli.cli import cli

            with self.runner.isolated_filesystem():
                result = self.runner.invoke(cli, ["init", "test-project", "--yes"])
        finally:
            # Leave the worker stamp exactly as found: absent, or with its own age.
            if times is None:
                stamp.unlink()
            else:
                os.utime(stamp, ns=times)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("A new version", result.output)
        self.assertIn("0.7.0", result.output)

    @patch("apm_cli.commands._helpers.check_for_updates")
    @patch.dict("os.environ", {"APM_E2E_TESTS": ""}, clear=False)
    def test_a_fresh_daily_stamp_skips_the_check_and_the_notice(self, mock_check):
        """Within a day of the last check the notice neither checks nor prints (620c6ea79)."""
        mock_check.return_value = "0.7.0"
        stamp = _stamp_path(self.home)
        stamp.parent.mkdir(parents=True)
        stamp.touch()

        from apm_cli.cli import cli

        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli, ["init", "test-project", "--yes"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("A new version", result.output)
        mock_check.assert_not_called()

    @patch("apm_cli.commands._helpers.check_for_updates")
    def test_no_notification_when_up_to_date(self, mock_check):
        """Test that no notification appears when version is up to date."""
        # Mock that no update is available
        mock_check.return_value = None

        from apm_cli.cli import cli

        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli, ["init", "test-project", "--yes"])

            # Check that update notification does NOT appear
            self.assertNotIn("A new version", result.output)
            self.assertNotIn("apm self-update", result.output)

    @patch("apm_cli.commands._helpers.check_for_updates")
    def test_notification_does_not_block_command(self, mock_check):
        """Test that version check errors don't block command execution."""
        # Mock that check raises an exception
        mock_check.side_effect = Exception("Network error")

        from apm_cli.cli import cli

        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli, ["init", "test-project", "--yes"])

            # Command should still succeed despite check failure
            self.assertEqual(result.exit_code, 0)
            self.assertIn("initialized successfully", result.output)


class TestUpdateCheckSkippedOnInvalidCommand(unittest.TestCase):
    """Test that update check is skipped for unknown/invalid commands."""

    def setUp(self):
        self.runner = CliRunner()

    @patch("apm_cli.cli._check_and_notify_updates")
    def test_no_update_check_on_unknown_command(self, mock_check):
        """Update check must not run when an unknown command is invoked."""
        from apm_cli.cli import cli

        result = self.runner.invoke(cli, ["invalid"])

        mock_check.assert_not_called()
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("invalid", result.output)

    @patch("apm_cli.cli._check_and_notify_updates")
    def test_callback_skips_update_check_on_unresolved_command(self, mock_check):
        """Update check must not run before Click reports an unknown command."""
        from apm_cli.cli import cli

        ctx = click.Context(cli, info_name="apm")
        ctx.invoked_subcommand = "invalid"

        with ctx:
            cli.callback(verbose=False)

        mock_check.assert_not_called()

    @patch("apm_cli.cli._check_and_notify_updates")
    def test_update_check_runs_on_valid_command(self, mock_check):
        """Update check must still run when a known command is invoked."""
        from apm_cli.cli import cli

        with self.runner.isolated_filesystem():
            self.runner.invoke(cli, ["init", "test-project", "--yes"])

        mock_check.assert_called_once()

    @patch("apm_cli.cli._check_and_notify_updates")
    def test_callback_skips_update_check_without_subcommand(self, mock_check):
        """Update check must not run when no subcommand is given."""
        from apm_cli.cli import cli

        ctx = click.Context(cli, info_name="apm")
        ctx.invoked_subcommand = None

        with ctx:
            cli.callback(verbose=False)

        mock_check.assert_not_called()


class TestUpdateCommand(unittest.TestCase):
    """Test the update command."""

    def setUp(self):
        """Set up test fixtures."""
        self.runner = CliRunner()

    @patch("apm_cli.utils.version_checker.get_latest_version_from_github")
    @patch("apm_cli.utils.version_checker.is_newer_version")
    def test_update_check_flag(self, mock_is_newer, mock_get_latest):
        """Test update --check flag shows available update."""
        mock_get_latest.return_value = "0.7.0"
        mock_is_newer.return_value = True

        from apm_cli.cli import cli

        result = self.runner.invoke(cli, ["update", "--check"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Current version", result.output)
        self.assertIn("Latest version available", result.output)
        self.assertIn("0.7.0", result.output)

    @patch("apm_cli.utils.version_checker.get_latest_version_from_github")
    @patch("apm_cli.utils.version_checker.is_newer_version")
    def test_update_check_already_latest(self, mock_is_newer, mock_get_latest):
        """Test update --check when already on latest version."""
        mock_get_latest.return_value = "0.6.3"
        mock_is_newer.return_value = False

        from apm_cli.cli import cli

        result = self.runner.invoke(cli, ["update", "--check"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("already on the latest version", result.output)

    @patch("apm_cli.utils.version_checker.get_latest_version_from_github")
    def test_update_check_network_error(self, mock_get_latest):
        """Test update --check handles network errors gracefully."""
        mock_get_latest.return_value = None

        from apm_cli.cli import cli

        result = self.runner.invoke(cli, ["update", "--check"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Unable to fetch latest version", result.output)


if __name__ == "__main__":
    unittest.main()
