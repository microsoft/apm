"""Invocation-mode, context, and CODEOWNERS contracts for advisory workflows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
TRIAGE_SKILL = ROOT / "packages/autopilot-issue-triage-worker/SKILL.md"
TRIAGE_TEMPLATE = ROOT / "packages/autopilot-issue-triage-worker/assets/triage-template.md"
ALIAS_TRIAGE_PANEL = ROOT / "packages/apm-triage-panel/.apm/skills/apm-triage-panel/SKILL.md"
TRIAGE_WORKFLOW = ROOT / ".github/workflows/triage-panel.md"
REVIEW_SKILL = ROOT / "packages/apm-review-panel/SKILL.md"
REVIEW_WORKFLOW = ROOT / ".github/workflows/pr-review-panel.md"
SCHEDULER_TRIAGE = (
    ROOT
    / "packages/autopilot-issue-triage-scheduler/.apm/skills/autopilot-issue-triage-scheduler/SKILL.md"
)
SCHEDULER_CODE = (
    ROOT
    / "packages/autopilot-issue-delivery-scheduler/.apm/skills/autopilot-issue-delivery-scheduler/SKILL.md"
)
ALIAS_DELIVERY = (
    ROOT
    / "packages/autopilot-scheduler-issue-delivery/.apm/skills/autopilot-scheduler-issue-delivery/SKILL.md"
)
SCHEDULER_PR_REVIEW = (
    ROOT
    / "packages/autopilot-pr-review-scheduler/.apm/skills/autopilot-pr-review-scheduler/SKILL.md"
)
SCHEDULER_PR_TRIAGE = (
    ROOT
    / "packages/autopilot-pr-triage-scheduler/.apm/skills/autopilot-pr-triage-scheduler/SKILL.md"
)
WORKER_PR_TRIAGE = (
    ROOT / "packages/autopilot-pr-triage-worker/.apm/skills/autopilot-pr-triage-worker/SKILL.md"
)
ALIAS_SCHEDULER_ISSUES = (
    ROOT / "packages/autopilot-scheduler-issues/.apm/skills/autopilot-scheduler-issues/SKILL.md"
)
ALIAS_SCHEDULER_PRS = (
    ROOT
    / "packages/autopilot-scheduler-pull-requests/.apm/skills/autopilot-scheduler-pull-requests/SKILL.md"
)
WORKER_CODE = (
    ROOT
    / "packages/autopilot-issue-delivery-worker/.apm/skills/autopilot-issue-delivery-worker/SKILL.md"
)
ALIAS_WORKER_ISSUE = (
    ROOT / "packages/autopilot-worker-issue/.apm/skills/autopilot-worker-issue/SKILL.md"
)
WORKER_PR = ROOT / "packages/autopilot-pr-review-worker/assets/worker-prompt.md"
WORKER_PR_SKILL = ROOT / "packages/autopilot-pr-review-worker/SKILL.md"
ALIAS_AUTOPILOT = ROOT / "packages/apm-issue-autopilot/.apm/skills/apm-issue-autopilot/SKILL.md"
ALIAS_SHEPHERD = ROOT / "packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/SKILL.md"
ALIAS_DRIVER = ROOT / "packages/shepherd-driver/SKILL.md"


def _ascii(path: Path) -> str:
    """Read a contract file as printable ASCII with newlines flattened."""
    return " ".join(path.read_text(encoding="ascii").split())


def test_review_panel_declares_origin_intent_contract() -> None:
    """Session, unattended, and composed review must not share writes."""
    skill = _ascii(REVIEW_SKILL)
    for token in (
        "unattended",
        "actor-session",
        "agentic-workflow",
        "session-review",
        "direct-user-review",
        "composed-implementation-review",
        "Cloud Agent",
        "Remote Agent",
    ):
        assert token in skill
    assert "Do not infer COMPOSED from parent skill names." in skill
    assert "Else ORIGIN=`unattended` (fail closed: no ownership writes)." in skill
    assert "This skill never assigns issues or PRs in any mode." in skill
    assert "This skill owns the actor-session `@me` reviewer request" in skill
    assert "The PR review scheduler never comments" in skill
    assert "No accepted, no review" in skill
    assert "remove `panel-review` if present" in skill
    assert "Request authenticated `@me` as a supplemental reviewer only" in skill
    assert "self-review-red-flag" in skill
    assert "CODEOWNERS is paramount" in skill
    assert "Never convert a failed reviewer request into an assignee write" in skill


def test_review_panel_requires_complete_paginated_context_and_watermark_noop() -> None:
    """Fresh advice is forbidden unless the full conversation was read."""
    skill = _ascii(REVIEW_SKILL)
    assert "Paginate every list to exhaustion" in skill
    assert "If any required page cannot be read" in skill
    assert "apm-review-advisory:v1" in skill
    assert "conversation_watermark" in skill
    assert "Unchanged context is not a fresh review" in skill
    assert "Ownership consistency gate" in skill
    workflow = _ascii(REVIEW_WORKFLOW)
    assert "Invocation mode is `agentic-workflow` (ORIGIN=`unattended`)" in workflow
    assert "Never assign a user" in workflow
    assert "No accepted, no review" in workflow
    assert "gh api --paginate" in workflow
    assert "closingIssuesReferences" in workflow
    assert "reviewRequests" in workflow


def test_triage_never_assigns_and_requires_full_comment_history() -> None:
    """Triage stays advisory for every invocation, including retriage."""
    skill = _ascii(TRIAGE_SKILL)
    assert "activation_card: on" in skill
    assert "write: on | off" in skill
    assert "`write` defaults to `on`" in skill
    assert "`write: off` returns the filled template only" in skill
    assert "If `write: on`, apply the advisory writes yourself" in skill
    assert "No assignment needed." in skill
    assert "This worker owns advisory writes even when summoned without a" in skill
    assert "The scheduler never comments" in skill
    assert "No ORIGIN or INTENT assigns contributors" in skill
    assert "Cloud Agent" in skill
    assert "Remote Agent" in skill
    assert "complete issue context" in skill
    assert "fail closed if a page cannot be read" in skill
    assert "Same target plus same conversation watermark" in skill
    assert "never contradict CODEOWNERS" in skill
    template = _ascii(TRIAGE_TEMPLATE)
    assert "apm-triage-advisory:v2 target=issue#" in template
    assert '"kind": "apm-triage-advisory"' in template
    workflow = _ascii(TRIAGE_WORKFLOW)
    assert "Invocation mode is `agentic-workflow` (ORIGIN=`unattended`)" in workflow
    assert "Worker emits advisory outputs (not the scheduler)" in workflow
    assert "Never assign contributors" in workflow
    assert "scripts/fetch_queue.py" in workflow
    assert "paginate its complete comment history" in workflow
    assert "unchanged context is a no-op" in workflow
    assert "`triage/requested` is the only request trigger" in workflow
    assert "legacy event alias" not in workflow
    assert "legacy `status/needs-triage` event also works" not in workflow
    triage_sched = _ascii(SCHEDULER_TRIAGE)
    assert "legacy event alias" not in triage_sched
    assert "is the only request trigger" in triage_sched


def test_implementation_harnesses_assign_issue_and_pr_not_reviewer() -> None:
    """Actor-session workers own the issue and PR as assignee only."""
    worker = _ascii(WORKER_CODE)
    assert "Cloud Agent" in worker
    assert "Remote Agent" in worker
    assert "When ORIGIN is `actor-session`" in worker
    assert "assignment is a hard gate" in worker
    assert "public signal of which user is working" in worker
    assert "Do not start implementation while the issue is unassigned" in worker
    assert "Do not steal." in worker
    assert "If ORIGIN is `unattended` or unknown, skip" in worker
    assert "gh issue edit --add-assignee @me" in worker
    assert "gh pr edit --add-assignee @me" in worker
    assert "Do not request that actor as a reviewer" in worker
    assert "autopilot-pr-review-scheduler" in worker
    assert "node scripts/governance/eligibility.cjs --help" in worker
    assert "--repo microsoft/apm --issue N --approval-url URL" in worker
    assert "authorizes_implementation: false" in worker
    assert "deleted withdrawals" in worker
    assert "ORIGIN `unattended` never claims to have obtained it" in worker
    delivery = _ascii(SCHEDULER_CODE)
    assert "Workers re-check `scripts/governance/eligibility.cjs`" in delivery
    assert "ORIGIN `unattended` never implements" in delivery
    driver = _ascii(WORKER_PR)
    assert "composed-implementation-review" in driver
    assert "never requests the implementer as a reviewer" in driver
    assert "Never request the implementer as a reviewer" in driver
    assert "emit the activation card" in driver
    assert "No accepted, no review" in driver
    assert "Paginate every list to exhaustion" in driver
    pr_worker = _ascii(WORKER_PR_SKILL)
    assert "activation_card: on" in pr_worker
    assert "write: on | off" in pr_worker
    assert "`write` defaults to `on`" in pr_worker
    assert "`write: off` returns the filled template only" in pr_worker
    assert "If `write: on`, apply the advisory writes yourself" in pr_worker
    assert "The PR review scheduler never comments" in pr_worker


def test_schedulers_own_isolated_fanout_pools_and_aliases_redirect() -> None:
    """Each scheduler has its own default-2 pool; old names are aliases."""
    triage = _ascii(SCHEDULER_TRIAGE)
    delivery = _ascii(SCHEDULER_CODE)
    review = _ascii(SCHEDULER_PR_REVIEW)
    assert "Do not comment, label, close, or assign" in triage
    assert "Workers own those writes" in triage
    assert "FANOUT_LIMIT=2" in triage
    assert "FANOUT_LIMIT=2" in delivery
    assert "FANOUT_LIMIT=2" in review
    assert "concurrency, not queue length" in triage
    assert "concurrency, not queue length" in delivery
    assert "concurrency, not queue length" in review
    assert "Do not truncate the table to FANOUT_LIMIT" in triage
    assert "Do not truncate the table to FANOUT_LIMIT" in delivery
    assert "Do not truncate the table to FANOUT_LIMIT" in review
    assert "when a slot returns, fill it with the next item" in triage
    assert "No assignment needed" in triage
    assert "assign the implementing user" in delivery
    assert "the reviewing session requests the reviewing user as reviewer" in review
    assert "Do not comment, label, close, assign, or request reviewers" in review
    assert "Reviewing sessions own those writes" in review
    assert "Never borrow slots from `autopilot-issue-delivery-scheduler`" in triage
    assert "Never borrow slots from `autopilot-issue-triage-scheduler`" in delivery
    assert "Never borrow slots from `autopilot-issue-triage-scheduler`" in review
    assert "same issue to two slots" in triage
    assert "scripts/fetch_queue.py" in triage
    assert "scripts/triage_state.py" in triage
    assert "Do not invent a second filter" in triage
    assert "run the worker in this thread" in triage
    assert "Compatibility alias" in _ascii(ALIAS_TRIAGE_PANEL)
    assert "autopilot-issue-triage-worker" in _ascii(ALIAS_TRIAGE_PANEL)
    assert "Do not call" in _ascii(TRIAGE_SKILL)
    assert "fetch_queue.py" in _ascii(TRIAGE_SKILL)
    assert "processing.read_reviewed" in triage
    assert "at most two per author" in triage
    assert "oldest first" in triage
    assert "same issue to two slots" in delivery
    assert "#<issue-number> autopilot-issue-delivery-worker <Issue Title>" in delivery
    pool = _ascii(
        ROOT
        / "packages/autopilot-issue-delivery-scheduler/.apm/skills/autopilot-issue-delivery-scheduler/assets/fan-out-pool.md"
    )
    assert "#<issue-number> autopilot-issue-delivery-worker <Issue Title>" in pool
    assert "Do not dispatch an unaccepted issue." in delivery
    assert "Do not dispatch an issue assigned to another user unless named." in delivery
    assert "assignment as a hard gate" in delivery
    assert (
        "`triage/recommended` and legacy `status/triaged` are advisory processing markers and are not authorization."
        in delivery
    )
    assert "`status/accepted`" in delivery
    assert "Type does not matter" in delivery
    assert "Refuse unless the issue already has `status/accepted`" in _ascii(WORKER_CODE)
    assert "Compatibility alias" in _ascii(ALIAS_DELIVERY)
    assert "Compatibility alias" in _ascii(ALIAS_WORKER_ISSUE)
    assert "Compatibility alias" in _ascii(
        ROOT / "packages/autopilot-scheduler-code/.apm/skills/autopilot-scheduler-code/SKILL.md"
    )
    assert "Compatibility alias" in _ascii(
        ROOT / "packages/autopilot-worker-code/.apm/skills/autopilot-worker-code/SKILL.md"
    )
    assert "Compatibility alias" in _ascii(
        ROOT / "packages/autopilot-code-scheduler/.apm/skills/autopilot-code-scheduler/SKILL.md"
    )
    assert "Compatibility alias" in _ascii(
        ROOT / "packages/autopilot-code-worker/.apm/skills/autopilot-code-worker/SKILL.md"
    )
    assert "Compatibility alias" in _ascii(
        ROOT / "packages/autopilot-issue-scheduler/.apm/skills/autopilot-issue-scheduler/SKILL.md"
    )
    assert "Compatibility alias" in _ascii(
        ROOT / "packages/autopilot-issue-worker/.apm/skills/autopilot-issue-worker/SKILL.md"
    )
    assert "same PR to two slots" in review
    assert "`panel-review` is the only request trigger" in review
    assert "No accepted, no review" in review
    assert "Never list all open PRs" in review
    assert "gh pr list --state open --label panel-review" in review
    assert "Empty label queue -> empty table, stop" in review
    assert "oldest first, cap 10" in review
    assert "Compatibility alias" in _ascii(ALIAS_AUTOPILOT)
    assert "Do not implement from this file" in _ascii(ALIAS_AUTOPILOT)
    assert "autopilot-issue-delivery-scheduler" in _ascii(ALIAS_AUTOPILOT)
    assert "Compatibility alias" in _ascii(ALIAS_SHEPHERD)
    assert "Do not implement from this file" in _ascii(ALIAS_SHEPHERD)
    assert "selector `bugs`" in _ascii(ALIAS_SHEPHERD)
    assert "autopilot-pr-review-worker" in _ascii(ALIAS_DRIVER)
    assert "Compatibility alias" in _ascii(ALIAS_SCHEDULER_ISSUES)
    assert "Compatibility alias" in _ascii(ALIAS_SCHEDULER_PRS)
    assert "Compatibility alias" in _ascii(
        ROOT
        / "packages/autopilot-scheduler-issue-triage/.apm/skills/autopilot-scheduler-issue-triage/SKILL.md"
    )
    assert "Compatibility alias" in _ascii(ROOT / "packages/autopilot-worker-pull-request/SKILL.md")
    assert "Compatibility alias" in _ascii(ROOT / "packages/autopilot-pull-request-worker/SKILL.md")
    assert "Compatibility alias" in _ascii(
        ROOT
        / "packages/autopilot-pr-review-triage-scheduler/.apm/skills/autopilot-pr-review-triage-scheduler/SKILL.md"
    )
    assert "Compatibility alias" in _ascii(
        ROOT
        / "packages/autopilot-pr-review-triage-worker/.apm/skills/autopilot-pr-review-triage-worker/SKILL.md"
    )
    assert "Compatibility alias" in _ascii(
        ROOT
        / "packages/autopilot-scheduler-pull-request-review/.apm/skills/autopilot-scheduler-pull-request-review/SKILL.md"
    )
    pr_triage = _ascii(SCHEDULER_PR_TRIAGE)
    assert "FANOUT_LIMIT=2" in pr_triage
    assert "Do not comment, label, close, merge, or assign" in pr_triage
    assert "Workers own those writes" in pr_triage
    assert "concurrency, not queue length" in pr_triage
    assert "Do not truncate the table to FANOUT_LIMIT" in pr_triage
    assert "No assignment needed" in pr_triage
    assert "scripts/fetch_queue.py" in pr_triage
    assert "scripts/triage_state.py" in pr_triage
    assert "A missing linked issue is not a skip" in pr_triage
    assert (
        "Never borrow slots from `autopilot-issue-triage-scheduler`, "
        "`autopilot-issue-delivery-scheduler`, or "
        "`autopilot-pr-review-scheduler`."
    ) in pr_triage
    assert "Do not run `apm-review-panel`" in pr_triage
    assert "same PR to two slots" in pr_triage
    assert "#<pr-number> pr-triage-worker" in _ascii(
        ROOT
        / "packages/autopilot-pr-triage-scheduler/.apm/skills/autopilot-pr-triage-scheduler/assets/fan-out-pool.md"
    )
    worker_pr_triage = _ascii(WORKER_PR_TRIAGE)
    assert "needs-issue" in worker_pr_triage
    assert "Never request reviewers" in worker_pr_triage
    assert "Do not run `apm-review-panel`" in worker_pr_triage
    assert "Never write human decision labels" in worker_pr_triage
    assert "`autopilot-pr-triage-scheduler`" in triage
    assert "`autopilot-pr-triage-scheduler`" in delivery
    assert "`autopilot-pr-triage-scheduler`" in review


def test_agentic_workflow_frontmatter_has_no_assignment_outputs() -> None:
    """Compiled-safe workflow sources must not grow assignee writers."""
    forbidden = {"add-assignee", "add_assignee", "update-issue", "update_issue"}
    for path in (TRIAGE_WORKFLOW, REVIEW_WORKFLOW):
        frontmatter = yaml.safe_load(path.read_text(encoding="ascii").split("---", 2)[1])
        outputs = set(frontmatter["safe-outputs"])
        assert not forbidden & outputs
        assert set(frontmatter["permissions"].values()) == {"read"}


def test_pr_review_lock_keeps_advisory_writers_only() -> None:
    """The compiled PR review lock must not gain assignment or issue mutation."""
    lock_path = ROOT / ".github/workflows/pr-review-panel.lock.yml"
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    forbidden = {
        "assign_milestone",
        "update_issue",
        "create_issue",
        "close_issue",
        "add_assignee",
        "update_comment",
    }
    configs: dict[str, dict] = {}
    for job in lock["jobs"].values():
        for step in job.get("steps", []):
            for key, value in step.get("env", {}).items():
                if key in {"GH_AW_SAFE_OUTPUTS_CONFIG", "GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG"}:
                    configs[key] = json.loads(value)
    assert configs
    for config in configs.values():
        assert not forbidden & set(config.keys())
