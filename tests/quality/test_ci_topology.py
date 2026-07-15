from __future__ import annotations

import subprocess
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.test_file_inventory import is_test_module_path
from tests.quality.repository_python_inventory import (
    INVENTORY_CALLS,
    PythonModuleFacts,
    tracked_python_inventory,
)
from tests.workflow_contracts import (
    WorkflowNode,
    load_workflow,
    shell_commands,
    workflow_job,
    workflow_step,
)

RATCHET_TEST_SCOPE = "repository"

REPO_ROOT = Path(__file__).resolve().parents[2]
RATCHET_JOB = "test-architecture"
RATCHET_CHECK = "Test Architecture Ratchets"
ALLOW_PROVISIONAL_KEY = "APM_ALLOW_PROVISIONAL_BASELINES"
DRAFT_PROVISIONAL_GUARD = (
    "${{ github.event_name == 'pull_request' && "
    "github.event.pull_request.draft == true && '1' || '0' }}"
)
REPOSITORY_SCOPE = "repository"
FIXTURE_SCOPE = "fixture"
REPOSITORY_ROOT = "tests/quality"
FIXTURE_ROOT = "tests/unit/scripts"
REQUIRED_SHARD_CHECKS = (
    "Build & Test Shard 1 (Linux)",
    "Build & Test Shard 2 (Linux)",
)
REQUIRED_SHARD_ROOTS = (
    "tests/unit",
    "tests/test_console.py",
    "tests/red_team",
)


def _run_steps(job: WorkflowNode) -> list[WorkflowNode]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _pytest_targets(job: WorkflowNode) -> tuple[str, ...]:
    targets: list[str] = []
    for step in _run_steps(job):
        run = step.get("run")
        if not isinstance(run, str) or "pytest" not in run:
            continue
        for tokens in shell_commands(step):
            if "pytest" not in tokens:
                continue
            pytest_index = tokens.index("pytest")
            targets.extend(
                token for token in tokens[pytest_index + 1 :] if token.startswith("tests/")
            )
    return tuple(targets)


def _ratchet_test_inventory(
    python_inventory: dict[str, PythonModuleFacts],
) -> dict[str, str]:
    return {
        path: facts.ratchet_scope
        for path, facts in python_inventory.items()
        if is_test_module_path(path) and facts.ratchet_scope is not None
    }


def _assert_ci_provisional_guard(
    ci: WorkflowNode,
) -> None:
    jobs = ci["jobs"]
    assert isinstance(jobs, dict)
    bindings: list[tuple[str, str, object]] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in _run_steps(job):
            env = step.get("env")
            if isinstance(env, dict) and ALLOW_PROVISIONAL_KEY in env:
                bindings.append(
                    (
                        job_name,
                        str(step.get("name", "")),
                        env[ALLOW_PROVISIONAL_KEY],
                    )
                )
            run = step.get("run")
            if isinstance(run, str):
                assert "--allow-provisional" not in run
                assert f"{ALLOW_PROVISIONAL_KEY}=1" not in run

    expected = [
        (
            RATCHET_JOB,
            "Run ratchet contract tests",
            DRAFT_PROVISIONAL_GUARD,
        )
    ]
    assert bindings == expected


def _assert_ci_test_targets(
    ci: WorkflowNode,
    inventory: dict[str, str],
) -> None:
    repository_modules = {path for path, scope in inventory.items() if scope == REPOSITORY_SCOPE}
    fixture_modules = {path for path, scope in inventory.items() if scope == FIXTURE_SCOPE}
    ratchet = workflow_job(ci, RATCHET_JOB)
    expected_ratchet_targets = Counter({path: 1 for path in repository_modules | fixture_modules})
    assert Counter(_pytest_targets(ratchet)) == expected_ratchet_targets

    required_shard = workflow_job(ci, "build-and-test-shard")
    assert _pytest_targets(required_shard) == REQUIRED_SHARD_ROOTS


def _assert_topology(root: Path, inventory: dict[str, str]) -> None:
    repository_modules = {path for path, scope in inventory.items() if scope == REPOSITORY_SCOPE}
    fixture_modules = {path for path, scope in inventory.items() if scope == FIXTURE_SCOPE}
    unknown_scopes = {
        path: scope
        for path, scope in inventory.items()
        if scope not in {REPOSITORY_SCOPE, FIXTURE_SCOPE}
    }
    assert unknown_scopes == {}
    assert repository_modules
    assert all(path.startswith(f"{REPOSITORY_ROOT}/") for path in repository_modules), (
        f"repository-state ratchet tests must live under {REPOSITORY_ROOT}: "
        f"{sorted(repository_modules)}"
    )
    assert fixture_modules
    assert all(path.startswith(f"{FIXTURE_ROOT}/") for path in fixture_modules), (
        f"fixture-only ratchet tests must live under {FIXTURE_ROOT}: {sorted(fixture_modules)}"
    )

    workflows = root / ".github" / "workflows"
    ci = load_workflow(workflows / "ci.yml")
    ratchet = workflow_job(ci, RATCHET_JOB)
    assert ratchet["name"] == RATCHET_CHECK
    _assert_ci_test_targets(ci, inventory)

    merge_gate = load_workflow(workflows / "merge-gate.yml")
    gate = workflow_job(merge_gate, "gate")
    wait_step = workflow_step(gate, "Wait for all required checks")
    expected_checks = wait_step["env"]["EXPECTED_CHECKS"]
    assert isinstance(expected_checks, str)
    assert RATCHET_CHECK not in expected_checks
    for required_check in REQUIRED_SHARD_CHECKS:
        assert required_check in expected_checks

    ratchet_callers: list[tuple[str, str]] = []
    for workflow_path in sorted(workflows.glob("*.yml")):
        workflow = load_workflow(workflow_path)
        workflow_jobs = workflow.get("jobs")
        if not isinstance(workflow_jobs, dict):
            continue
        for job_name, job in workflow_jobs.items():
            if not isinstance(job, dict):
                continue
            if repository_modules.intersection(_pytest_targets(job)):
                ratchet_callers.append((workflow_path.name, job_name))
    assert ratchet_callers == [("ci.yml", RATCHET_JOB)]


@pytest.fixture(scope="module")
def repository_inventory(
    repository_python_inventory: dict[str, PythonModuleFacts],
) -> dict[str, str]:
    return _ratchet_test_inventory(repository_python_inventory)


def test_test_architecture_ratchets_remain_non_required(
    repository_inventory: dict[str, str],
) -> None:
    _assert_topology(REPO_ROOT, repository_inventory)


def test_repository_ratchet_inventory_is_collected_once(
    repository_inventory: dict[str, str],
) -> None:
    assert repository_inventory
    assert INVENTORY_CALLS[REPO_ROOT.resolve()] == 1


def test_provisional_ci_opt_in_is_draft_guarded() -> None:
    ci = load_workflow(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    _assert_ci_provisional_guard(ci)


def test_relocated_repository_contract_fails_topology(tmp_path: Path) -> None:
    source = REPO_ROOT / "tests" / "quality" / "test_quality_baselines.py"
    relocated = tmp_path / "tests" / "unit" / "example_test.py"
    relocated.parent.mkdir(parents=True)
    relocated.write_text(
        source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "tests"], check=True)
    temp_inventory = _ratchet_test_inventory(tracked_python_inventory(tmp_path))

    with pytest.raises(
        AssertionError,
        match="repository-state ratchet tests must live under tests/quality",
    ):
        _assert_topology(
            tmp_path,
            temp_inventory,
        )


def test_relocated_fixture_contract_fails_topology(tmp_path: Path) -> None:
    source = REPO_ROOT / "tests" / "unit" / "scripts" / "test_check_test_assertions.py"
    relocated = tmp_path / "tests" / "quality" / "example_test.py"
    relocated.parent.mkdir(parents=True)
    relocated.write_text(
        source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    repository_contract = tmp_path / "tests" / "quality" / "test_repository.py"
    repository_contract.write_text(
        'RATCHET_TEST_SCOPE = "repository"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "tests"], check=True)
    temp_inventory = _ratchet_test_inventory(tracked_python_inventory(tmp_path))

    with pytest.raises(
        AssertionError,
        match="fixture-only ratchet tests must live under tests/unit/scripts",
    ):
        _assert_topology(
            tmp_path,
            temp_inventory,
        )


@pytest.fixture
def provisional_ci() -> WorkflowNode:
    return deepcopy(load_workflow(REPO_ROOT / ".github" / "workflows" / "ci.yml"))


def _ratchet_step(ci: WorkflowNode) -> WorkflowNode:
    return workflow_step(
        workflow_job(ci, RATCHET_JOB),
        "Run ratchet contract tests",
    )


def test_unconditional_provisional_opt_in_fails(
    provisional_ci: WorkflowNode,
) -> None:
    _ratchet_step(provisional_ci)["env"][ALLOW_PROVISIONAL_KEY] = "1"

    with pytest.raises(AssertionError):
        _assert_ci_provisional_guard(provisional_ci)


def test_required_shard_provisional_opt_in_fails(
    provisional_ci: WorkflowNode,
) -> None:
    shard_step = _run_steps(workflow_job(provisional_ci, "build-and-test-shard"))[0]
    shard_step["env"] = {ALLOW_PROVISIONAL_KEY: DRAFT_PROVISIONAL_GUARD}

    with pytest.raises(AssertionError):
        _assert_ci_provisional_guard(provisional_ci)


def test_non_draft_provisional_condition_fails(
    provisional_ci: WorkflowNode,
) -> None:
    _ratchet_step(provisional_ci)["env"][ALLOW_PROVISIONAL_KEY] = (
        "${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.draft == false && '1' || '0' }}"
    )

    with pytest.raises(AssertionError):
        _assert_ci_provisional_guard(provisional_ci)


def test_merge_group_provisional_acceptance_fails(
    provisional_ci: WorkflowNode,
) -> None:
    _ratchet_step(provisional_ci)["env"][ALLOW_PROVISIONAL_KEY] = (
        "${{ github.event_name == 'merge_group' && '1' || '0' }}"
    )

    with pytest.raises(AssertionError):
        _assert_ci_provisional_guard(provisional_ci)


def test_duplicate_ratchet_target_fails(
    provisional_ci: WorkflowNode,
    repository_inventory: dict[str, str],
) -> None:
    step = _ratchet_step(provisional_ci)
    target = "tests/quality/test_quality_baselines.py"
    step["run"] = step["run"].replace(target, f"{target} {target}")

    with pytest.raises(AssertionError):
        _assert_ci_test_targets(
            provisional_ci,
            repository_inventory,
        )
