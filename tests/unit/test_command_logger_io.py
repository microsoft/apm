"""Unit tests for CommandLogger, InstallLogger, and _ValidationOutcome (filesystem, process and CLI boundary cases)."""

import pytest

pytestmark = pytest.mark.component


class TestVerboseFlagAcceptance:
    """Verify CLI commands accept --verbose without crashing on unknown option."""

    def test_uninstall_accepts_verbose_flag(self):
        from click.testing import CliRunner

        from apm_cli.commands.uninstall.cli import uninstall

        runner = CliRunner()
        result = runner.invoke(uninstall, ["some-package", "--verbose"])
        # exit code 2 = click UsageError (unknown option) — must not happen
        assert result.exit_code != 2
