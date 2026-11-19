"""Release validation modes and evidence-gated tag promotion."""

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


def _fetch(release: Sequence[dict[str, object]]):
    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        assert (workflow, sha) == ("build-release.yml", SHA)
        return release

    return fetch


def _jobs(summary: str = "success"):
    def jobs(run_id: str) -> Sequence[Mapping[str, object]]:
        return [
            {"name": "Release Plan", "conclusion": "success"},
            {"name": "Integration Tests Summary", "conclusion": summary},
        ]

    return jobs


def _tag(**extra: str) -> dict[str, str]:
    return {"EVENT_NAME": "push", "REF_TYPE": "tag", "REF_NAME": "v1.2.3", "SHA": SHA, **extra}


@pytest.mark.parametrize(
    ("env", "mode", "publish"),
    [
        ({"EVENT_NAME": "push", "REF_TYPE": "branch", "REF_NAME": "main"}, "push", False),
        ({"EVENT_NAME": "schedule", "REF_TYPE": "branch", "REF_NAME": "main"}, "full", False),
        ({"EVENT_NAME": "repository_dispatch", "REF_TYPE": "branch"}, "full", False),
    ],
)
def test_events_map_to_modes_without_api_calls(
    env: dict[str, str], mode: str, publish: bool
) -> None:
    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        raise AssertionError("no evidence lookup expected")

    plan = release_plan.decide(env, fetch, _jobs())

    assert (plan.mode, plan.publish) == (mode, publish)
    assert plan.run_unit is (mode != "promoted")


@pytest.mark.parametrize("event", ["workflow_dispatch", "pull_request", ""])
def test_unknown_events_fail_closed(event: str) -> None:
    with pytest.raises(ValueError, match="does not handle"):
        release_plan.decide({"EVENT_NAME": event, "REF_TYPE": "tag"}, _fetch([]), _jobs())


@pytest.mark.parametrize("event", ["schedule", "repository_dispatch"])
def test_tag_promotes_on_a_full_run_of_the_same_sha(event: str) -> None:
    plan = release_plan.decide(_tag(), _fetch([_run(event, run_id=7)]), _jobs())

    assert (plan.mode, plan.publish, plan.run_unit, plan.full) == ("promoted", True, False, False)
    assert plan.evidence.endswith("/runs/7")


@pytest.mark.parametrize(
    "release",
    [
        [],
        [_run("schedule", conclusion="failure")],
        [_run("schedule", branch="feature")],
        [_run("schedule", sha="b" * 40)],
        [_run("push")],
        [_run("pull_request")],
        [_run("workflow_dispatch")],
        [_run("merge_group", branch="gh-readonly-queue/main/pr-1")],
        [{**_run("repository_dispatch"), "status": "in_progress"}],
    ],
)
def test_tag_without_evidence_validates_fully_then_publishes(
    release: list[dict[str, object]],
) -> None:
    """A green main push (even with a green merge queue) is not full validation."""
    plan = release_plan.decide(_tag(), _fetch(release), _jobs())

    assert (plan.mode, plan.publish, plan.run_unit, plan.full) == ("full", True, True, True)
    assert plan.evidence.startswith("none:")


def test_failed_evidence_lookup_never_promotes() -> None:
    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        raise RuntimeError("gh api: HTTP 502")

    plan = release_plan.decide(_tag(), fetch, _jobs())

    assert (plan.mode, plan.publish, plan.full) == ("full", True, True)


def test_current_run_is_never_its_own_evidence() -> None:
    plan = release_plan.decide(_tag(RUN_ID="9"), _fetch([_run("schedule", run_id=9)]), _jobs())

    assert plan.mode == "full"


def test_branch_pushes_never_publish_or_look_up_evidence() -> None:
    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        raise AssertionError("no evidence lookup expected")

    env = {"EVENT_NAME": "push", "REF_TYPE": "branch", "REF_NAME": "v1.2.3", "SHA": SHA}
    plan = release_plan.decide(env, fetch, _jobs())

    assert (plan.mode, plan.publish) == ("push", False)


def test_main_writes_outputs_and_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "out"
    summary = tmp_path / "summary"
    monkeypatch.setattr(
        release_plan, "github_fetch", lambda repo: (_fetch([_run("schedule")]), _jobs())
    )

    env = {**_tag(), "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    assert release_plan.main(env) == 0
    assert output.read_text() == "mode=promoted\npublish=true\nrun_unit=false\nfull=false\n"
    assert "/runs/1" in summary.read_text()

    output.unlink()
    summary.unlink()
    monkeypatch.setattr(release_plan, "github_fetch", lambda repo: (_fetch([]), _jobs()))
    assert release_plan.main(env) == 0
    assert output.read_text() == "mode=full\npublish=true\nrun_unit=true\nfull=true\n"
    assert "Evidence: none: no green schedule/repository_dispatch run" in summary.read_text()


@pytest.mark.parametrize("summary", ["skipped", "failure", "cancelled"])
def test_green_run_without_integration_summary_is_not_evidence(summary: str) -> None:
    """A green dispatch that skipped integration (e.g. tag recovery) never promotes."""
    plan = release_plan.decide(_tag(), _fetch([_run("repository_dispatch")]), _jobs(summary))

    assert plan.mode == "full"


def test_run_with_no_integration_summary_job_is_not_evidence() -> None:
    plan = release_plan.decide(_tag(), _fetch([_run("repository_dispatch")]), lambda run_id: [])

    assert plan.mode == "full"
