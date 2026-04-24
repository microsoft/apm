"""Tests for marketplace experimental flag gating.

Verifies:
  - ``marketplace_authoring`` flag is registered in the ``FLAGS`` registry
  - Consumer commands (add, list, browse, update, remove, validate) work
    WITHOUT the flag enabled
  - Authoring commands (init, build, check, outdated, doctor, publish, package)
    are blocked when the flag is disabled, with an enablement message
  - Authoring commands proceed when the flag is enabled

Note: The directory-level conftest patches ``is_enabled`` to return True
for ``marketplace_authoring`` (so existing marketplace subcommand tests pass).
Tests here that need the flag *disabled* wrap their assertions in an
explicit ``patch`` context manager that overrides the conftest mock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from apm_cli.core.experimental import is_enabled as _real_is_enabled

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Flag registration (uses the real FLAGS dict -- unaffected by is_enabled mock)
# ---------------------------------------------------------------------------


class TestMarketplaceFlagRegistration:
    """Verify the marketplace_authoring flag exists with correct metadata."""

    def test_marketplace_flag_in_registry(self) -> None:
        """marketplace_authoring is a registered ExperimentalFlag."""
        from apm_cli.core.experimental import FLAGS

        assert "marketplace_authoring" in FLAGS

    def test_flag_default_is_false(self) -> None:
        """Flag ships disabled by default."""
        from apm_cli.core.experimental import FLAGS

        flag = FLAGS["marketplace_authoring"]
        assert flag.default is False

    def test_flag_name_matches_key(self) -> None:
        """Registry key matches the flag's .name attribute."""
        from apm_cli.core.experimental import FLAGS

        flag = FLAGS["marketplace_authoring"]
        assert flag.name == "marketplace_authoring"

    def test_flag_has_hint(self) -> None:
        """Flag provides a post-enable hint."""
        from apm_cli.core.experimental import FLAGS

        flag = FLAGS["marketplace_authoring"]
        assert flag.hint is not None
        assert "marketplace" in flag.hint.lower()

    def test_flag_description_mentions_authoring(self) -> None:
        """Flag description is scoped to authoring commands only."""
        from apm_cli.core.experimental import FLAGS

        flag = FLAGS["marketplace_authoring"]
        assert "authoring" in flag.description.lower()


# ---------------------------------------------------------------------------
# Consumer commands: always available (no flag required)
# ---------------------------------------------------------------------------


class TestConsumerCommandsUngated:
    """Consumer commands must work without marketplace_authoring enabled."""

    @pytest.mark.parametrize("subcmd", ["add", "list", "browse", "update", "remove", "validate"])
    def test_consumer_command_reachable_when_flag_disabled(self, subcmd: str) -> None:
        """Consumer subcommands are not blocked by the authoring flag."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            result = runner.invoke(marketplace, [subcmd, "--help"])

        # --help should succeed (exit 0) and NOT show the experimental
        # gating message -- the command is reachable.
        assert result.exit_code == 0
        assert "experimental" not in result.output.lower()

    def test_marketplace_help_works_when_flag_disabled(self) -> None:
        """``marketplace --help`` shows consumer section without the flag."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            result = runner.invoke(marketplace, ["--help"])

        assert result.exit_code == 0
        assert "Consumer commands" in result.output

    def test_marketplace_help_hides_authoring_when_flag_disabled(self) -> None:
        """``marketplace --help`` omits authoring section when flag is off."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            result = runner.invoke(marketplace, ["--help"])

        assert result.exit_code == 0
        assert "Authoring commands" not in result.output

    @pytest.mark.parametrize("subcmd", ["init", "build", "check", "outdated", "doctor", "publish", "package"])
    def test_authoring_commands_hidden_from_help_when_flag_disabled(self, subcmd: str) -> None:
        """Individual authoring command names are absent from --help when flag is off."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            result = runner.invoke(marketplace, ["--help"])

        assert result.exit_code == 0
        # Each authoring command name should not appear as a listed subcommand
        # (it may appear in the group description; check the commands section)
        lines = result.output.split("\n")
        command_lines = [
            line for line in lines
            if line.strip().startswith(subcmd)
        ]
        assert not command_lines, (
            f"Authoring command '{subcmd}' should be hidden from --help "
            f"when flag is disabled, but found: {command_lines}"
        )


# ---------------------------------------------------------------------------
# Authoring commands: blocked without the flag
# ---------------------------------------------------------------------------


class TestAuthoringCommandsGated:
    """Authoring commands must be blocked when the flag is disabled."""

    @pytest.mark.parametrize("subcmd", ["init", "build", "check", "outdated", "doctor", "publish"])
    def test_authoring_command_blocked_when_disabled(self, subcmd: str) -> None:
        """Authoring subcommand exits with enablement hint when flag off."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            result = runner.invoke(marketplace, [subcmd])

        assert result.exit_code != 0
        assert "experimental" in result.output.lower()
        assert "apm experimental enable marketplace-authoring" in result.output

    def test_package_subgroup_blocked_when_disabled(self) -> None:
        """``marketplace package`` exits with enablement hint when flag off."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            result = runner.invoke(marketplace, ["package", "add", "x/y"])

        assert result.exit_code != 0
        assert "experimental" in result.output.lower()
        assert "apm experimental enable marketplace-authoring" in result.output

    @pytest.mark.parametrize("subcmd", ["init", "build", "check", "outdated", "doctor", "publish"])
    def test_authoring_guard_message_includes_learn_more(self, subcmd: str) -> None:
        """Guard message includes 'apm experimental list' for discoverability."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            result = runner.invoke(marketplace, [subcmd])

        assert "apm experimental list" in result.output


# ---------------------------------------------------------------------------
# Authoring commands: accessible when the flag IS enabled
# ---------------------------------------------------------------------------


class TestAuthoringCommandsEnabled:
    """Authoring commands proceed normally when the flag is enabled."""

    @pytest.fixture(autouse=True)
    def _enable_flag(self):
        """Enable marketplace_authoring for this class's tests."""
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: True if name == "marketplace_authoring" else _real_is_enabled(name),
        ):
            yield

    @pytest.mark.parametrize("subcmd", ["init", "build", "check", "outdated", "doctor", "publish"])
    def test_authoring_command_help_reachable_when_enabled(self, subcmd: str) -> None:
        """Authoring subcommand --help works when flag is enabled."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        result = runner.invoke(marketplace, [subcmd, "--help"])

        assert result.exit_code == 0
        assert "experimental" not in result.output.lower()

    def test_package_subgroup_help_reachable_when_enabled(self) -> None:
        """``marketplace package --help`` works when flag is enabled."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        result = runner.invoke(marketplace, ["package", "--help"])

        assert result.exit_code == 0
        assert "experimental" not in result.output.lower()

    def test_marketplace_help_shows_both_sections_when_enabled(self) -> None:
        """``marketplace --help`` shows Consumer and Authoring sections when flag on."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        result = runner.invoke(marketplace, ["--help"])

        assert result.exit_code == 0
        assert "Consumer commands" in result.output
        assert "Authoring commands" in result.output

    @pytest.mark.parametrize("subcmd", ["init", "build", "check", "outdated", "doctor", "publish", "package"])
    def test_authoring_commands_listed_in_help_when_enabled(self, subcmd: str) -> None:
        """Authoring command names appear in --help when flag is on."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        result = runner.invoke(marketplace, ["--help"])

        assert result.exit_code == 0
        lines = result.output.split("\n")
        command_lines = [
            line for line in lines
            if line.strip().startswith(subcmd)
        ]
        assert command_lines, (
            f"Authoring command '{subcmd}' should be visible in --help "
            f"when flag is enabled"
        )
