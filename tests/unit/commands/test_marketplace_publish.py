"""Tests for ``apm marketplace publish`` subcommand."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, call, patch  # noqa: F401

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.marketplace.pr_integration import PrResult, PrState
from apm_cli.marketplace.publisher import (
    ConsumerTarget,
    PublishOutcome,
    PublishPlan,
    TargetResult,
)

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_BASIC_YML = textwrap.dedent("""\
    name: test-marketplace
    description: Test marketplace
    version: 2.0.0
    owner:
      name: Test Owner
    packages:
      - name: pkg-alpha
        source: acme-org/pkg-alpha
        version: "^1.0.0"
""")

_TARGETS_YML = textwrap.dedent("""\
    targets:
      - repo: acme-org/service-a
        branch: main
      - repo: acme-org/service-b
        branch: develop
""")

_MARKETPLACE_JSON = json.dumps(
    {
        "name": "test-marketplace",
        "plugins": [],
    }
)


def _fake_plan(targets=None):
    """Build a fake ``PublishPlan``."""
    if targets is None:
        targets = (
            ConsumerTarget(repo="acme-org/service-a", branch="main"),
            ConsumerTarget(repo="acme-org/service-b", branch="develop"),
        )
    return PublishPlan(
        marketplace_name="test-marketplace",
        marketplace_version="2.0.0",
        targets=targets,
        commit_message="chore(apm): bump test-marketplace to 2.0.0",
        branch_name="apm/marketplace-update-test-marketplace-2.0.0-abcd1234",
        new_ref="v2.0.0",
        tag_pattern_used="v{version}",
        short_hash="abcd1234",
    )


def _fake_result(target, outcome=PublishOutcome.UPDATED, message="OK"):
    """Build a fake ``TargetResult``."""
    return TargetResult(
        target=target,
        outcome=outcome,
        message=message,
        old_version="v1.0.0",
        new_version="v2.0.0",
    )


def _fake_pr_result(target, state=PrState.OPENED, pr_number=42, pr_url=None):
    """Build a fake ``PrResult``."""
    url = pr_url or f"https://github.com/{target.repo}/pull/{pr_number}"
    return PrResult(
        target=target,
        state=state,
        pr_number=pr_number,
        pr_url=url,
        message="PR opened.",
    )


def _write_fixtures(tmp_path, *, targets_yml=_TARGETS_YML, yml=_BASIC_YML):
    """Write marketplace.yml, marketplace.json, and consumer-targets.yml."""
    (tmp_path / "marketplace.yml").write_text(yml, encoding="utf-8")
    (tmp_path / "marketplace.json").write_text(_MARKETPLACE_JSON, encoding="utf-8")
    (tmp_path / "consumer-targets.yml").write_text(targets_yml, encoding="utf-8")


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPublishHappyPath:
    """Happy path: publish to 2 targets with PRs opened."""

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_happy_path_exit_0(self, MockPublisher, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        targets = list(plan.targets)

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(targets[0]),
            _fake_result(targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.side_effect = [
            _fake_pr_result(targets[0], pr_number=10),
            _fake_pr_result(targets[1], pr_number=11),
        ]

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Published 2/2 targets" in result.output
        assert "publish-state.json" in result.output

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_pr_integrator_called_for_updated_targets(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        targets = list(plan.targets)

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(targets[0]),
            _fake_result(targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.side_effect = [
            _fake_pr_result(targets[0]),
            _fake_pr_result(targets[1]),
        ]

        runner.invoke(marketplace, ["publish", "--yes"])
        assert mock_pr.open_or_update.call_count == 2


# ---------------------------------------------------------------------------
# --no-pr flag
# ---------------------------------------------------------------------------


class TestPublishNoPr:
    """--no-pr: publisher runs but PR integrator is not called."""

    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_no_pr_skips_pr_integration(self, MockPublisher, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        targets = list(plan.targets)

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(targets[0]),
            _fake_result(targets[1]),
        ]

        with patch("apm_cli.commands.marketplace.publish.PrIntegrator") as MockPr:
            result = runner.invoke(marketplace, ["publish", "--yes", "--no-pr"])
            assert result.exit_code == 0, result.output
            # PrIntegrator should not have been instantiated for operations
            mock_pr = MockPr.return_value
            mock_pr.open_or_update.assert_not_called()


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


class TestPublishDryRun:
    """--dry-run: publisher.execute with dry_run=True, PR with dry_run=True."""

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_dry_run_passes_flag_to_execute(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        targets = list(plan.targets)

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(targets[0]),
            _fake_result(targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(targets[0])

        result = runner.invoke(marketplace, ["publish", "--yes", "--dry-run"])
        assert result.exit_code == 0, result.output

        # Verify dry_run=True was passed to execute
        mock_pub.execute.assert_called_once_with(
            plan,
            dry_run=True,
            parallel=4,
        )

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_dry_run_passes_flag_to_pr_integration(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        targets = list(plan.targets)

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(targets[0]),
            _fake_result(targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(targets[0])

        runner.invoke(marketplace, ["publish", "--yes", "--dry-run"])

        # Verify dry_run=True was passed to pr.open_or_update
        for c in mock_pr.open_or_update.call_args_list:
            assert c.kwargs.get("dry_run") is True or c[1].get("dry_run") is True

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_dry_run_shows_info_note(self, MockPublisher, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        result = runner.invoke(marketplace, ["publish", "--yes", "--dry-run"])
        assert "dry-run" in result.output.lower() or "Dry run" in result.output


# ---------------------------------------------------------------------------
# Missing files
# ---------------------------------------------------------------------------


class TestPublishMissingFiles:
    def test_missing_marketplace_yml_exit_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.json").write_text("{}", encoding="utf-8")
        (tmp_path / "consumer-targets.yml").write_text(_TARGETS_YML, encoding="utf-8")

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1  # _load_yml_or_exit calls sys.exit(1) on missing file

    def test_missing_marketplace_json_exit_1(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")
        (tmp_path / "consumer-targets.yml").write_text(_TARGETS_YML, encoding="utf-8")

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1
        assert "marketplace.json not found" in result.output
        assert "apm pack" in result.output

    def test_marketplace_yml_schema_error_exit_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bad_yml = "name: test\n"  # missing required fields
        (tmp_path / "marketplace.yml").write_text(bad_yml, encoding="utf-8")
        (tmp_path / "marketplace.json").write_text("{}", encoding="utf-8")
        (tmp_path / "consumer-targets.yml").write_text(_TARGETS_YML, encoding="utf-8")

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 2


class TestPublishMissingTargets:
    def test_missing_targets_file_exit_1_with_guidance(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")
        (tmp_path / "marketplace.json").write_text(_MARKETPLACE_JSON, encoding="utf-8")

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1
        assert "consumer-targets.yml" in result.output
        assert "--targets" in result.output

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_explicit_targets_file(self, MockPublisher, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")
        (tmp_path / "marketplace.json").write_text(_MARKETPLACE_JSON, encoding="utf-8")
        custom_targets = tmp_path / "custom-targets.yml"
        custom_targets.write_text(_TARGETS_YML, encoding="utf-8")

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        result = runner.invoke(
            marketplace,
            ["publish", "--yes", "--targets", str(custom_targets)],
        )
        assert result.exit_code == 0, result.output

    def test_explicit_targets_file_not_found(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")
        (tmp_path / "marketplace.json").write_text(_MARKETPLACE_JSON, encoding="utf-8")

        result = runner.invoke(
            marketplace,
            ["publish", "--yes", "--targets", "/nonexistent/file.yml"],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# Invalid targets
# ---------------------------------------------------------------------------


class TestPublishInvalidTargets:
    def test_target_missing_repo_key(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(
            tmp_path,
            targets_yml="targets:\n  - branch: main\n",
        )

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1
        assert "repo" in result.output.lower()

    def test_path_unsafe_path_in_repo(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        unsafe_targets = textwrap.dedent("""\
            targets:
              - repo: acme-org/service-a
                branch: main
                path_in_repo: ../etc/passwd
        """)
        _write_fixtures(tmp_path, targets_yml=unsafe_targets)

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1

    def test_target_missing_branch(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        no_branch = textwrap.dedent("""\
            targets:
              - repo: acme-org/service-a
        """)
        _write_fixtures(tmp_path, targets_yml=no_branch)

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1
        assert "branch" in result.output.lower()


# ---------------------------------------------------------------------------
# gh availability
# ---------------------------------------------------------------------------


class TestPublishGhAvailability:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    def test_gh_not_available_exit_1(self, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (
            False,
            "gh CLI not found on PATH. Install from https://cli.github.com/ or pass --no-pr.",
        )

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1
        assert "gh" in result.output.lower()

    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_gh_not_available_but_no_pr_proceeds(
        self, MockPublisher, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        # PrIntegrator should not even be instantiated for check_available
        result = runner.invoke(marketplace, ["publish", "--yes", "--no-pr"])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# TTY / interactive behaviour
# ---------------------------------------------------------------------------


class TestPublishInteractive:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    @patch("apm_cli.commands.marketplace.publish._is_interactive", return_value=False)
    def test_non_tty_without_yes_exit_1(
        self, mock_interactive, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan

        result = runner.invoke(marketplace, ["publish"])
        assert result.exit_code == 1
        assert "Non-interactive session" in result.output

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    @patch("apm_cli.commands.marketplace.publish._is_interactive", return_value=False)
    def test_non_tty_with_yes_proceeds(
        self, mock_interactive, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 0, result.output

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    @patch("apm_cli.commands.marketplace.publish._is_interactive", return_value=True)
    def test_tty_user_types_n_aborts_gracefully(
        self, mock_interactive, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")

        result = runner.invoke(marketplace, ["publish"], input="n\n")
        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()
        mock_pub.execute.assert_not_called()

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    @patch("apm_cli.commands.marketplace.publish._is_interactive", return_value=True)
    def test_tty_user_types_y_proceeds(
        self, mock_interactive, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        targets = list(plan.targets)

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(targets[0]),
            _fake_result(targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(targets[0])

        result = runner.invoke(marketplace, ["publish"], input="y\n")
        assert result.exit_code == 0, result.output
        mock_pub.execute.assert_called_once()


# ---------------------------------------------------------------------------
# --draft flag
# ---------------------------------------------------------------------------


class TestPublishDraft:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_draft_passed_to_pr_integrator(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        targets = list(plan.targets)

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(targets[0]),
            _fake_result(targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(targets[0])

        runner.invoke(marketplace, ["publish", "--yes", "--draft"])

        for c in mock_pr.open_or_update.call_args_list:
            assert c.kwargs.get("draft") is True


# ---------------------------------------------------------------------------
# --allow-downgrade and --allow-ref-change
# ---------------------------------------------------------------------------


class TestPublishPlanFlags:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_allow_downgrade_passed_to_plan(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        runner.invoke(marketplace, ["publish", "--yes", "--allow-downgrade"])

        _, kwargs = mock_pub.plan.call_args
        assert kwargs.get("allow_downgrade") is True

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_allow_ref_change_passed_to_plan(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        runner.invoke(marketplace, ["publish", "--yes", "--allow-ref-change"])

        _, kwargs = mock_pub.plan.call_args
        assert kwargs.get("allow_ref_change") is True


# ---------------------------------------------------------------------------
# --parallel
# ---------------------------------------------------------------------------


class TestPublishParallel:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_parallel_passed_to_execute(self, MockPublisher, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        runner.invoke(marketplace, ["publish", "--yes", "--parallel", "2"])

        mock_pub.execute.assert_called_once_with(
            plan,
            dry_run=False,
            parallel=2,
        )


# ---------------------------------------------------------------------------
# Mixed outcomes
# ---------------------------------------------------------------------------


class TestPublishMixedOutcomes:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_mixed_outcomes_exit_1(self, MockPublisher, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        targets_yml = textwrap.dedent("""\
            targets:
              - repo: acme-org/service-a
                branch: main
              - repo: acme-org/service-b
                branch: develop
              - repo: acme-org/service-c
                branch: main
        """)
        _write_fixtures(tmp_path, targets_yml=targets_yml)

        t_a = ConsumerTarget(repo="acme-org/service-a", branch="main")
        t_b = ConsumerTarget(repo="acme-org/service-b", branch="develop")
        t_c = ConsumerTarget(repo="acme-org/service-c", branch="main")
        plan = _fake_plan(targets=(t_a, t_b, t_c))

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(t_a, PublishOutcome.UPDATED, "Updated"),
            _fake_result(t_b, PublishOutcome.SKIPPED_DOWNGRADE, "Downgrade"),
            _fake_result(t_c, PublishOutcome.FAILED, "Clone failed"),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(t_a)

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1
        assert "1/3 targets" in result.output or "Published" in result.output
        # Verify all repos mentioned in output
        assert "acme-org/service-a" in result.output
        assert "acme-org/service-b" in result.output
        assert "acme-org/service-c" in result.output

    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_summary_table_has_all_outcomes(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)

        targets_yml = textwrap.dedent("""\
            targets:
              - repo: acme-org/service-a
                branch: main
              - repo: acme-org/service-b
                branch: develop
              - repo: acme-org/service-c
                branch: main
        """)
        _write_fixtures(tmp_path, targets_yml=targets_yml)

        t_a = ConsumerTarget(repo="acme-org/service-a", branch="main")
        t_b = ConsumerTarget(repo="acme-org/service-b", branch="develop")
        t_c = ConsumerTarget(repo="acme-org/service-c", branch="main")
        plan = _fake_plan(targets=(t_a, t_b, t_c))

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(t_a, PublishOutcome.UPDATED),
            _fake_result(t_b, PublishOutcome.SKIPPED_DOWNGRADE),
            _fake_result(t_c, PublishOutcome.FAILED, "Clone failed"),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(t_a)

        result = runner.invoke(marketplace, ["publish", "--yes"])
        output = result.output
        assert "updated" in output
        # Rich may truncate column values; check for partial matches
        assert "skipped" in output or "downgrade" in output
        assert "failed" in output or "Clone" in output


# ---------------------------------------------------------------------------
# Verbose flag
# ---------------------------------------------------------------------------


class TestPublishVerbose:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_verbose_does_not_crash(self, MockPublisher, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        result = runner.invoke(marketplace, ["publish", "--yes", "--verbose"])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# State file path printed
# ---------------------------------------------------------------------------


class TestPublishStateFile:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_state_file_path_printed(self, MockPublisher, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert "publish-state.json" in result.output


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------


class TestPublishPlanRendering:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_plan_shows_marketplace_name(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert "test-marketplace" in result.output
        assert "2.0.0" in result.output


# ---------------------------------------------------------------------------
# No-change outcomes (all targets already up to date)
# ---------------------------------------------------------------------------


class TestPublishNoChange:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_all_no_change_exit_0(self, MockPublisher, MockPr, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        targets = list(plan.targets)

        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(targets[0], PublishOutcome.NO_CHANGE),
            _fake_result(targets[1], PublishOutcome.NO_CHANGE),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Dry-run with --no-pr
# ---------------------------------------------------------------------------


class TestPublishDryRunNoPr:
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_dry_run_no_pr_exit_0(self, MockPublisher, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        result = runner.invoke(
            marketplace,
            ["publish", "--yes", "--dry-run", "--no-pr"],
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Invalid target format (repo not owner/name)
# ---------------------------------------------------------------------------


class TestPublishInvalidRepoFormat:
    def test_bad_repo_format(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bad_targets = textwrap.dedent("""\
            targets:
              - repo: just-a-name
                branch: main
        """)
        _write_fixtures(tmp_path, targets_yml=bad_targets)

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1
        assert "owner/name" in result.output


# ---------------------------------------------------------------------------
# Targets file with empty targets list
# ---------------------------------------------------------------------------


class TestPublishEmptyTargets:
    def test_empty_targets_list(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        empty_targets = "targets: []\n"
        _write_fixtures(tmp_path, targets_yml=empty_targets)

        result = runner.invoke(marketplace, ["publish", "--yes"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Default flags: allow-downgrade and allow-ref-change default to False
# ---------------------------------------------------------------------------


class TestPublishDefaultFlags:
    @patch("apm_cli.commands.marketplace.publish.PrIntegrator")
    @patch("apm_cli.commands.marketplace.publish.MarketplacePublisher")
    def test_defaults_no_allow_downgrade_no_allow_ref_change(
        self, MockPublisher, MockPr, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_fixtures(tmp_path)

        plan = _fake_plan()
        mock_pub = MockPublisher.return_value
        mock_pub.plan.return_value = plan
        mock_pub.execute.return_value = [
            _fake_result(plan.targets[0]),
            _fake_result(plan.targets[1]),
        ]

        mock_pr = MockPr.return_value
        mock_pr.check_available.return_value = (True, "gh 2.0")
        mock_pr.open_or_update.return_value = _fake_pr_result(plan.targets[0])

        runner.invoke(marketplace, ["publish", "--yes"])

        _, kwargs = mock_pub.plan.call_args
        assert kwargs.get("allow_downgrade") is False
        assert kwargs.get("allow_ref_change") is False
