"""Merge-queue lanes: complete, duration-sharded and required by the gate."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration import conftest as integration_conftest
from tests.workflow_contracts import (
    effective_env,
    load_workflow,
    shell_commands,
    workflow_job,
    workflow_step,
)

pytestmark = pytest.mark.component

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
HERMETIC = "not live and not requires_github_token and not requires_network_integration"
NETWORK = "(requires_github_token or requires_network_integration) and not live"
SHARD_STEP = "Run integration tests (sharded + parallelized)"
NETWORK_STEP = "Run network-dependent integration tests"


def _mq() -> dict:
    return load_workflow(WORKFLOWS / "ci-integration.yml")


def _expected_checks(event: str) -> list[str]:
    gate = workflow_job(load_workflow(WORKFLOWS / "merge-gate.yml"), "gate")
    expression = workflow_step(gate, "Wait for all required checks")["env"]["EXPECTED_CHECKS"]
    merge_group, pull_request = re.findall(r"'([^']*)'", expression)[1:3]
    return (merge_group if event == "merge_group" else pull_request).split(",")


def _assert_merge_queue_lanes(workflow: dict) -> None:
    on = workflow["on"]
    assert "pull_request" not in on and "pull_request_target" not in on
    assert workflow["env"]["HERMETIC_MARK_EXPR"] == HERMETIC
    assert workflow["env"]["NETWORK_MARK_EXPR"] == NETWORK

    shards = workflow_job(workflow, "integration-tests-shard")
    assert shards["needs"] == ["build", "smoke-test", "integration-plan"]
    assert shards["strategy"]["matrix"]["shard"] == (
        "${{ fromJSON(needs.integration-plan.outputs.shards) }}"
    )
    step = workflow_step(shards, SHARD_STEP)
    assert effective_env(workflow, shards, step)["PYTEST_MARK_EXPR"] == HERMETIC
    args = re.findall(r"\$\{\{.*?\}\}|\S+", step["env"]["PYTEST_EXTRA_ARGS"])
    assert args[:2] == ["-p", "scripts.ci_sharding"]
    assert args[args.index("--shard-count") + 1] == "${{ needs.integration-plan.outputs.count }}"
    assert args[args.index("--shard-index") + 1] == "${{ matrix.shard }}"
    assert args[args.index("--dist") + 1] == "loadgroup"
    assert "--cov" in args
    assert "PYTEST_RETRY_FAILED" not in step["env"], "hermetic shards never retry"

    network = workflow_job(workflow, "integration-network")
    network_step = workflow_step(network, NETWORK_STEP)
    assert effective_env(workflow, network, network_step)["PYTEST_MARK_EXPR"] == NETWORK
    assert network_step["env"]["PYTEST_RETRY_FAILED"] == "2"
    assert "--cov" in network_step["env"]["PYTEST_EXTRA_ARGS"].split()

    aggregate = workflow_job(workflow, "integration-tests")
    assert aggregate["name"] == "Integration Tests (Linux)"
    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == {
        "integration-plan",
        "integration-tests-shard",
        "integration-network",
        "scaling-guards",
    }
    results = workflow_step(aggregate, "Aggregate shard results")["run"]
    for job_id in ("integration-plan", "integration-tests-shard", "integration-network"):
        assert f"needs.{job_id}.result" in results

    windows = workflow_job(workflow, "windows-unit-shard")
    assert windows["runs-on"] == "windows-latest"
    assert windows["strategy"]["matrix"]["shard"] == [1, 2, 3]
    (command,) = shell_commands(workflow_step(windows, "Run unit tests (Windows shard)"))
    assert command[command.index("pytest") + 1 : command.index("pytest") + 3] == [
        "tests/unit",
        "tests/test_console.py",
    ]
    assert command[command.index("--shard-count") + 1] == "3"
    windows_aggregate = workflow_job(workflow, "windows-unit")
    assert windows_aggregate["name"] == "Windows Unit Tests"
    assert windows_aggregate["needs"] == ["windows-unit-shard"]
    assert windows_aggregate["if"] == "always()"

    assert workflow_job(workflow, "release-validation")["needs"] == ["build", "smoke-test"]
    build_jobs = [
        job_id
        for job_id, job in workflow["jobs"].items()
        if any("build-binary.sh" in str(s.get("run", "")) for s in job.get("steps", []))
    ]
    assert build_jobs == ["build"], "the merge-queue binary is built exactly once"


def test_merge_queue_lanes_are_complete_and_sharded() -> None:
    _assert_merge_queue_lanes(_mq())


def test_merge_gate_requires_windows_unit_only_in_merge_group() -> None:
    assert "Windows Unit Tests" in _expected_checks("merge_group")
    assert "Windows Unit Tests" not in _expected_checks("pull_request")
    mq = _mq()
    names = {job.get("name") for job in mq["jobs"].values()}
    for check in ("Build (Linux)", "Smoke Test (Linux)", "Integration Tests (Linux)"):
        assert check in _expected_checks("merge_group") and check in names
    assert "Release Validation (Linux)" in names and "Windows Unit Tests" in names


@pytest.mark.parametrize(
    ("job_id", "step_name", "key", "value"),
    [
        ("integration-tests-shard", SHARD_STEP, "PYTEST_MARK_EXPR", "not live"),
        ("integration-tests-shard", SHARD_STEP, "PYTEST_RETRY_FAILED", "2"),
        ("integration-network", NETWORK_STEP, "PYTEST_MARK_EXPR", "requires_github_token"),
        ("integration-network", NETWORK_STEP, "PYTEST_RETRY_FAILED", "0"),
    ],
)
def test_merge_queue_lane_drift_is_rejected(
    job_id: str, step_name: str, key: str, value: str
) -> None:
    workflow = deepcopy(_mq())
    workflow_step(workflow_job(workflow, job_id), step_name)["env"][key] = value

    with pytest.raises(AssertionError):
        _assert_merge_queue_lanes(workflow)


def test_dropping_the_network_lane_from_the_aggregate_is_rejected() -> None:
    workflow = deepcopy(_mq())
    workflow_job(workflow, "integration-tests")["needs"].remove("integration-network")

    with pytest.raises(AssertionError):
        _assert_merge_queue_lanes(workflow)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("test_architecture_owner_rule_mutations.py", True),
        ("test_architecture_lifecycle_lock.py", True),
        ("test_core_smoke.py", False),
        ("test_lockfile_symlink_write_e2e.py", False),
    ],
)
def test_architecture_contracts_are_classified_by_module_name(
    filename: str, expected: bool
) -> None:
    added: list[str] = []
    item = SimpleNamespace(
        path=Path("tests/integration") / filename,
        add_marker=lambda mark: added.append(mark.name),
    )

    integration_conftest.pytest_itemcollected(item)

    assert (added == ["architecture_contract"]) is expected


def test_pr_architecture_contracts_are_path_gated_and_not_required() -> None:
    ci = load_workflow(WORKFLOWS / "ci.yml")
    job = workflow_job(ci, "architecture-contracts")
    assert job["if"] == "github.event_name == 'pull_request'"
    run = workflow_step(job, "Run architecture contracts")
    assert run["if"] == "steps.changes.outputs.run == 'true'"
    tokens = shell_commands(run)[0]
    tokens = tokens[tokens.index("pytest") :]
    assert tokens[tokens.index("-m") + 1] == "architecture_contract"
    assert "tests/integration" in tokens
    names = {job.get("name", "") for job in ci["jobs"].values()}
    required = set(_expected_checks("pull_request")) | set(_expected_checks("merge_group"))
    assert not any(name.startswith("Architecture Contracts") for name in required)
    assert any(name.startswith("Architecture Contracts") for name in names)
