"""PR-superseding concurrency must never cancel or queue non-PR runs.

ci.yml, spec-conformance.yml and codeql.yml cancel an in-flight
pull_request run when a newer push lands on the same PR. Any other event
(merge_group, push to main, schedule, dispatch) must land in a unique
per-run group: a shared group would cancel pending runs even with
cancel-in-progress false. docs.yml keeps its ref-keyed group so deploys
queue, and only cancels PR build-only runs.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import load_workflow

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
PR_ONLY_CANCEL = "${{ github.event_name == 'pull_request' }}"
PER_PR_OR_PER_RUN = (
    "${{ github.event.pull_request.number && "
    "format('pr-{0}', github.event.pull_request.number) || "
    "format('run-{0}', github.run_id) }}"
)
SUPERSEDING_WORKFLOWS = {
    "ci.yml": "ci",
    "spec-conformance.yml": "spec-conformance",
    "codeql.yml": "codeql",
}


def _assert_pr_superseding(workflow: dict, prefix: str) -> None:
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), "workflow must declare a concurrency block"
    assert concurrency.get("group") == f"{prefix}-{PER_PR_OR_PER_RUN}", (
        "only pull_request runs may share a group; other events need a per-run group"
    )
    assert concurrency.get("cancel-in-progress") == PR_ONLY_CANCEL, (
        "cancel-in-progress must be limited to pull_request events"
    )


@pytest.mark.parametrize(("filename", "prefix"), SUPERSEDING_WORKFLOWS.items())
def test_pr_workflows_supersede_only_pull_request_runs(filename: str, prefix: str) -> None:
    _assert_pr_superseding(load_workflow(WORKFLOWS / filename), prefix)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cancel-in-progress", True),
        ("cancel-in-progress", "${{ github.event_name != 'merge_group' }}"),
        ("group", "ci-${{ github.event.pull_request.number || github.ref }}"),
        ("group", "ci-${{ github.event.pull_request.number || github.sha }}"),
    ],
)
def test_cancelling_or_grouping_non_pr_runs_fails(key: str, value: object) -> None:
    workflow = deepcopy(load_workflow(WORKFLOWS / "ci.yml"))
    workflow["concurrency"][key] = value

    with pytest.raises(AssertionError):
        _assert_pr_superseding(workflow, "ci")


def test_docs_deploys_queue_and_pr_builds_supersede() -> None:
    concurrency = load_workflow(WORKFLOWS / "docs.yml")["concurrency"]
    assert concurrency == {
        "group": "pages-${{ github.ref }}",
        "cancel-in-progress": PR_ONLY_CANCEL,
    }
