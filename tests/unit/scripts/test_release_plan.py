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


def _compare(statuses: Mapping[str, str], calls: list[tuple[str, str]] | None = None):
    def compare(base: str, sha: str) -> str:
        if calls is not None:
            calls.append((base, sha))
        return statuses.get(base, "diverged")

    return compare


def _branches(*names: str):
    return lambda: list(names)


ON_MAIN = (_compare({"main": "behind"}), _branches())


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
    plan = release_plan.decide(_tag(), _fetch([_run(event, run_id=7)]), _jobs(), *ON_MAIN)

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
    plan = release_plan.decide(_tag(), _fetch(release), _jobs(), *ON_MAIN)

    assert (plan.mode, plan.publish, plan.run_unit, plan.full) == ("full", True, True, True)
    assert plan.evidence.startswith("none:")


def test_failed_evidence_lookup_never_promotes() -> None:
    def fetch(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
        raise RuntimeError("gh api: HTTP 502")

    plan = release_plan.decide(_tag(), fetch, _jobs(), *ON_MAIN)

    assert (plan.mode, plan.publish, plan.full) == ("full", True, True)


def test_current_run_is_never_its_own_evidence() -> None:
    plan = release_plan.decide(
        _tag(RUN_ID="9"), _fetch([_run("schedule", run_id=9)]), _jobs(), *ON_MAIN
    )

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
        release_plan, "github_fetch", lambda repo: (_fetch([_run("schedule")]), _jobs(), *ON_MAIN)
    )

    env = {**_tag(), "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    assert release_plan.main(env) == 0
    assert output.read_text() == "mode=promoted\npublish=true\nrun_unit=false\nfull=false\n"
    assert "/runs/1" in summary.read_text()

    output.unlink()
    summary.unlink()
    monkeypatch.setattr(release_plan, "github_fetch", lambda repo: (_fetch([]), _jobs(), *ON_MAIN))
    assert release_plan.main(env) == 0
    assert output.read_text() == "mode=full\npublish=true\nrun_unit=true\nfull=true\n"
    assert "Evidence: none: no green schedule/repository_dispatch run" in summary.read_text()


@pytest.mark.parametrize("summary", ["skipped", "failure", "cancelled"])
def test_green_run_without_integration_summary_is_not_evidence(summary: str) -> None:
    """A green dispatch that skipped integration (e.g. tag recovery) never promotes."""
    plan = release_plan.decide(
        _tag(), _fetch([_run("repository_dispatch")]), _jobs(summary), *ON_MAIN
    )

    assert plan.mode == "full"


def test_run_with_no_integration_summary_job_is_not_evidence() -> None:
    plan = release_plan.decide(
        _tag(), _fetch([_run("repository_dispatch")]), lambda run_id: [], *ON_MAIN
    )

    assert plan.mode == "full"


def _no_evidence_lookup(workflow: str, sha: str) -> Sequence[Mapping[str, object]]:
    raise AssertionError("no evidence lookup expected")


@pytest.mark.parametrize(
    ("statuses", "branches", "evidence", "mode"),
    [
        ({"main": "identical"}, (), [_run("schedule")], "promoted"),
        ({"main": "behind"}, (), [_run("schedule")], "promoted"),
        ({"main": "behind"}, (), [], "full"),
        ({"main": "diverged", "release/0.38": "behind"}, ("release/0.38",), [], "full"),
        (
            {"main": "ahead", "release/0.37": "diverged", "release/0.38": "identical"},
            ("release/0.37", "release/0.38"),
            [_run("repository_dispatch")],
            "promoted",
        ),
    ],
)
def test_tracked_tag_is_planned_as_today(
    statuses: dict[str, str], branches: tuple[str, ...], evidence: list, mode: str
) -> None:
    calls: list[tuple[str, str]] = []
    plan = release_plan.decide(
        _tag(), _fetch(evidence), _jobs(), _compare(statuses, calls), _branches(*branches)
    )

    assert (plan.mode, plan.publish) == (mode, True)
    assert calls[0] == ("main", SHA)
    assert all(sha == SHA for _, sha in calls)


@pytest.mark.parametrize(
    ("statuses", "branches"),
    [
        ({"main": "ahead"}, ()),
        ({"main": "diverged"}, ()),
        ({"main": "diverged", "release/0.38": "ahead"}, ("release/0.38",)),
        ({"main": "ahead", "release/0.37": "diverged"}, ("release/0.37",)),
        ({"main": ""}, ()),
    ],
)
def test_untracked_tag_is_refused_before_evidence(
    statuses: dict[str, str], branches: tuple[str, ...]
) -> None:
    with pytest.raises(release_plan.UntrackedTagError) as refused:
        release_plan.decide(
            _tag(), _no_evidence_lookup, _jobs(), _compare(statuses), _branches(*branches)
        )

    message = str(refused.value)
    assert f"Tag v1.2.3 points at {SHA}" in message
    assert "is not on main or a release/* branch" in message
    assert "nothing was built or published" in message


@pytest.mark.parametrize("failing", ["compare", "branches"])
def test_reachability_api_error_fails_closed(failing: str) -> None:
    def compare(base: str, sha: str) -> str:
        if failing == "compare":
            raise RuntimeError("gh api: HTTP 502")
        return "diverged"

    def branches() -> Sequence[str]:
        raise RuntimeError("gh api: HTTP 503")

    with pytest.raises(release_plan.UntrackedTagError) as refused:
        release_plan.decide(_tag(), _no_evidence_lookup, _jobs(), compare, branches)

    message = str(refused.value)
    assert f"Could not confirm that tag v1.2.3 ({SHA}) is on main or a release/* branch" in message
    assert "HTTP 50" in message
    assert "re-run this job" in message


def test_tag_without_a_reachability_check_is_refused() -> None:
    with pytest.raises(release_plan.UntrackedTagError, match="no reachability check"):
        release_plan.decide(_tag(), _no_evidence_lookup, _jobs())


@pytest.mark.parametrize(
    "env",
    [
        {"EVENT_NAME": "push", "REF_TYPE": "branch", "REF_NAME": "main", "SHA": SHA},
        {"EVENT_NAME": "schedule", "REF_TYPE": "branch", "REF_NAME": "main", "SHA": SHA},
        {"EVENT_NAME": "repository_dispatch", "REF_TYPE": "branch", "SHA": SHA},
    ],
)
def test_non_tag_events_never_call_the_compare_api(env: dict[str, str]) -> None:
    def compare(base: str, sha: str) -> str:
        raise AssertionError("no compare call expected")

    def branches() -> Sequence[str]:
        raise AssertionError("no branch listing expected")

    plan = release_plan.decide(env, _no_evidence_lookup, _jobs(), compare, branches)

    assert plan.publish is False


def test_main_refuses_an_untracked_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out"
    summary = tmp_path / "summary"
    monkeypatch.setattr(
        release_plan,
        "github_fetch",
        lambda repo: (_no_evidence_lookup, _jobs(), _compare({"main": "ahead"}), _branches()),
    )

    env = {**_tag(), "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    assert release_plan.main(env) == 1
    assert not output.exists()
    assert "::error title=Release tag refused::Tag v1.2.3 points at" in capsys.readouterr().out
    assert "Refused: Tag v1.2.3" in summary.read_text()


def test_github_fetch_compares_branches_and_lists_release_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def gh_json(path: str) -> object:
        paths.append(path)
        if "/matching-refs/" in path:
            return [{"ref": "refs/heads/release/0.37"}, {"ref": "refs/heads/release/0.38"}]
        return {"status": "behind"}

    monkeypatch.setattr(release_plan, "_gh_json", gh_json)
    _fetch_runs, _fetch_jobs, compare, branches = release_plan.github_fetch("o/r")

    assert branches() == ["release/0.37", "release/0.38"]
    assert compare("release/0.38", SHA) == "behind"
    assert paths == [
        "repos/o/r/git/matching-refs/heads/release/",
        f"repos/o/r/compare/release%2F0.38...{SHA}?per_page=1",
    ]
