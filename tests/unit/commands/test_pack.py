"""Unit tests for ``apm pack`` command helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from apm_cli.commands.pack import _render_marketplace_result, pack_cmd
from apm_cli.marketplace.builder import BuildReport, MarketplaceOutputReport


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.successes: list[str] = []
        self.dry_runs: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def success(self, message: str) -> None:
        self.successes.append(message)

    def dry_run_notice(self, message: str) -> None:
        self.dry_runs.append(message)


def test_pack_help_recommends_manifest_marketplace_output_config() -> None:
    result = CliRunner().invoke(pack_cmd, ["--help"])

    assert result.exit_code == 0
    # The new --marketplace-path flag is shown in help
    assert "--marketplace-path" in result.output
    # The deprecated --marketplace-output is hidden
    assert "--marketplace-output" not in result.output
    assert "--claude-output" not in result.output


def test_marketplace_fallback_renders_warnings_and_package_count() -> None:
    logger = _RecordingLogger()
    fallback_report = SimpleNamespace(
        outputs=(),
        resolved=(object(), object()),
        warnings=("duplicate package warning",),
    )

    _render_marketplace_result(
        logger,
        fallback_report,
        dry_run=False,
        extra_warnings=("duplicate package warning",),
        outputs=[Path("marketplace.json")],
    )

    assert logger.warnings == ["duplicate package warning"]
    assert logger.successes == ["Built marketplace.json (2 package(s)) -> marketplace.json"]


def test_marketplace_fallback_renders_report_warnings_without_extra_warnings() -> None:
    logger = _RecordingLogger()

    _render_marketplace_result(
        logger,
        BuildReport(
            outputs=(
                MarketplaceOutputReport(
                    profile="claude",
                    resolved=(),
                    errors=(),
                    warnings=("report warning",),
                    output_path=Path("unused"),
                ),
            )
        ),
        dry_run=False,
        outputs=[],
    )

    assert logger.warnings == ["report warning"]
