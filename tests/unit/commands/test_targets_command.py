"""Tests for ``apm targets`` CLI command (commands/targets.py).

The command and target detection run for real against a project tree in
``tmp_path``: harness signals (``CLAUDE.md``, ``.github/copilot-instructions.md``)
and ``apm.yml`` declarations decide what is active, as they do for install.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from apm_cli.commands.targets import targets

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIGNAL_FILES = {
    "claude": "CLAUDE.md",
    "copilot": ".github/copilot-instructions.md",
}


def _project(root: Path, *harnesses: str, manifest_targets: list[str] | None = None) -> Path:
    """A project with the named harness signals and, optionally, apm.yml targets."""
    for harness in harnesses:
        signal = root / _SIGNAL_FILES[harness]
        signal.parent.mkdir(parents=True, exist_ok=True)
        signal.write_text("# harness\n", encoding="utf-8")
    if manifest_targets is not None:
        (root / "apm.yml").write_text(
            f"name: demo\nversion: 1.0.0\ntargets: [{', '.join(manifest_targets)}]\n",
            encoding="utf-8",
        )
    return root


def _rows(output: str) -> dict[str, dict]:
    return {row["target"]: row for row in json.loads(output)}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Table output: normal active targets
# ---------------------------------------------------------------------------


class TestTargetsTableOutput:
    def test_table_headers_present(self, runner: CliRunner, project: Path) -> None:
        _project(project, "claude")
        result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        assert "TARGET" in result.output
        assert "STATUS" in result.output
        assert "DEPLOY DIR" in result.output

    def test_active_target_shown_as_active(self, runner: CliRunner, project: Path) -> None:
        _project(project, "claude")
        result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        claude_row = next(line for line in result.output.splitlines() if "claude" in line)
        assert "active" in claude_row.split()
        assert "CLAUDE.md" in claude_row

    def test_grok_build_directory_is_displayed_as_active(
        self, runner: CliRunner, project: Path
    ) -> None:
        (project / ".grok").mkdir()
        result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        grok_row = next(line for line in result.output.splitlines() if "grok-build" in line)
        assert "active" in grok_row
        assert ".grok/" in grok_row

    def test_inactive_target_shows_needs(self, runner: CliRunner, project: Path) -> None:
        """Inactive targets show 'needs <signal>' in the source column."""
        _project(project, "claude")
        result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        cursor_row = next(line for line in result.output.splitlines() if "cursor" in line)
        assert "inactive" in cursor_row
        assert "needs .cursor/" in cursor_row

    def test_manifest_target_without_signal_reports_the_manifest(
        self, runner: CliRunner, project: Path
    ) -> None:
        """A declared target with no harness signal is active, sourced from apm.yml."""
        _project(project, manifest_targets=["copilot"])
        result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        copilot_row = next(line for line in result.output.splitlines() if "copilot" in line)
        assert "active" in copilot_row.split()
        assert "apm.yml" in copilot_row

    def test_inactive_target_with_no_canonical_signal(
        self, runner: CliRunner, project: Path
    ) -> None:
        """Inactive target without a CANONICAL_SIGNAL entry shows blank source col."""
        # Every shipped target has a signal; narrow the table to one that lacks it.
        with (
            patch(
                "apm_cli.core.target_detection.CANONICAL_SIGNAL",
                {"claude": "CLAUDE.md", "cursor": ".cursor/"},
            ),
            patch("apm_cli.core.target_detection.CANONICAL_TARGETS_ORDERED", ["copilot"]),
        ):
            result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        copilot_row = next(line for line in result.output.splitlines() if "copilot" in line)
        assert "inactive" in copilot_row
        assert "needs" not in copilot_row

    def test_empty_active_shows_info_hint(self, runner: CliRunner, project: Path) -> None:
        """When no targets are active, a hint about harness config is printed."""
        result = runner.invoke(targets, [])
        assert result.exit_code == 0, result.output
        assert "Create a harness config" in result.output
        assert "declare `targets:` in apm.yml" in result.output


# ---------------------------------------------------------------------------
# Error paths: AmbiguousHarnessError, NoHarnessError, UsageError
# ---------------------------------------------------------------------------


class TestTargetsErrorPaths:
    def test_ambiguous_harness_falls_back_to_detect_signals(
        self, runner: CliRunner, project: Path
    ) -> None:
        """Two harness signals are ambiguous for install; targets shows both active."""
        _project(project, "claude", "copilot")
        result = runner.invoke(targets, ["--json"])
        assert result.exit_code == 0, result.output
        rows = _rows(result.output)
        assert rows["claude"]["status"] == "active"
        assert rows["claude"]["source"] == "CLAUDE.md"
        assert rows["copilot"]["status"] == "active"
        assert rows["copilot"]["source"] == ".github/copilot-instructions.md"

    def test_no_harness_error_yields_empty_active(self, runner: CliRunner, project: Path) -> None:
        result = runner.invoke(targets, ["--json"])
        assert result.exit_code == 0, result.output
        assert {row["status"] for row in _rows(result.output).values()} == {"inactive"}

    def test_usage_error_yields_empty_active(self, runner: CliRunner, project: Path) -> None:
        # No project input reaches this branch today; it guards future resolver errors.
        with patch(
            "apm_cli.core.target_detection.resolve_targets", side_effect=click.UsageError("bad")
        ):
            result = runner.invoke(targets, ["--json"])
        assert result.exit_code == 0, result.output
        assert {row["status"] for row in _rows(result.output).values()} == {"inactive"}


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestTargetsJsonOutput:
    def test_json_flag_emits_valid_json(self, runner: CliRunner, project: Path) -> None:
        _project(project, "claude")
        result = runner.invoke(targets, ["--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert any(row["target"] == "claude" for row in data)

    def test_json_active_target_has_correct_status(self, runner: CliRunner, project: Path) -> None:
        _project(project, "claude")
        result = runner.invoke(targets, ["--json"])
        claude_row = _rows(result.output)["claude"]
        assert claude_row["status"] == "active"
        assert claude_row["source"] == "CLAUDE.md"

    def test_json_all_includes_agent_skills_meta_target(
        self, runner: CliRunner, project: Path
    ) -> None:
        _project(project, "claude")
        result = runner.invoke(targets, ["--json", "--all"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        meta = [r for r in data if r.get("target") == "agent-skills"]
        assert len(meta) == 1
        assert meta[0]["meta_target"] is True

    def test_json_without_all_excludes_agent_skills(self, runner: CliRunner, project: Path) -> None:
        _project(project, "claude")
        result = runner.invoke(targets, ["--json"])
        data = json.loads(result.output)
        assert not any(r.get("target") == "agent-skills" for r in data)

    def test_json_all_agent_skills_inactive_when_not_in_active(
        self, runner: CliRunner, project: Path
    ) -> None:
        """agent-skills meta-target shows 'inactive' when not in active list."""
        _project(project, manifest_targets=["claude"])
        result = runner.invoke(targets, ["--json", "--all"])
        meta = _rows(result.output)["agent-skills"]
        assert meta["status"] == "inactive"
        assert meta["source"] is None

    def test_json_all_agent_skills_active_when_in_active(
        self, runner: CliRunner, project: Path
    ) -> None:
        """agent-skills meta-target shows 'active' when in active list."""
        _project(project, manifest_targets=["claude", "agent-skills"])
        result = runner.invoke(targets, ["--json", "--all"])
        meta = _rows(result.output)["agent-skills"]
        assert meta["status"] == "active"
        assert meta["source"] == "apm.yml"

    def test_json_all_includes_grok_bot(self, runner: CliRunner, project: Path) -> None:
        """grok-bot is a stable explicit-only target, surfaced only via --all."""
        _project(project, "claude")
        result = runner.invoke(targets, ["--json", "--all"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        grok_bot = [r for r in data if r.get("target") == "grok-bot"]
        assert len(grok_bot) == 1
        assert grok_bot[0]["deploy_dir"] == "agent-data/"
        assert grok_bot[0]["meta_target"] is False

    def test_json_all_lists_catalog_explicit_only_targets(
        self, runner: CliRunner, project: Path
    ) -> None:
        """Stable explicit-only targets appear under --all; experimental ones do not."""
        from apm_cli.core.target_catalog import TARGET_CAPABILITIES

        _project(project, "claude")
        result = runner.invoke(targets, ["--json", "--all"])
        assert result.exit_code == 0, result.output
        rows = _rows(result.output)

        expected = {
            "agent-skills": (".agents/", True),
            "antigravity": (".agents/", False),
            "grok-bot": ("agent-data/", False),
            "hermes": (".agents/", False),
        }
        for name, (deploy_dir, meta) in expected.items():
            assert rows[name]["deploy_dir"] == deploy_dir
            assert rows[name]["meta_target"] is meta
        for name, cap in TARGET_CAPABILITIES.items():
            if cap.experimental_flag:
                assert name not in rows

    def test_json_without_all_excludes_grok_bot(self, runner: CliRunner, project: Path) -> None:
        _project(project, "claude")
        result = runner.invoke(targets, ["--json"])
        data = json.loads(result.output)
        assert not any(r.get("target") == "grok-bot" for r in data)

    def test_json_all_grok_bot_active_when_in_active(
        self, runner: CliRunner, project: Path
    ) -> None:
        """grok-bot shows 'active' when apm.yml declares it."""
        _project(project, manifest_targets=["grok-bot"])
        result = runner.invoke(targets, ["--json", "--all"])
        grok_bot = _rows(result.output)["grok-bot"]
        assert grok_bot["status"] == "active"
        assert grok_bot["source"] == "apm.yml"

    def test_json_inactive_target_has_null_source(self, runner: CliRunner, project: Path) -> None:
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


class TestTargetsHonourManifest:
    def test_manifest_targets_are_active_without_signals(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apm.yml ``targets:`` wins over detection, as it does for install (#3792)."""
        (tmp_path / "apm.yml").write_text(
            "name: demo\nversion: 1.0.0\ntargets: [copilot, claude]\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(targets, ["--json"])

        assert result.exit_code == 0, result.output
        rows = {row["target"]: row["status"] for row in json.loads(result.stdout)}
        assert rows["copilot"] == "active"
        assert rows["claude"] == "active"

    def test_singular_target_all_is_active_like_install(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Singular ``target: all`` names the install set, not auto-detect."""
        from apm_cli.core.target_catalog import expand_all

        (tmp_path / "apm.yml").write_text(
            "name: demo\nversion: 1.0.0\ntarget: all\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(targets, ["--json"])

        assert result.exit_code == 0, result.output
        rows = {row["target"]: row["status"] for row in json.loads(result.stdout)}
        assert {name for name, status in rows.items() if status == "active"} == set(
            expand_all("install")
        )
