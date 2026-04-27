"""Integration tests for ``apm marketplace publish``.

Strategy
--------
These tests use CliRunner with both ``MarketplacePublisher`` and
``PrIntegrator`` mocked out.  This verifies the CLI orchestration
layer (pre-flight checks, plan rendering, confirmation guard, summary)
without touching the network or any real git repositories.

All tests in this file use CliRunner for consistency.

Scenarios covered:
- Happy path: publisher.plan -> publisher.execute -> PrIntegrator.open_or_update -> exit 0.
- Non-interactive without --yes exits 1.
- --dry-run is forwarded to both services (dry_run=True).
- Mixed results (one FAILED) exits 1.
- Missing marketplace.yml exits 1.
- Missing marketplace.json exits 1.
- Missing consumer-targets.yml exits 1.
- Targets file with invalid format exits 1.
- --no-pr skips PR creation (PrIntegrator not called).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import publish
from apm_cli.marketplace.pr_integration import PrResult, PrState
from apm_cli.marketplace.publisher import (
    ConsumerTarget,
    PublishOutcome,
    PublishPlan,
    TargetResult,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PUBLISH_YML = """\
name: acme-marketplace
description: Acme marketplace
version: 2.0.0
owner:
  name: Acme Corp
packages:
  - name: tool-a
    source: org/tool-a
    version: "^1.0.0"
    tags:
      - test
"""

_GOLDEN_JSON = """\
{
  "name": "acme-marketplace",
  "description": "Acme marketplace",
  "version": "2.0.0",
  "owner": {"name": "Acme Corp"},
  "plugins": [
    {
      "name": "tool-a",
      "tags": ["test"],
      "source": {
        "type": "github",
        "repository": "org/tool-a",
        "ref": "v1.2.0",
        "commit": "aaaa000000000000000000000000000000000001"
      }
    }
  ]
}
"""

_TARGETS_YML = """\
targets:
  - repo: consumer-org/service-a
    branch: main
  - repo: consumer-org/service-b
    branch: develop
"""

_TARGETS_SINGLE_YML = """\
targets:
  - repo: consumer-org/service-a
    branch: main
"""


def _make_plan(targets):
    return PublishPlan(
        marketplace_name="acme-marketplace",
        marketplace_version="2.0.0",
        targets=tuple(targets),
        commit_message="chore(apm): bump acme-marketplace to 2.0.0",
        branch_name="apm/marketplace-update-acme-marketplace-2.0.0-abc12345",
        new_ref="v2.0.0",
        tag_pattern_used="v{version}",
        short_hash="abc12345",
    )


def _make_target_result(repo, outcome=PublishOutcome.UPDATED):
    target = ConsumerTarget(repo=repo, branch="main")
    return TargetResult(
        target=target,
        outcome=outcome,
        message=f"{repo}: {outcome.value}",
        old_version="1.0.0",
        new_version="2.0.0",
    )


def _make_pr_result(repo, state=PrState.OPENED):
    target = ConsumerTarget(repo=repo, branch="main")
    return PrResult(
        target=target,
        state=state,
        pr_number=42,
        pr_url=f"https://github.com/{repo}/pull/42",
        message=f"PR {state.value}",
    )


def _setup_workspace(tmp_path: Path, with_targets=True, with_json=True):
    """Write marketplace.yml, optionally marketplace.json and consumer-targets.yml."""
    (tmp_path / "marketplace.yml").write_text(_PUBLISH_YML, encoding="utf-8")
    if with_json:
        (tmp_path / "marketplace.json").write_text(_GOLDEN_JSON, encoding="utf-8")
    if with_targets:
        (tmp_path / "consumer-targets.yml").write_text(_TARGETS_SINGLE_YML, encoding="utf-8")


def _run_publish(tmp_path: Path, extra_args=(), mock_plan=None, mock_results=None,
                 mock_pr_available=True, mock_pr_results=None,
                 env_overrides=None):
    """Run publish via CliRunner with publisher and PrIntegrator mocked."""
    runner = CliRunner()

    targets = [ConsumerTarget(repo="consumer-org/service-a", branch="main")]
    plan = mock_plan or _make_plan(targets)

    results = mock_results or [
        _make_target_result("consumer-org/service-a", PublishOutcome.UPDATED),
    ]
    pr_results = mock_pr_results or [
        _make_pr_result("consumer-org/service-a", PrState.OPENED),
    ]

    env = {}
    if env_overrides:
        env.update(env_overrides)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        import shutil
        for fname in ("marketplace.yml", "marketplace.json", "consumer-targets.yml"):
            src = tmp_path / fname
            if src.exists():
                shutil.copy(str(src), f"{cwd}/{fname}")

        with (
            patch(
                "apm_cli.commands.marketplace.MarketplacePublisher.plan",
                return_value=plan,
            ),
            patch(
                "apm_cli.commands.marketplace.MarketplacePublisher.execute",
                return_value=results,
            ),
            patch(
                "apm_cli.commands.marketplace.PrIntegrator.check_available",
                return_value=(mock_pr_available, "gh available"),
            ),
            patch(
                "apm_cli.commands.marketplace.PrIntegrator.open_or_update",
                side_effect=pr_results,
            ),
            patch(
                "apm_cli.commands.marketplace._is_interactive",
                return_value=False,
            ),
            patch.dict(os.environ, env, clear=False),
        ):
            result = runner.invoke(publish, list(extra_args), catch_exceptions=False)

    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPublishHappyPath:
    """Happy path: all targets updated, PRs opened, exit 0."""

    def test_exit_code_zero_happy_path(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        result = _run_publish(tmp_path, extra_args=["--yes"])
        assert result.exit_code == 0

    def test_summary_appears_in_output(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        result = _run_publish(tmp_path, extra_args=["--yes"])
        combined = result.output
        # Summary table must mention the target
        assert "service-a" in combined or "consumer-org" in combined

    def test_no_traceback(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        result = _run_publish(tmp_path, extra_args=["--yes"])
        assert "Traceback" not in result.output


class TestPublishNonInteractive:
    """Without --yes in non-interactive mode, publish exits 1."""

    def test_exits_1_without_yes(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        result = _run_publish(tmp_path, extra_args=[])
        assert result.exit_code == 1

    def test_error_message_mentions_yes(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        result = _run_publish(tmp_path, extra_args=[])
        combined = result.output
        assert "--yes" in combined or "non-interactive" in combined.lower()


class TestPublishDryRun:
    """--dry-run must be forwarded to execute (dry_run=True)."""

    def test_dry_run_forwarded_to_execute(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        execute_mock = MagicMock(
            return_value=[
                _make_target_result("consumer-org/service-a", PublishOutcome.UPDATED),
            ]
        )
        runner = CliRunner()
        targets = [ConsumerTarget(repo="consumer-org/service-a", branch="main")]
        plan = _make_plan(targets)

        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            for fname in ("marketplace.yml", "marketplace.json", "consumer-targets.yml"):
                src = tmp_path / fname
                if src.exists():
                    shutil.copy(str(src), f"{cwd}/{fname}")

            with (
                patch(
                    "apm_cli.commands.marketplace.MarketplacePublisher.plan",
                    return_value=plan,
                ),
                patch(
                    "apm_cli.commands.marketplace.MarketplacePublisher.execute",
                    execute_mock,
                ),
                patch(
                    "apm_cli.commands.marketplace.PrIntegrator.check_available",
                    return_value=(True, "ok"),
                ),
                patch(
                    "apm_cli.commands.marketplace.PrIntegrator.open_or_update",
                    return_value=_make_pr_result("consumer-org/service-a", PrState.SKIPPED),
                ),
                patch(
                    "apm_cli.commands.marketplace._is_interactive",
                    return_value=False,
                ),
            ):
                result = runner.invoke(
                    publish, ["--yes", "--dry-run"], catch_exceptions=False
                )

        # execute must have been called with dry_run=True
        assert execute_mock.called
        call_kwargs = execute_mock.call_args
        # dry_run is a keyword arg
        assert call_kwargs.kwargs.get("dry_run") is True or (
            call_kwargs.args and call_kwargs.args[1] is True
        )

    def test_dry_run_message_in_output(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        result = _run_publish(tmp_path, extra_args=["--yes", "--dry-run"])
        assert "Dry run" in result.output or "dry" in result.output.lower()


class TestPublishMixedResults:
    """A FAILED result must cause exit 1."""

    def test_exit_code_one_on_failure(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        results = [
            _make_target_result("consumer-org/service-a", PublishOutcome.FAILED),
        ]
        result = _run_publish(tmp_path, extra_args=["--yes"], mock_results=results)
        assert result.exit_code == 1


class TestPublishPreflightErrors:
    """Pre-flight error handling exits 1 with clear messages."""

    def test_missing_yml_exits_1(self, tmp_path: Path):
        # Only write marketplace.json and targets; no marketplace.yml
        (tmp_path / "marketplace.json").write_text(_GOLDEN_JSON, encoding="utf-8")
        (tmp_path / "consumer-targets.yml").write_text(_TARGETS_SINGLE_YML, encoding="utf-8")
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.json"), f"{cwd}/marketplace.json")
            shutil.copy(str(tmp_path / "consumer-targets.yml"), f"{cwd}/consumer-targets.yml")
            result = runner.invoke(publish, ["--yes"], catch_exceptions=False)
        assert result.exit_code == 1

    def test_missing_json_exits_1(self, tmp_path: Path):
        # marketplace.yml present, but marketplace.json absent
        (tmp_path / "marketplace.yml").write_text(_PUBLISH_YML, encoding="utf-8")
        (tmp_path / "consumer-targets.yml").write_text(_TARGETS_SINGLE_YML, encoding="utf-8")
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), f"{cwd}/marketplace.yml")
            shutil.copy(str(tmp_path / "consumer-targets.yml"), f"{cwd}/consumer-targets.yml")
            result = runner.invoke(publish, ["--yes"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "marketplace.json" in result.output

    def test_missing_targets_exits_1(self, tmp_path: Path):
        # Both yml and json present but no consumer-targets.yml
        _setup_workspace(tmp_path, with_targets=False)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), f"{cwd}/marketplace.yml")
            shutil.copy(str(tmp_path / "marketplace.json"), f"{cwd}/marketplace.json")
            result = runner.invoke(publish, ["--yes"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "consumer-targets.yml" in result.output or "targets" in result.output.lower()

    def test_invalid_targets_format_exits_1(self, tmp_path: Path):
        """A targets file without a 'targets' key must exit 1."""
        _setup_workspace(tmp_path, with_targets=False)
        (tmp_path / "consumer-targets.yml").write_text(
            "not_a_targets_file: true\n", encoding="utf-8"
        )
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            for fname in ("marketplace.yml", "marketplace.json", "consumer-targets.yml"):
                src = tmp_path / fname
                if src.exists():
                    shutil.copy(str(src), f"{cwd}/{fname}")
            result = runner.invoke(publish, ["--yes"], catch_exceptions=False)
        assert result.exit_code == 1


class TestPublishNoPr:
    """--no-pr skips PR creation."""

    def test_no_pr_skips_pr_integrator(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        open_or_update_mock = MagicMock()
        runner = CliRunner()
        targets = [ConsumerTarget(repo="consumer-org/service-a", branch="main")]
        plan = _make_plan(targets)

        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            for fname in ("marketplace.yml", "marketplace.json", "consumer-targets.yml"):
                src = tmp_path / fname
                if src.exists():
                    shutil.copy(str(src), f"{cwd}/{fname}")

            with (
                patch(
                    "apm_cli.commands.marketplace.MarketplacePublisher.plan",
                    return_value=plan,
                ),
                patch(
                    "apm_cli.commands.marketplace.MarketplacePublisher.execute",
                    return_value=[
                        _make_target_result("consumer-org/service-a", PublishOutcome.UPDATED)
                    ],
                ),
                patch(
                    "apm_cli.commands.marketplace.PrIntegrator.open_or_update",
                    open_or_update_mock,
                ),
                patch(
                    "apm_cli.commands.marketplace._is_interactive",
                    return_value=False,
                ),
            ):
                result = runner.invoke(
                    publish, ["--yes", "--no-pr"], catch_exceptions=False
                )

        # PrIntegrator.open_or_update must NOT have been called
        open_or_update_mock.assert_not_called()
        assert result.exit_code == 0
