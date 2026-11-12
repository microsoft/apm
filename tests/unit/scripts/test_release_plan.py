"""Release validation modes and fail-closed tag promotion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from scripts import release_plan

pytestmark = pytest.mark.unit

SHA = "a" * 40


def _run(
    event: str,
    *,
    branch: str = "main",
    conclusion: str = "success",
    sha: str = SHA,
    run_id: int = 1,
) -> dict[str, object]:
    return {
        "id": run_id,
        "event": event,
        "head_branch": branch,
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def _fetch(release: Sequence[dict[str, object]], queue: Sequence[dict[str, object]] = ()):
    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        assert sha == SHA
        return {"build-release.yml": release, "ci-integration.yml": queue}[workflow]

    return fetch


def _tag(**extra: str) -> dict[str, str]:
    return {"EVENT_NAME": "push", "REF_TYPE": "tag", "REF_NAME": "v1.2.3", "SHA": SHA, **extra}


@pytest.mark.parametrize(
    ("env", "mode", "publish"),
    [
        ({"EVENT_NAME": "push", "REF_TYPE": "branch", "REF_NAME": "main"}, "push", False),
        ({"EVENT_NAME": "schedule", "REF_TYPE": "branch", "REF_NAME": "main"}, "full", False),
        ({"EVENT_NAME": "repository_dispatch", "REF_TYPE": "branch"}, "full", False),
        (
            {**_tag(), "FULL_VALIDATION_TAG": "v1.2.3"},
            "full",
            True,
        ),
    ],
)
def test_events_map_to_modes_without_api_calls(
    env: dict[str, str], mode: str, publish: bool
) -> None:
    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        raise AssertionError("no evidence lookup expected")

    plan = release_plan.decide(env, fetch)

    assert (plan.mode, plan.publish) == (mode, publish)
    assert plan.run_unit is (mode != "promoted")


@pytest.mark.parametrize("event", ["workflow_dispatch", "pull_request", ""])
def test_unknown_events_fail_closed(event: str) -> None:
    with pytest.raises(ValueError, match="does not handle"):
        release_plan.decide({"EVENT_NAME": event, "REF_TYPE": "tag"}, _fetch([]))


def test_override_names_one_exact_tag() -> None:
    with pytest.raises(release_plan.MissingEvidence):
        release_plan.decide({**_tag(), "FULL_VALIDATION_TAG": "v1.2.4"}, _fetch([]))
    with pytest.raises(release_plan.MissingEvidence):
        release_plan.decide({**_tag(REF_NAME=""), "FULL_VALIDATION_TAG": ""}, _fetch([]))


@pytest.mark.parametrize("event", ["schedule", "repository_dispatch"])
def test_tag_promotes_on_a_full_run_of_the_same_sha(event: str) -> None:
    plan = release_plan.decide(_tag(), _fetch([_run(event, run_id=7)]))

    assert (plan.mode, plan.publish, plan.run_unit, plan.full) == ("promoted", True, False, False)
    assert plan.evidence.endswith("/runs/7")


def test_tag_promotes_on_push_plus_merge_queue_evidence() -> None:
    plan = release_plan.decide(
        _tag(),
        _fetch([_run("push", run_id=3)], [_run("merge_group", branch="gh-readonly", run_id=4)]),
    )

    assert plan.mode == "promoted"
    assert "/runs/3" in plan.evidence and "/runs/4" in plan.evidence


@pytest.mark.parametrize(
    ("release", "queue"),
    [
        ([], []),
        ([_run("schedule", conclusion="failure")], []),
        ([_run("schedule", branch="feature")], []),
        ([_run("schedule", sha="b" * 40)], []),
        ([_run("push")], []),
        ([_run("push")], [_run("merge_group", conclusion="failure")]),
        ([_run("pull_request")], [_run("merge_group")]),
        ([_run("workflow_dispatch")], []),
    ],
)
def test_tag_without_evidence_fails_closed(
    release: list[dict[str, object]], queue: list[dict[str, object]]
) -> None:
    with pytest.raises(release_plan.MissingEvidence, match="no full-validation evidence"):
        release_plan.decide(_tag(), _fetch(release, queue))


def test_current_run_is_never_its_own_evidence() -> None:
    with pytest.raises(release_plan.MissingEvidence):
        release_plan.decide(_tag(RUN_ID="9"), _fetch([_run("schedule", run_id=9)]))


def test_rehearsal_branches_never_publish() -> None:
    full = {"EVENT_NAME": "push", "REF_TYPE": "branch", "REF_NAME": "rehearse"}
    promote = {**full, "REF_NAME": "promote", "SHA": SHA}
    settings = {
        "FULL_VALIDATION_BRANCHES": "rehearse",
        "PROMOTION_BRANCHES": "promote",
        "EVIDENCE_BRANCH": "rehearse",
        "EVIDENCE_EVENTS": "push",
    }

    assert release_plan.decide({**full, **settings}, _fetch([])).mode == "full"
    plan = release_plan.decide({**promote, **settings}, _fetch([_run("push", branch="rehearse")]))
    assert (plan.mode, plan.publish) == ("promoted", False)


def test_main_writes_outputs_and_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "out"
    summary = tmp_path / "summary"
    monkeypatch.setattr(release_plan, "github_fetch", lambda repo: _fetch([_run("schedule")]))

    assert release_plan.main({**_tag(), "GITHUB_OUTPUT": str(output)}) == 0
    assert "mode=promoted\npublish=true\nrun_unit=false\nfull=false\n" in output.read_text()

    monkeypatch.setattr(release_plan, "github_fetch", lambda repo: _fetch([]))
    assert release_plan.main({**_tag(), "GITHUB_STEP_SUMMARY": str(summary)}) == 1
    assert "refused" in summary.read_text()
