"""Tests for marketplace experimental flag gating.

Verifies:
  - ``marketplace_authoring`` flag is registered in the ``FLAGS`` registry
  - Marketplace group callback exits with a helpful message when flag disabled
  - Marketplace group callback proceeds normally when flag enabled

Note: The directory-level conftest patches ``is_enabled`` to return True
for ``marketplace_authoring`` (so existing marketplace subcommand tests pass).
Tests here that need the flag *disabled* wrap their assertions in an
explicit ``patch`` context manager that overrides the conftest mock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# Defence-in-depth: marketplace group callback guard
# ---------------------------------------------------------------------------


class TestMarketplaceGroupCallbackGuard:
    """Verify the guard inside the marketplace() group callback."""

    def test_exits_with_message_when_disabled(self) -> None:
        """When flag is disabled, marketplace group exits with enablement hint."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        # Override the conftest patch to force the flag off
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            # Invoke a subcommand so the group callback fires (--help is eager)
            result = runner.invoke(marketplace, ["list"])

        assert result.exit_code != 0
        assert "experimental" in result.output.lower()
        assert "apm experimental enable marketplace-authoring" in result.output

    def test_proceeds_when_enabled(self) -> None:
        """When flag is enabled, marketplace group does not block subcommands.

        The conftest already patches is_enabled to return True for
        marketplace_authoring, so no additional setup needed.
        """
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        result = runner.invoke(marketplace, ["--help"])

        assert result.exit_code == 0
        assert "marketplace" in result.output.lower()

    def test_guard_message_includes_learn_more(self) -> None:
        """Guard message includes 'apm experimental list' for discoverability."""
        from apm_cli.commands.marketplace import marketplace

        runner = CliRunner()
        with patch(
            "apm_cli.core.experimental.is_enabled",
            side_effect=lambda name: False,
        ):
            result = runner.invoke(marketplace, ["list"])

        assert "apm experimental list" in result.output


# ---------------------------------------------------------------------------
# CLI registration gate (cli.py conditional add_command)
# ---------------------------------------------------------------------------


class TestCliRegistrationGate:
    """Verify the conditional registration references the correct flag."""

    def test_cli_py_references_marketplace_authoring_flag(self) -> None:
        """cli.py source code checks the marketplace_authoring flag."""
        import inspect
        import apm_cli.cli as cli_mod

        source = inspect.getsource(cli_mod)
        assert "_xp_enabled(\"marketplace_authoring\")" in source

    def test_cli_py_wraps_registration_in_try_except(self) -> None:
        """cli.py wraps marketplace registration in try/except for resilience."""
        import inspect
        import apm_cli.cli as cli_mod

        source = inspect.getsource(cli_mod)
        # Find the marketplace gating block
        idx = source.index("marketplace_authoring")
        # Look backwards for 'try' and forwards for 'except'
        block = source[max(0, idx - 200):idx + 200]
        assert "try:" in block
        assert "except" in block
