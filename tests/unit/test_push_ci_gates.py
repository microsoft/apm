"""Push artifacts and the PR conformance signal require every expected producer."""

from pathlib import Path

import pytest

from scripts.ci_selections import SELECTIONS
from tests.workflow_contracts import load_workflow, run_bash, workflow_step

pytestmark = pytest.mark.component
WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
BRANCH_PUSH = "github.event_name == 'push' && github.ref_type == 'branch'"


@pytest.mark.parametrize("lane", ["windows", "spec"])
def test_missing_or_unsuccessful_producer_blocks_gate(lane: str) -> None:
    """Execute the actual gate command, including an empty result in each position."""
    if lane == "windows":
        jobs = load_workflow(WORKFLOWS / "build-release.yml")["jobs"]
        job = jobs["build-windows-push"]
        gate = workflow_step(job, "Require Windows push tests")
        # A failed check must stop the single artifact-producing path before checkout.
        assert job["steps"][0] is gate
        assert gate["if"] == "matrix.platform == 'windows' && " + BRANCH_PUSH
        assert job["if"] == "always() && " + BRANCH_PUSH
        assert jobs["build-windows"]["if"] == "needs.release-plan.outputs.mode != 'push'"
        assert job["steps"] == jobs["build-windows"]["steps"]
        expected = [
            "release-plan",
            "windows-installer",
            "windows-unit-1",
            "windows-unit-2",
            "windows-unit-3",
        ]
        key, env = "PUSH_RESULTS", job["env"]
        prefix = ["${{ needs.release-plan.outputs.mode }}"]
        values = ["push"]
    else:
        jobs = load_workflow(WORKFLOWS / "spec-conformance.yml")["jobs"]
        job = jobs["spec-conformance"]
        gate = workflow_step(job, "Require statement and all execution shards")
        assert job["if"] == "always()" and "if" not in gate
        expected = ["statement", "spec-1", "spec-2", "spec-3"]
        key, env = "RESULTS", gate["env"]
        prefix, values = [], []
    assert job["needs"] == expected
    assert env[key].split(":") == prefix + ["${{ needs." + dep + ".result }}" for dep in expected]
    for node in [job, gate, *(jobs[dep] for dep in expected)]:
        assert not node.get("continue-on-error")
    values.extend(["success"] * len(expected))

    def accepts(parts: list[str]) -> bool:
        return run_bash(gate["run"], env={key: ":".join(parts)}).returncode == 0

    assert accepts(values)
    for i in range(len(values)):
        for result in ("failure", "cancelled", "skipped", ""):
            assert not accepts([*values[:i], result, *values[i + 1 :]])


def test_push_shards_execute_the_original_suites() -> None:
    """Neither missing matrix entries nor collection-only jobs can supply execution."""
    release = load_workflow(WORKFLOWS / "build-release.yml")["jobs"]
    installer = workflow_step(release["windows-installer"], "Test install.ps1 end-to-end (Windows)")
    original = workflow_step(release["build-windows"], installer["name"])
    assert release["windows-installer"]["if"] == BRANCH_PUSH
    assert installer["run"] == original["run"] and installer["env"] == original["env"]
    spec = load_workflow(WORKFLOWS / "spec-conformance.yml")["jobs"]
    for jobs, group, count, name, selection, seed in (
        (
            release,
            "windows-unit",
            3,
            "Run unit tests (Windows diagnostics)",
            "tests/unit tests/test_console.py",
            "windows-unit",
        ),
        (
            spec,
            "spec",
            3,
            "Execute conformance tests",
            "tests/spec_conformance",
            "spec-conformance",
        ),
    ):
        for i in range(1, count + 1):
            job = jobs[f"{group}-{i}"]
            step = workflow_step(job, name)
            run = step["run"]
            if group == "windows-unit":
                # The owner holds the selection; expand it to check what runs.
                owner = "python -m scripts.ci_selections windows-unit"
                assert run.count(owner) == 1
                run = run.replace(owner, "python -m pytest " + " ".join(SELECTIONS[group]))
            assert job["env"]["SHARD"] == str(i)
            assert "if" not in step and not step.get("continue-on-error")
            assert f"python -m pytest {selection} " in run
            assert "--collect-only" not in run
            assert f"--shard-count {count} --shard-index" in run
            assert f"--shard-durations .github/ci-durations/{seed}.json" in run
            if group == "windows-unit":
                assert job["if"] == BRANCH_PUSH and job["runs-on"] == "windows-latest"
                assert step["timeout-minutes"] == 60
                assert "-n 4 --dist worksteal" in run
                assert "-vv --tb=short --show-capture=no --no-showlocals" in run
