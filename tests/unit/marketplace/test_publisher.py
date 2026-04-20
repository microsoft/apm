"""Tests for publisher.py -- MarketplacePublisher, PublishState, data model."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from apm_cli.marketplace.publisher import (
    ConsumerTarget,
    MarketplacePublisher,
    PublishOutcome,
    PublishPlan,
    PublishState,
    TargetResult,
    _redact_token,
)
from apm_cli.utils.path_security import PathTraversalError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASIC_MARKETPLACE_YML = textwrap.dedent("""\
    name: acme-tools
    description: Curated developer tools
    version: 2.0.0
    owner:
      name: Acme Corp
    packages:
      - name: code-reviewer
        source: acme-org/code-reviewer
        version: "^2.0.0"
        description: Automated code review assistant
        tags: [review, quality]
""")

_CONSUMER_APM_YML_V1 = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@acme-tools#v1.0.0
""")

_CONSUMER_APM_YML_V2 = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@acme-tools#v2.0.0
""")

_CONSUMER_APM_YML_NO_REF = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@acme-tools
""")

_CONSUMER_APM_YML_BRANCH_REF = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@acme-tools#main
""")

_CONSUMER_APM_YML_SHA_REF = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@acme-tools#abc123def456
""")

_CONSUMER_APM_YML_NO_MATCH = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@other-marketplace#v1.0.0
""")

_CONSUMER_APM_YML_MULTI_MATCH = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@acme-tools#v1.0.0
        - test-generator@acme-tools#v1.0.0
""")

_CONSUMER_APM_YML_MIXED = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@acme-tools#v1.0.0
        - microsoft/apm-sample-package#v1.0.0
""")

_CONSUMER_APM_YML_CASE_INSENSITIVE = textwrap.dedent("""\
    dependencies:
      apm:
        - code-reviewer@ACME-TOOLS#v1.0.0
""")


def _write_marketplace_yml(
    root: Path, content: str = _BASIC_MARKETPLACE_YML
) -> Path:
    """Write a marketplace.yml file and return the root path."""
    yml_path = root / "marketplace.yml"
    yml_path.write_text(content, encoding="utf-8")
    return root


def _fixed_clock(
    ts: datetime | None = None,
) -> datetime:
    """Return a fixed timestamp for deterministic tests."""
    if ts is not None:
        return ts
    return datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


class FakeRunner:
    """Injectable ``subprocess.run`` replacement for tests.

    Records all calls and can be configured with:
    - ``clone_files``: dict mapping repo -> {path: content} to create
      when a ``git clone`` command targets that repo.
    - ``log_output``: stdout returned by ``git log`` commands.
    - ``fail_on``: set of (verb,) tuples; when a command starts with
      ``["git", verb]``, the runner raises ``CalledProcessError``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.clone_files: dict[str, dict[str, str]] = {}
        self.log_output: str = ""
        self.fail_on: set[str] = set()

    def __call__(
        self, cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess:
        self.calls.append((list(cmd), dict(kwargs)))

        # Check for configured failures
        if len(cmd) >= 2 and cmd[1] in self.fail_on:
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(
                    1, cmd, output="", stderr="command failed"
                )
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="command failed"
            )

        # Handle git clone
        if len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "clone":
            target_dir = cmd[-1]
            os.makedirs(target_dir, exist_ok=True)
            for repo, files in self.clone_files.items():
                clone_url = f"https://github.com/{repo}.git"
                if clone_url in cmd:
                    for path, content in files.items():
                        full_path = Path(target_dir) / path
                        full_path.parent.mkdir(
                            parents=True, exist_ok=True
                        )
                        full_path.write_text(content, encoding="utf-8")
                    break
            return subprocess.CompletedProcess(
                cmd, 0, stdout="", stderr=""
            )

        # Handle git log (for safe_force_push)
        if len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "log":
            return subprocess.CompletedProcess(
                cmd, 0, stdout=self.log_output, stderr=""
            )

        # Default: success
        return subprocess.CompletedProcess(
            cmd, 0, stdout="", stderr=""
        )

    def git_calls(self, verb: str | None = None) -> list[list[str]]:
        """Return git command lists, optionally filtered by verb."""
        result = []
        for cmd, _ in self.calls:
            if cmd[0] != "git":
                continue
            if verb is None or (len(cmd) > 1 and cmd[1] == verb):
                result.append(cmd)
        return result


def _make_publisher(
    tmp_path: Path,
    *,
    yml_content: str = _BASIC_MARKETPLACE_YML,
    runner: FakeRunner | None = None,
) -> tuple[MarketplacePublisher, FakeRunner]:
    """Create a publisher with a fake runner and marketplace.yml."""
    _write_marketplace_yml(tmp_path, yml_content)
    if runner is None:
        runner = FakeRunner()
    publisher = MarketplacePublisher(
        tmp_path,
        clock=_fixed_clock,
        runner=runner,
    )
    return publisher, runner


# ===================================================================
# State file tests
# ===================================================================


class TestPublishState:
    """Tests for the transactional state file manager."""

    def test_load_missing_file_returns_fresh(self, tmp_path: Path) -> None:
        state = PublishState.load(tmp_path)
        assert state.data["schemaVersion"] == 1
        assert state.data["lastRun"] is None
        assert state.data["history"] == []

    def test_load_corrupt_file_returns_fresh(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        (apm_dir / "publish-state.json").write_text(
            "not valid json {{{", encoding="utf-8"
        )
        state = PublishState.load(tmp_path)
        assert state.data["schemaVersion"] == 1
        assert state.data["lastRun"] is None

    def test_begin_run_creates_apm_dir(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        state.begin_run(plan)
        assert (tmp_path / ".apm" / "publish-state.json").exists()

    def test_begin_run_writes_started_at(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        state.begin_run(plan)
        data = state.data
        assert data["lastRun"]["startedAt"] is not None
        assert data["lastRun"]["finishedAt"] is None
        assert data["lastRun"]["marketplaceName"] == "acme-tools"
        assert data["lastRun"]["marketplaceVersion"] == "2.0.0"

    def test_record_result_appends(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        state.begin_run(plan)

        target = ConsumerTarget(repo="acme-org/svc-a")
        result = TargetResult(
            target=target,
            outcome=PublishOutcome.UPDATED,
            message="Updated to 2.0.0",
            old_version="1.0.0",
            new_version="2.0.0",
        )
        state.record_result(result)

        results = state.data["lastRun"]["results"]
        assert len(results) == 1
        assert results[0]["repo"] == "acme-org/svc-a"
        assert results[0]["outcome"] == "updated"

    def test_record_result_without_begin_is_noop(
        self, tmp_path: Path
    ) -> None:
        state = PublishState(tmp_path)
        target = ConsumerTarget(repo="acme-org/svc-a")
        result = TargetResult(
            target=target,
            outcome=PublishOutcome.UPDATED,
            message="test",
        )
        # Should not raise
        state.record_result(result)
        assert state.data["lastRun"] is None

    def test_finalise_sets_finished_at(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        state.begin_run(plan)
        finished = datetime(2025, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        state.finalise(finished)
        assert (
            state.data["lastRun"]["finishedAt"]
            == finished.isoformat()
        )

    def test_finalise_rotates_history(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        state.begin_run(plan)
        finished = datetime(2025, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        state.finalise(finished)
        assert len(state.data["history"]) == 1
        assert (
            state.data["history"][0]["marketplaceName"] == "acme-tools"
        )

    def test_history_trimmed_at_10(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        for i in range(12):
            plan = PublishPlan(
                marketplace_name="acme-tools",
                marketplace_version=f"{i}.0.0",
                targets=(),
                commit_message="test",
                branch_name=f"branch-{i}",
                new_ref=f"v{i}.0.0",
                tag_pattern_used="v{version}",
            )
            state.begin_run(plan)
            finished = datetime(
                2025, 1, 15, 12, i, 0, tzinfo=timezone.utc
            )
            state.finalise(finished)
        assert len(state.data["history"]) == 10
        # Most recent should be first
        assert (
            state.data["history"][0]["marketplaceVersion"] == "11.0.0"
        )

    def test_abort_sets_marker(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        state.begin_run(plan)
        state.abort("network failure")
        assert state.data["lastRun"]["finishedAt"].startswith(
            "ABORTED:"
        )
        assert "network failure" in state.data["lastRun"]["finishedAt"]

    def test_round_trip_persistence(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        state.begin_run(plan)
        finished = datetime(2025, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        state.finalise(finished)

        # Reload from disk
        state2 = PublishState.load(tmp_path)
        assert (
            state2.data["lastRun"]["marketplaceName"] == "acme-tools"
        )
        assert len(state2.data["history"]) == 1

    def test_atomic_write_no_partial_on_disk(
        self, tmp_path: Path
    ) -> None:
        """Verify the temp file is cleaned up after a successful write."""
        state = PublishState(tmp_path)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        state.begin_run(plan)
        tmp_file = tmp_path / ".apm" / "publish-state.json.tmp"
        assert not tmp_file.exists()


# ===================================================================
# plan() tests
# ===================================================================


class TestPublishPlan:
    """Tests for MarketplacePublisher.plan()."""

    def test_plan_loads_yml_name_and_version(
        self, tmp_path: Path
    ) -> None:
        pub, _ = _make_publisher(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        assert plan.marketplace_name == "acme-tools"
        assert plan.marketplace_version == "2.0.0"

    def test_plan_deterministic_branch_name(
        self, tmp_path: Path
    ) -> None:
        pub, _ = _make_publisher(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan1 = pub.plan(targets)
        plan2 = pub.plan(targets)
        assert plan1.branch_name == plan2.branch_name
        assert plan1.branch_name.startswith(
            "apm/marketplace-update-acme-tools-2.0.0-"
        )

    def test_plan_hash_stable_across_calls(
        self, tmp_path: Path
    ) -> None:
        pub, _ = _make_publisher(tmp_path)
        targets = [
            ConsumerTarget(repo="acme-org/svc-a"),
            ConsumerTarget(repo="acme-org/svc-b"),
        ]
        plan1 = pub.plan(targets)
        plan2 = pub.plan(targets)
        assert plan1.commit_message == plan2.commit_message

    def test_plan_hash_changes_with_target_package(
        self, tmp_path: Path
    ) -> None:
        pub, _ = _make_publisher(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan1 = pub.plan(targets)
        plan2 = pub.plan(targets, target_package="code-reviewer")
        assert plan1.branch_name != plan2.branch_name

    def test_plan_commit_message_contains_trailer(
        self, tmp_path: Path
    ) -> None:
        pub, _ = _make_publisher(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        assert "APM-Publish-Id:" in plan.commit_message
        assert "chore(apm): bump acme-tools to 2.0.0" in (
            plan.commit_message
        )

    def test_plan_rejects_path_traversal(
        self, tmp_path: Path
    ) -> None:
        pub, _ = _make_publisher(tmp_path)
        targets = [
            ConsumerTarget(
                repo="acme-org/svc-a",
                path_in_repo="../etc/passwd",
            )
        ]
        with pytest.raises(PathTraversalError):
            pub.plan(targets)

    def test_plan_rejects_dot_dot_path(
        self, tmp_path: Path
    ) -> None:
        pub, _ = _make_publisher(tmp_path)
        targets = [
            ConsumerTarget(
                repo="acme-org/svc-a",
                path_in_repo="../../secrets.yml",
            )
        ]
        with pytest.raises(PathTraversalError):
            pub.plan(targets)

    def test_plan_stores_flags(self, tmp_path: Path) -> None:
        pub, _ = _make_publisher(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(
            targets,
            allow_downgrade=True,
            allow_ref_change=True,
        )
        assert plan.allow_downgrade is True
        assert plan.allow_ref_change is True

    def test_plan_branch_name_sanitised(
        self, tmp_path: Path
    ) -> None:
        """Marketplace names with spaces/special chars are sanitised."""
        yml = textwrap.dedent("""\
            name: "acme tools v2"
            description: Tools
            version: 1.0.0
            owner:
              name: Acme Corp
        """)
        pub, _ = _make_publisher(tmp_path, yml_content=yml)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        # Spaces replaced with hyphens
        assert " " not in plan.branch_name
        assert "acme-tools-v2" in plan.branch_name

    def test_plan_hash_independent_of_target_order(
        self, tmp_path: Path
    ) -> None:
        """Hash is stable regardless of target ordering (repos sorted)."""
        pub, _ = _make_publisher(tmp_path)
        targets_a = [
            ConsumerTarget(repo="acme-org/svc-a"),
            ConsumerTarget(repo="acme-org/svc-b"),
        ]
        targets_b = [
            ConsumerTarget(repo="acme-org/svc-b"),
            ConsumerTarget(repo="acme-org/svc-a"),
        ]
        plan_a = pub.plan(targets_a)
        plan_b = pub.plan(targets_b)
        assert plan_a.branch_name == plan_b.branch_name

    def test_plan_computes_new_ref(self, tmp_path: Path) -> None:
        """new_ref is computed via render_tag from tag_pattern."""
        pub, _ = _make_publisher(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        assert plan.new_ref == "v2.0.0"
        assert plan.tag_pattern_used == "v{version}"

    def test_plan_custom_tag_pattern(self, tmp_path: Path) -> None:
        """Custom tag_pattern in marketplace.yml is honoured."""
        yml = textwrap.dedent("""\
            name: acme-tools
            description: Tools
            version: 3.1.0
            owner:
              name: Acme Corp
            build:
              tagPattern: "{name}-v{version}"
        """)
        pub, _ = _make_publisher(tmp_path, yml_content=yml)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        assert plan.new_ref == "acme-tools-v3.1.0"
        assert plan.tag_pattern_used == "{name}-v{version}"


# ===================================================================
# execute() tests
# ===================================================================


class TestExecuteHappyPath:
    """Tests for MarketplacePublisher.execute() -- happy path."""

    def test_execute_updates_single_entry(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert len(results) == 1
        assert results[0].outcome == PublishOutcome.UPDATED
        assert results[0].old_version == "v1.0.0"
        assert results[0].new_version == "v2.0.0"

    def test_execute_runs_git_add_commit_push(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        pub.execute(plan)

        add_calls = runner.git_calls("add")
        commit_calls = runner.git_calls("commit")
        push_calls = runner.git_calls("push")
        assert len(add_calls) == 1
        assert len(commit_calls) == 1
        assert len(push_calls) == 1
        assert "apm.yml" in add_calls[0]
        assert "-u" in push_calls[0]

    def test_execute_case_insensitive_match(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_CASE_INSENSITIVE,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.UPDATED

    def test_execute_records_state(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        pub.execute(plan)

        state = PublishState.load(tmp_path)
        assert state.data["lastRun"] is not None
        assert state.data["lastRun"]["finishedAt"] is not None
        assert len(state.data["lastRun"]["results"]) == 1

    def test_execute_multiple_targets(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        runner.clone_files["acme-org/svc-b"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [
            ConsumerTarget(repo="acme-org/svc-a"),
            ConsumerTarget(repo="acme-org/svc-b"),
        ]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert len(results) == 2
        assert all(
            r.outcome == PublishOutcome.UPDATED for r in results
        )

    def test_execute_multi_match_updates_all(
        self, tmp_path: Path
    ) -> None:
        """Multiple plugins from the same marketplace are all updated."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_MULTI_MATCH,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.UPDATED
        assert "2" in results[0].message  # "Updated 2 entries"

    def test_execute_ignores_direct_repo_refs(
        self, tmp_path: Path
    ) -> None:
        """Direct repo refs (owner/repo#ref) are not marketplace entries."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_MIXED,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        # Only the marketplace entry is updated; direct repo ref is ignored
        assert results[0].outcome == PublishOutcome.UPDATED

    def test_execute_new_ref_computed_from_tag_pattern(
        self, tmp_path: Path
    ) -> None:
        """plan().new_ref is computed via render_tag from tag_pattern."""
        pub, _ = _make_publisher(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        assert plan.new_ref == "v2.0.0"
        assert plan.tag_pattern_used == "v{version}"


class TestExecuteGuards:
    """Tests for downgrade, ref-change, and no-change guards."""

    def test_downgrade_guard_skips(self, tmp_path: Path) -> None:
        """Consumer at v3.0.0, marketplace publishing v2.0.0 -> downgrade."""
        runner = FakeRunner()
        consumer_yml = textwrap.dedent("""\
            dependencies:
              apm:
                - code-reviewer@acme-tools#v3.0.0
        """)
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": consumer_yml,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.SKIPPED_DOWNGRADE
        assert "Downgrade" in results[0].message

    def test_downgrade_guard_allowed(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        consumer_yml = textwrap.dedent("""\
            dependencies:
              apm:
                - code-reviewer@acme-tools#v3.0.0
        """)
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": consumer_yml,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets, allow_downgrade=True)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.UPDATED

    def test_ref_change_guard_implicit_latest(
        self, tmp_path: Path
    ) -> None:
        """Entry without #ref (implicit latest) triggers ref-change guard."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_NO_REF,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.SKIPPED_REF_CHANGE
        assert "allow_ref_change" in results[0].message

    def test_ref_change_guard_implicit_allowed(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_NO_REF,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets, allow_ref_change=True)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.UPDATED

    def test_ref_change_guard_branch_ref(
        self, tmp_path: Path
    ) -> None:
        """Non-semver old ref (branch name) + semver new ref -> guard."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_BRANCH_REF,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.SKIPPED_REF_CHANGE
        assert "main" in results[0].message

    def test_ref_change_guard_sha_ref(
        self, tmp_path: Path
    ) -> None:
        """Non-semver old ref (SHA) + semver new ref -> guard."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_SHA_REF,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.SKIPPED_REF_CHANGE
        assert "abc123def456" in results[0].message

    def test_ref_change_guard_branch_allowed(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_BRANCH_REF,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets, allow_ref_change=True)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.UPDATED

    def test_no_change_identical_pin(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V2,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.NO_CHANGE
        assert "Already at" in results[0].message

    def test_no_change_no_commit_no_push(
        self, tmp_path: Path
    ) -> None:
        """When ref is unchanged, no git commit or push should occur."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V2,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        pub.execute(plan)

        assert len(runner.git_calls("commit")) == 0
        assert len(runner.git_calls("push")) == 0

    def test_downgrade_guard_any_entry_fires(
        self, tmp_path: Path
    ) -> None:
        """If ANY matching entry triggers downgrade, entire target skipped."""
        runner = FakeRunner()
        consumer_yml = textwrap.dedent("""\
            dependencies:
              apm:
                - code-reviewer@acme-tools#v1.0.0
                - test-gen@acme-tools#v3.0.0
        """)
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": consumer_yml,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.SKIPPED_DOWNGRADE


class TestExecuteMatching:
    """Tests for marketplace name matching via parse_marketplace_ref."""

    def test_not_found(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_NO_MATCH,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "not referenced" in results[0].message.lower()

    def test_empty_apm_list(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": "dependencies:\n  apm: []\n",
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "not referenced" in results[0].message.lower()

    def test_missing_dependencies_key(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": "some_key: value\n",
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "not referenced" in results[0].message.lower()

    def test_missing_apm_key(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": "dependencies:\n  npm: []\n",
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "not referenced" in results[0].message.lower()

    def test_only_direct_repo_refs_no_match(
        self, tmp_path: Path
    ) -> None:
        """All entries are direct repo refs -- no marketplace match."""
        runner = FakeRunner()
        consumer_yml = textwrap.dedent("""\
            dependencies:
              apm:
                - microsoft/apm-sample-package#v1.0.0
                - acme-org/code-reviewer#v2.0.0
        """)
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": consumer_yml,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED

    def test_malformed_entry_warning_included(
        self, tmp_path: Path
    ) -> None:
        """Malformed entries (semver range) produce warnings but continue."""
        runner = FakeRunner()
        consumer_yml = textwrap.dedent("""\
            dependencies:
              apm:
                - code-reviewer@acme-tools#v1.0.0
                - bad-plugin@acme-tools#^2.0.0
        """)
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": consumer_yml,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        # The valid entry should still be matched and updated
        assert results[0].outcome == PublishOutcome.UPDATED

    def test_malformed_only_entries_fails_with_warning(
        self, tmp_path: Path
    ) -> None:
        """All marketplace entries malformed -> FAILED with warnings."""
        runner = FakeRunner()
        consumer_yml = textwrap.dedent("""\
            dependencies:
              apm:
                - bad@acme-tools#^2.0.0
        """)
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": consumer_yml,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "warning" in results[0].message.lower()

    def test_non_string_entries_skipped(
        self, tmp_path: Path
    ) -> None:
        """Non-string entries in the list are silently skipped."""
        runner = FakeRunner()
        consumer_yml = textwrap.dedent("""\
            dependencies:
              apm:
                - code-reviewer@acme-tools#v1.0.0
                - 42
        """)
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": consumer_yml,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.UPDATED


class TestExecuteDryRun:
    """Tests for dry_run mode."""

    def test_dry_run_no_push(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan, dry_run=True)

        assert results[0].outcome == PublishOutcome.UPDATED
        assert len(runner.git_calls("push")) == 0

    def test_dry_run_still_commits_locally(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        pub.execute(plan, dry_run=True)

        assert len(runner.git_calls("commit")) == 1

    def test_dry_run_records_state(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        pub.execute(plan, dry_run=True)

        state = PublishState.load(tmp_path)
        results = state.data["lastRun"]["results"]
        assert len(results) == 1
        assert results[0]["outcome"] == "updated"


class TestExecuteErrorIsolation:
    """Tests for error isolation between targets."""

    def test_exception_in_one_target_does_not_abort_others(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        # svc-a will fail (no files in clone)
        runner.clone_files["acme-org/svc-a"] = {}
        # svc-b will succeed
        runner.clone_files["acme-org/svc-b"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [
            ConsumerTarget(repo="acme-org/svc-a"),
            ConsumerTarget(repo="acme-org/svc-b"),
        ]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert len(results) == 2
        # One should fail, the other succeed
        outcomes = {r.target.repo: r.outcome for r in results}
        assert outcomes["acme-org/svc-a"] == PublishOutcome.FAILED
        assert outcomes["acme-org/svc-b"] == PublishOutcome.UPDATED

    def test_clone_failure_recorded_as_failed(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.fail_on.add("clone")
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "Clone failed" in results[0].message

    def test_push_failure_recorded_as_failed(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        runner.fail_on.add("push")
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "Push failed" in results[0].message

    def test_commit_failure_recorded_as_failed(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        runner.fail_on.add("commit")
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "Commit failed" in results[0].message

    def test_invalid_yaml_recorded_as_failed(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": "{{invalid yaml",
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "parse" in results[0].message.lower()

    def test_file_not_found_recorded_as_failed(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        # Clone creates the dir but no apm.yml
        runner.clone_files["acme-org/svc-a"] = {}
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "not found" in results[0].message.lower()


class TestExecutePathSecurity:
    """Tests for path security during execution."""

    def test_path_traversal_in_repo_rejected_at_execute(
        self, tmp_path: Path
    ) -> None:
        """Even if plan() is bypassed, execute() checks path containment."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        # Build a plan manually with a traversal path (bypassing plan()
        # validation)
        plan = PublishPlan(
            marketplace_name="acme-tools",
            marketplace_version="2.0.0",
            targets=(
                ConsumerTarget(
                    repo="acme-org/svc-a",
                    path_in_repo="../../../etc/passwd",
                ),
            ),
            commit_message="test",
            branch_name="test-branch",
            new_ref="v2.0.0",
            tag_pattern_used="v{version}",
        )
        results = pub.execute(plan)
        assert results[0].outcome == PublishOutcome.FAILED
        assert "traversal" in results[0].message.lower()


class TestTokenRedaction:
    """Tests for token redaction in error messages."""

    def test_redact_token_in_stderr(self) -> None:
        raw = (
            "fatal: authentication failed for "
            "'https://x-access-token:ghp_FAKE123@github.com/acme/tools'"
        )
        redacted = _redact_token(raw)
        assert "ghp_FAKE123" not in redacted
        assert "https://***@" in redacted

    def test_redact_token_no_token(self) -> None:
        raw = "fatal: repository not found"
        assert _redact_token(raw) == raw

    def test_clone_error_token_redacted(
        self, tmp_path: Path
    ) -> None:
        """Clone failure stderr with embedded token is redacted."""

        class TokenRunner(FakeRunner):
            def __call__(self, cmd, **kwargs):
                self.calls.append((list(cmd), dict(kwargs)))
                if cmd[1] == "clone" and kwargs.get("check"):
                    raise subprocess.CalledProcessError(
                        128,
                        cmd,
                        stdout="",
                        stderr=(
                            "fatal: authentication failed for "
                            "'https://x-access-token:ghp_FAKE123"
                            "@github.com/acme/tools'"
                        ),
                    )
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="", stderr=""
                )

        runner = TokenRunner()
        pub = MarketplacePublisher(
            tmp_path,
            clock=_fixed_clock,
            runner=runner,
        )
        _write_marketplace_yml(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "ghp_FAKE123" not in results[0].message

    def test_recorded_state_token_redacted(
        self, tmp_path: Path
    ) -> None:
        """State file result messages are also redacted."""

        class TokenRunner(FakeRunner):
            def __call__(self, cmd, **kwargs):
                self.calls.append((list(cmd), dict(kwargs)))
                if cmd[1] == "clone" and kwargs.get("check"):
                    raise subprocess.CalledProcessError(
                        128,
                        cmd,
                        stdout="",
                        stderr=(
                            "https://x-access-token:ghp_SECRET"
                            "@github.com/x/y"
                        ),
                    )
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="", stderr=""
                )

        runner = TokenRunner()
        pub = MarketplacePublisher(
            tmp_path,
            clock=_fixed_clock,
            runner=runner,
        )
        _write_marketplace_yml(tmp_path)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        pub.execute(plan)

        state = PublishState.load(tmp_path)
        msg = state.data["lastRun"]["results"][0]["message"]
        assert "ghp_SECRET" not in msg


# ===================================================================
# safe_force_push() tests
# ===================================================================


class TestSafeForcePush:
    """Tests for MarketplacePublisher.safe_force_push()."""

    def test_trailer_match_pushes(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.log_output = (
            "chore(apm): bump acme-tools to 2.0.0\n\n"
            "APM-Publish-Id: abc12345\n"
        )
        pub, _ = _make_publisher(tmp_path, runner=runner)

        result = pub.safe_force_push(
            "origin", "apm/update-branch", "abc12345"
        )
        assert result is True
        push_calls = runner.git_calls("push")
        assert len(push_calls) == 1
        assert "--force-with-lease" in push_calls[0]

    def test_trailer_mismatch_refuses(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.log_output = (
            "chore(apm): bump acme-tools to 2.0.0\n\n"
            "APM-Publish-Id: different-hash\n"
        )
        pub, _ = _make_publisher(tmp_path, runner=runner)

        result = pub.safe_force_push(
            "origin", "apm/update-branch", "abc12345"
        )
        assert result is False
        push_calls = runner.git_calls("push")
        assert len(push_calls) == 0

    def test_no_trailer_refuses(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.log_output = "some random commit message\n"
        pub, _ = _make_publisher(tmp_path, runner=runner)

        result = pub.safe_force_push(
            "origin", "apm/update-branch", "abc12345"
        )
        assert result is False

    def test_git_log_failure_returns_false(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.fail_on.add("log")
        pub, _ = _make_publisher(tmp_path, runner=runner)

        result = pub.safe_force_push(
            "origin", "apm/update-branch", "abc12345"
        )
        assert result is False

    def test_push_failure_returns_false(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.log_output = "APM-Publish-Id: abc12345\n"
        runner.fail_on.add("push")
        pub, _ = _make_publisher(tmp_path, runner=runner)

        result = pub.safe_force_push(
            "origin", "apm/update-branch", "abc12345"
        )
        assert result is False


# ===================================================================
# Data model tests
# ===================================================================


class TestDataModel:
    """Tests for the data model classes."""

    def test_consumer_target_defaults(self) -> None:
        t = ConsumerTarget(repo="acme-org/svc-a")
        assert t.branch == "main"
        assert t.path_in_repo == "apm.yml"

    def test_consumer_target_frozen(self) -> None:
        t = ConsumerTarget(repo="acme-org/svc-a")
        with pytest.raises(AttributeError):
            t.repo = "other"  # type: ignore[misc]

    def test_publish_plan_frozen(self) -> None:
        plan = PublishPlan(
            marketplace_name="test",
            marketplace_version="1.0.0",
            targets=(),
            commit_message="test",
            branch_name="test",
            new_ref="v1.0.0",
            tag_pattern_used="v{version}",
        )
        with pytest.raises(AttributeError):
            plan.marketplace_name = "other"  # type: ignore[misc]

    def test_target_result_frozen(self) -> None:
        t = ConsumerTarget(repo="acme-org/svc-a")
        result = TargetResult(
            target=t,
            outcome=PublishOutcome.UPDATED,
            message="test",
        )
        with pytest.raises(AttributeError):
            result.message = "other"  # type: ignore[misc]

    def test_publish_outcome_values(self) -> None:
        assert PublishOutcome.UPDATED.value == "updated"
        assert PublishOutcome.NO_CHANGE.value == "no-change"
        assert PublishOutcome.SKIPPED_DOWNGRADE.value == (
            "skipped-downgrade"
        )
        assert PublishOutcome.SKIPPED_REF_CHANGE.value == (
            "skipped-ref-change"
        )
        assert PublishOutcome.FAILED.value == "failed"

    def test_publish_outcome_is_str(self) -> None:
        """PublishOutcome(str, Enum) instances are also strings."""
        assert isinstance(PublishOutcome.UPDATED, str)


# ===================================================================
# Edge case tests
# ===================================================================


class TestEdgeCases:
    """Miscellaneous edge cases."""

    def test_non_dict_yaml_is_failed(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": "- just a list\n",
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "mapping" in results[0].message.lower()

    def test_dependencies_apm_not_a_list(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": "dependencies:\n  apm: not-a-list\n",
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "not referenced" in results[0].message.lower()

    def test_custom_path_in_repo(self, tmp_path: Path) -> None:
        """Targets can use a custom path for apm.yml."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "config/apm.yml": _CONSUMER_APM_YML_V1,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [
            ConsumerTarget(
                repo="acme-org/svc-a",
                path_in_repo="config/apm.yml",
            )
        ]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.UPDATED

    def test_results_preserved_in_target_order(
        self, tmp_path: Path
    ) -> None:
        """Results list matches plan.targets order, not completion order."""
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        runner.clone_files["acme-org/svc-b"] = {
            "apm.yml": _CONSUMER_APM_YML_V2,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [
            ConsumerTarget(repo="acme-org/svc-a"),
            ConsumerTarget(repo="acme-org/svc-b"),
        ]
        plan = pub.plan(targets)
        results = pub.execute(plan, parallel=1)

        assert results[0].target.repo == "acme-org/svc-a"
        assert results[1].target.repo == "acme-org/svc-b"
        assert results[0].outcome == PublishOutcome.UPDATED
        assert results[1].outcome == PublishOutcome.NO_CHANGE

    def test_semver_comparison_strips_v_prefix(
        self, tmp_path: Path
    ) -> None:
        """Downgrade guard strips leading 'v' for semver comparison."""
        runner = FakeRunner()
        # Consumer pinned at v3.0.0, marketplace publishing v2.0.0
        consumer_yml = textwrap.dedent("""\
            dependencies:
              apm:
                - code-reviewer@acme-tools#v3.0.0
        """)
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": consumer_yml,
        }
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.SKIPPED_DOWNGRADE

    def test_branch_checkout_failure(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.clone_files["acme-org/svc-a"] = {
            "apm.yml": _CONSUMER_APM_YML_V1,
        }
        runner.fail_on.add("checkout")
        pub, _ = _make_publisher(tmp_path, runner=runner)
        targets = [ConsumerTarget(repo="acme-org/svc-a")]
        plan = pub.plan(targets)
        results = pub.execute(plan)

        assert results[0].outcome == PublishOutcome.FAILED
        assert "Branch creation failed" in results[0].message
