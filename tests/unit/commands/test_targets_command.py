"""Tests for ``apm targets`` CLI command (commands/targets.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from apm_cli.commands.targets import targets
from apm_cli.core.target_detection import ResolvedTargets, Signal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolved(target_list: list[str]) -> ResolvedTargets:
    return ResolvedTargets(targets=target_list, source="auto-detect", auto_create=True)


def _signal(target: str, source: str) -> Signal:
    return Signal(target=target, source=source)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Table output: normal active targets
# ---------------------------------------------------------------------------


class TestTargetsTableOutput:
    def test_table_headers_present(self, runner: CliRunner, tmp_path: Path) -> None:
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch(
                "apm_cli.core.target_detection.detect_signals",
                return_value=[_signal("claude", "CLAUDE.md")],
            ),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        assert "TARGET" in result.output
        assert "STATUS" in result.output
        assert "DEPLOY DIR" in result.output

    def test_active_target_shown_as_active(self, runner: CliRunner, tmp_path: Path) -> None:
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch(
                "apm_cli.core.target_detection.detect_signals",
                return_value=[_signal("claude", "CLAUDE.md")],
            ),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        assert "active" in result.output
        assert "claude" in result.output

    def test_inactive_target_shows_needs(self, runner: CliRunner, tmp_path: Path) -> None:
        """Inactive targets show 'needs <signal>' in the source column."""
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        assert "inactive" in result.output
        # At least one "needs" annotation for a target with a canonical signal
        assert "needs" in result.output

    def test_active_with_no_signal_source(self, runner: CliRunner, tmp_path: Path) -> None:
        """Active target with no matching signal still renders without error."""
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["copilot"])
            ),
            # No signals returned → active_source will be None
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        assert "copilot" in result.output
        assert "active" in result.output

    def test_inactive_target_with_no_canonical_signal(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Inactive target without a CANONICAL_SIGNAL entry shows blank source col."""
        # Patch CANONICAL_SIGNAL so one target has no entry
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch(
                "apm_cli.core.target_detection.CANONICAL_SIGNAL",
                # Remove copilot's entry so it has no signal hint
                {"claude": "CLAUDE.md", "cursor": ".cursor/"},
            ),
            patch(
                "apm_cli.core.target_detection.CANONICAL_TARGETS_ORDERED",
                ["copilot"],
            ),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output

    def test_empty_active_shows_info_hint(self, runner: CliRunner, tmp_path: Path) -> None:
        """When no targets are active, a hint about harness config is printed."""
        with (
            patch("apm_cli.core.target_detection.resolve_targets", return_value=_resolved([])),
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("apm_cli.utils.console._rich_info") as mock_info,
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        mock_info.assert_called_once()
        call_text = mock_info.call_args[0][0]
        assert "harness" in call_text.lower() or "targets" in call_text.lower()


# ---------------------------------------------------------------------------
# Error paths: AmbiguousHarnessError, NoHarnessError, UsageError
# ---------------------------------------------------------------------------


class TestTargetsErrorPaths:
    def test_ambiguous_harness_falls_back_to_detect_signals(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from apm_cli.core.errors import AmbiguousHarnessError

        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets",
                side_effect=AmbiguousHarnessError("ambiguous"),
            ),
            patch(
                "apm_cli.core.target_detection.detect_signals",
                return_value=[
                    _signal("claude", "CLAUDE.md"),
                    _signal("copilot", ".github/"),
                ],
            ),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        # Both detected targets should be included in the active set
        assert "claude" in result.output
        assert "copilot" in result.output

    def test_no_harness_error_yields_empty_active(self, runner: CliRunner, tmp_path: Path) -> None:
        from apm_cli.core.errors import NoHarnessError

        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets",
                side_effect=NoHarnessError("no harness"),
            ),
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("apm_cli.utils.console._rich_info"),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        assert "inactive" in result.output

    def test_usage_error_yields_empty_active(self, runner: CliRunner, tmp_path: Path) -> None:
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets",
                side_effect=click.UsageError("bad"),
            ),
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("apm_cli.utils.console._rich_info"),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestTargetsJsonOutput:
    def test_json_flag_emits_valid_json(self, runner: CliRunner, tmp_path: Path) -> None:
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch(
                "apm_cli.core.target_detection.detect_signals",
                return_value=[_signal("claude", "CLAUDE.md")],
            ),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, ["--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert any(row["target"] == "claude" for row in data)

    def test_json_active_target_has_correct_status(self, runner: CliRunner, tmp_path: Path) -> None:
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch(
                "apm_cli.core.target_detection.detect_signals",
                return_value=[_signal("claude", "CLAUDE.md")],
            ),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, ["--json"])
        data = json.loads(result.output)
        claude_row = next(r for r in data if r["target"] == "claude")
        assert claude_row["status"] == "active"
        assert claude_row["source"] == "CLAUDE.md"

    def test_json_all_includes_agent_skills_meta_target(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch(
                "apm_cli.core.target_detection.detect_signals",
                return_value=[_signal("claude", "CLAUDE.md")],
            ),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, ["--json", "--all"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        meta = [r for r in data if r.get("target") == "agent-skills"]
        assert len(meta) == 1
        assert meta[0]["meta_target"] is True

    def test_json_without_all_excludes_agent_skills(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch(
                "apm_cli.core.target_detection.detect_signals",
                return_value=[_signal("claude", "CLAUDE.md")],
            ),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, ["--json"])
        data = json.loads(result.output)
        assert not any(r.get("target") == "agent-skills" for r in data)

    def test_json_all_agent_skills_inactive_when_not_in_active(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """agent-skills meta-target shows 'inactive' when not in active list."""
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets", return_value=_resolved(["claude"])
            ),
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, ["--json", "--all"])
        data = json.loads(result.output)
        meta = next(r for r in data if r.get("target") == "agent-skills")
        assert meta["status"] == "inactive"

    def test_json_all_agent_skills_active_when_in_active(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """agent-skills meta-target shows 'active' when in active list."""
        with (
            patch(
                "apm_cli.core.target_detection.resolve_targets",
                return_value=_resolved(["claude", "agent-skills"]),
            ),
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, ["--json", "--all"])
        data = json.loads(result.output)
        meta = next(r for r in data if r.get("target") == "agent-skills")
        assert meta["status"] == "active"

    def test_json_inactive_target_has_null_source(self, runner: CliRunner, tmp_path: Path) -> None:
        with (
            patch("apm_cli.core.target_detection.resolve_targets", return_value=_resolved([])),
            patch("apm_cli.core.target_detection.detect_signals", return_value=[]),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(targets, ["--json"])
        data = json.loads(result.output)
        # All rows should be inactive with null source
        for row in data:
            assert row["status"] == "inactive"
            assert row["source"] is None


# ---------------------------------------------------------------------------
# Subcommand delegation (line 49)
# ---------------------------------------------------------------------------


class TestTargetsSubcommandDelegation:
    def test_subcommand_delegates_without_running_body(self, runner: CliRunner) -> None:
        """Registering and invoking a sub-command triggers the early return."""

        # Temporarily add a lightweight sub-command to the group
        @targets.command("_test_sub")
        def _test_sub() -> None:
            click.echo("sub-ran")

        try:
            result = runner.invoke(targets, ["_test_sub"])
            assert result.exit_code == 0, result.output
            assert "sub-ran" in result.output
        finally:
            # Clean up so other tests are not affected
            targets.commands.pop("_test_sub", None)
