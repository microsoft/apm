"""Install not-found suggestions surfaced through package resolution."""

from __future__ import annotations

from unittest.mock import patch

from apm_cli.commands.install import _resolve_package_references
from apm_cli.marketplace.errors import PluginNotFoundError


class TestInstallMarketplaceNotFoundSuggestions:
    def test_near_miss_plugin_surfaces_suggestion_in_invalid_outcome(self):
        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("apm-review-panl", "apm-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=PluginNotFoundError(
                    "apm-review-panl",
                    "apm-marketplace",
                    suggestions=["apm-review-panel"],
                ),
            ),
        ):
            _valid, invalid, _validated, _mkt, _entries, _changed = _resolve_package_references(
                ["apm-review-panl@apm-marketplace"],
                [],
                set(),
            )

        assert len(invalid) == 1
        _package, reason = invalid[0]
        assert _package == "apm-review-panl@apm-marketplace"
        assert "Did you mean: apm-review-panel?" in reason
        assert "apm marketplace browse apm-marketplace" in reason

    def test_wild_plugin_name_has_no_suggestion(self):
        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("completely-unrelated-package", "apm-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=PluginNotFoundError(
                    "completely-unrelated-package",
                    "apm-marketplace",
                ),
            ),
        ):
            _valid, invalid, _validated, _mkt, _entries, _changed = _resolve_package_references(
                ["completely-unrelated-package@apm-marketplace"],
                [],
                set(),
            )

        _package, reason = invalid[0]
        assert "Did you mean" not in reason
        assert "Similar plugins" not in reason

    def test_marketplace_resolution_failure_falls_back_to_plain_message(self):
        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("missing-plugin", "apm-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=RuntimeError("catalog unavailable"),
            ),
        ):
            _valid, invalid, _validated, _mkt, _entries, _changed = _resolve_package_references(
                ["missing-plugin@apm-marketplace"],
                [],
                set(),
            )

        _package, reason = invalid[0]
        assert reason == "catalog unavailable"
        assert "Did you mean" not in reason


class TestInstallMarketplaceNotFoundExitCode:
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    def test_marketplace_not_found_preserves_validation_failure_contract(self, _mock_validate):
        """Marketplace not-found still lands in invalid_outcomes (exit path unchanged)."""
        import contextlib
        import os
        import tempfile
        from pathlib import Path

        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        try:
            original_dir = os.getcwd()
        except FileNotFoundError:
            original_dir = str(Path(__file__).parent.parent.parent)

        @contextlib.contextmanager
        def _chdir_tmp():
            with tempfile.TemporaryDirectory() as tmp_dir:
                try:
                    os.chdir(tmp_dir)
                    yield Path(tmp_dir)
                finally:
                    os.chdir(original_dir)

        with (
            _chdir_tmp(),
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("apm-review-panl", "apm-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=PluginNotFoundError(
                    "apm-review-panl",
                    "apm-marketplace",
                    suggestions=["apm-review-panel"],
                ),
            ),
        ):
            result = runner.invoke(cli, ["install", "apm-review-panl@apm-marketplace"])

        assert result.exit_code == 1
        assert "All packages failed validation" in result.output
        assert "Did you mean: apm-review-panel?" in result.output
