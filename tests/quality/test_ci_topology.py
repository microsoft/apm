from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from itertools import pairwise, product
from pathlib import Path

import pytest

from scripts import ci_sharding
from scripts.test_file_inventory import is_test_module_path
from tests.quality.repository_python_inventory import (
    INVENTORY_CALLS,
    PythonModuleFacts,
    tracked_python_inventory,
)
from tests.workflow_contracts import (
    WorkflowNode,
    load_workflow,
    run_bash,
    shell_commands,
    workflow_job,
    workflow_step,
)

RATCHET_TEST_SCOPE = "repository"

REPO_ROOT = Path(__file__).resolve().parents[2]
RATCHET_JOB = "test-architecture"
RATCHET_CHECK = "Test Architecture Ratchets"
RATCHET_CONTRACT_JOB = "test-architecture-contracts"
RATCHET_SCALING_JOB = "test-architecture-scaling"
RATCHET_EXECUTION_JOBS = (RATCHET_CONTRACT_JOB, RATCHET_SCALING_JOB)
ARCHITECTURE_SCALING_TARGET = (
    "tests/perf/test_architecture_linter_scaling.py::"
    "test_ci_real_catalog_work_scales_below_fifteen_x_after_fixed_cost_subtraction"
)
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
    "tests/fixtures/policy",
)

# Lifecycle Smoke (Linux): the smallest stable hermetic subset of the real
# Consume/Produce/Govern lifecycle contracts, promoted to a PR-time required
# check. See ci.yml's lifecycle-smoke job comment for full rationale.
#
# The full marker family stays merge-group covered while the required release
# expression excludes explicitly merge-group-only contracts to remain bounded.
LIFECYCLE_SMOKE_JOB = "lifecycle-smoke"
# Sharded jobs write their shard's test files here first (#4120).
SHARD_SELECT_STEP = "Select shard test files"
SHARD_FILE_LIST = "shard-files.txt"
LIFECYCLE_SMOKE_SHARD_JOBS = (
    "lifecycle-smoke-shard-1",
    "lifecycle-smoke-shard-2",
    "lifecycle-smoke-shard-3",
    "lifecycle-smoke-shard-4",
)
LIFECYCLE_SMOKE_SHARDS = tuple(range(1, len(LIFECYCLE_SMOKE_SHARD_JOBS) + 1))
# The aggregate also requires the cross-shard GATE cell coverage check (#4031).
LIFECYCLE_SMOKE_GATE_COVERAGE_JOB = "gate-cell-coverage"
LIFECYCLE_SMOKE_AGGREGATE_NEEDS = (*LIFECYCLE_SMOKE_SHARD_JOBS, LIFECYCLE_SMOKE_GATE_COVERAGE_JOB)
LIFECYCLE_SMOKE_AGGREGATE_STEP = "Require every lifecycle shard"
LIFECYCLE_SMOKE_DURATIONS = ".github/ci-durations/linux-x86_64.json"
LIFECYCLE_SMOKE_CHECK = "Lifecycle Smoke (Linux)"
LIFECYCLE_SMOKE_RUN_STEP = "Run required lifecycle smoke subset"
LIFECYCLE_SMOKE_TIMEOUT_MINUTES = 10
LIFECYCLE_SMOKE_MARKER = "lifecycle_smoke"
LIFECYCLE_SMOKE_ROOT = "tests/integration"
LIFECYCLE_SMOKE_E2E_ENV = "APM_E2E_TESTS"
LIFECYCLE_SMOKE_E2E_VALUE = "1"
LIFECYCLE_SMOKE_FULL_EXPRESSION = "lifecycle_smoke"
LIFECYCLE_SMOKE_MERGE_GROUP_MARKER = "lifecycle_merge_group"
LIFECYCLE_SMOKE_REQUIRED_EXPRESSION = (
    f"{LIFECYCLE_SMOKE_FULL_EXPRESSION} and not {LIFECYCLE_SMOKE_MERGE_GROUP_MARKER}"
)
MERGE_GROUP_INTEGRATION_WORKFLOW = REPO_ROOT / ".github/workflows/ci-integration.yml"
MERGE_GROUP_INTEGRATION_JOB = "integration-tests-shard"
MERGE_GROUP_INTEGRATION_STEP = "Run integration tests (sharded + parallelized)"
MERGE_GROUP_INTEGRATION_SCRIPT = REPO_ROOT / "scripts/test-integration.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"
FORBIDDEN_CREDENTIAL_ENV = ("GITHUB_APM_PAT", "ADO_APM_PAT", "GITHUB_TOKEN")
FORBIDDEN_NETWORK_ENV = ("APM_RUN_INTEGRATION_TESTS",)
# Covers the `${{ github.token }}` context alias, which does not contain any
# FORBIDDEN_CREDENTIAL_ENV substring but resolves to the same automatic token.
FORBIDDEN_CREDENTIAL_EXPRESSIONS = (*FORBIDDEN_CREDENTIAL_ENV, "github.token")


def _pytest_ini_markers() -> list[str]:
    """The registered marker declarations from
    [tool.pytest.ini_options].markers in pyproject.toml -- the single
    canonical place pytest markers are declared in this repo."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    assert isinstance(markers, list)
    return markers


def _run_steps(job: WorkflowNode) -> list[WorkflowNode]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


RESULT_OUTCOMES = ("success", "failure", "cancelled", "skipped", "", None)


def _result_combinations(count: int) -> list[tuple[str | None, ...]]:
    """Every combination for two results; beyond that, each single fault and each uniform set."""
    if count <= 2:
        return list(product(RESULT_OUTCOMES, repeat=count))
    combinations = [(outcome,) * count for outcome in RESULT_OUTCOMES]
    for index in range(count):
        for outcome in RESULT_OUTCOMES[1:]:
            values: list[str | None] = ["success"] * count
            values[index] = outcome
            combinations.append(tuple(values))
    return combinations


def _assert_success_results(step: WorkflowNode, result_names: tuple[str, ...]) -> None:
    """Execute the real aggregate with failed, skipped and absent results."""
    for values in _result_combinations(len(result_names)):
        results = {
            name: value
            for name, value in zip(result_names, values, strict=True)
            if value is not None
        }
        result = subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": os.environ["PATH"], **results},
        )
        assert (result.returncode == 0) == all(value == "success" for value in values), (
            f"aggregate accepted invalid results: {values!r}"
        )


def _workflow_uv_environment_commands(
    root: Path,
) -> list[tuple[str, str, str, list[str], bool]]:
    """Collect project-environment commands and prior frozen-sync state."""
    commands: list[tuple[str, str, str, list[str], bool]] = []
    workflows_dir = root / ".github" / "workflows"
    workflow_paths = sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")])
    for workflow_path in workflow_paths:
        workflow = load_workflow(workflow_path)
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            frozen_sync_seen = False
            for step in _run_steps(job):
                step_name = str(step.get("name", "<unnamed>"))
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                logical = run.replace("\\\n", " ")
                for line in logical.splitlines():
                    if not any(
                        command in line for command in ("uv sync", "uv run")
                    ) or line.lstrip().startswith("#"):
                        continue
                    try:
                        tokens = shlex.split(line, comments=True, posix=True)
                    except ValueError as exc:
                        location = f"{workflow_path.name}:{job_name}:{step_name}"
                        raise AssertionError(f"{location}: invalid shell line {line!r}") from exc
                    for index in range(len(tokens) - 1):
                        command = tokens[index : index + 2]
                        if command not in (["uv", "sync"], ["uv", "run"]):
                            continue
                        commands.append(
                            (
                                workflow_path.name,
                                str(job_name),
                                step_name,
                                tokens[index:],
                                frozen_sync_seen,
                            )
                        )
                        if command == ["uv", "sync"] and "--frozen" in tokens[index:]:
                            frozen_sync_seen = True
    return commands


def _pytest_targets(job: WorkflowNode) -> tuple[str, ...]:
    targets: list[str] = []
    for step in _run_steps(job):
        run = step.get("run")
        if not isinstance(run, str) or ("pytest" not in run and "scripts.ci_sharding" not in run):
            continue
        for tokens in shell_commands(step):
            if "pytest" in tokens:
                start = tokens.index("pytest") + 1
            elif "scripts.ci_sharding" in tokens and "files" in tokens:
                # A shard file-list emitter: its roots are the job's selection.
                start = tokens.index("files") + 1
            else:
                continue
            targets.extend(token for token in tokens[start:] if token.startswith("tests/"))
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
            RATCHET_CONTRACT_JOB,
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
    _assert_ratchet_split(ci)
    ratchet = workflow_job(ci, RATCHET_CONTRACT_JOB)
    expected_ratchet_targets = Counter({path: 1 for path in repository_modules | fixture_modules})
    assert Counter(_pytest_targets(ratchet)) == expected_ratchet_targets
    assert _pytest_targets(workflow_job(ci, RATCHET_SCALING_JOB)) == (ARCHITECTURE_SCALING_TARGET,)

    required_shard = workflow_job(ci, "build-and-test-shard")
    assert _pytest_targets(required_shard) == REQUIRED_SHARD_ROOTS


def _assert_ratchet_execution(ci: WorkflowNode, job_id: str) -> None:
    """Keep each execution's source, environment, selection and deadline."""
    job = workflow_job(ci, job_id)
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 5
    assert job["permissions"] == {"contents": "read"}
    assert job.get("env", {}) == {}
    assert all(key not in job for key in ("if", "needs", "continue-on-error", "strategy"))
    steps = _run_steps(job)
    assert len(steps) == 5
    assert all("if" not in step and "continue-on-error" not in step for step in steps)
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[0].get("with", {}) == {}, "execute the current checked-out source"
    assert steps[1]["uses"].startswith("actions/setup-python@")
    assert steps[1]["with"] == {"python-version": "${{ env.PYTHON_VERSION }}"}
    assert ci["env"]["PYTHON_VERSION"] == "3.12"
    assert steps[2]["uses"].startswith("astral-sh/setup-uv@")
    assert steps[2]["with"] == {"enable-cache": True}
    assert list(shell_commands(steps[3])) == [["uv", "sync", "--frozen", "--extra", "dev"]]
    assert all(step.get("env", {}) == {} for step in steps[:4])
    prefix = ["uv", "run", "--frozen", "--extra", "dev", "pytest", "-p", "no:cacheprovider", "-q"]
    if job_id == RATCHET_CONTRACT_JOB:
        expected_command = [*prefix, *_pytest_targets(job)]
        expected_env = {ALLOW_PROVISIONAL_KEY: DRAFT_PROVISIONAL_GUARD}
    else:
        assert job_id == RATCHET_SCALING_JOB
        expected_command = [*prefix, "-m", "benchmark", ARCHITECTURE_SCALING_TARGET]
        expected_env = {"PYTEST_PERF": "1"}
    assert list(shell_commands(steps[4])) == [expected_command]
    assert steps[4].get("env", {}) == expected_env


def _assert_ratchet_split(ci: WorkflowNode) -> None:
    """Preserve the original signal while independently bounding both jobs."""
    aggregate = workflow_job(ci, RATCHET_JOB)
    assert aggregate.get("needs") == list(RATCHET_EXECUTION_JOBS), (
        "ratchet contracts and scaling must execute in separate jobs"
    )
    assert aggregate["name"] == RATCHET_CHECK
    assert aggregate["if"] == "always()"
    assert aggregate["permissions"] == {}
    assert "continue-on-error" not in aggregate
    assert len(aggregate["steps"]) == 1
    step = workflow_step(aggregate, "Require both architecture executions")
    assert "if" not in step and "continue-on-error" not in step
    assert step["env"] == {
        "CONTRACT_RESULT": "${{ needs.test-architecture-contracts.result }}",
        "SCALING_RESULT": "${{ needs.test-architecture-scaling.result }}",
    }
    _assert_success_results(step, ("CONTRACT_RESULT", "SCALING_RESULT"))
    for job_id in RATCHET_EXECUTION_JOBS:
        _assert_ratchet_execution(ci, job_id)


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
    assert ratchet_callers == [("ci.yml", RATCHET_CONTRACT_JOB)]


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


def test_ratchet_executions_are_separate_and_bounded() -> None:
    """Both original workloads retain independent five-minute execution caps."""
    _assert_ratchet_split(load_workflow(REPO_ROOT / ".github" / "workflows" / "ci.yml"))


def test_first_party_workflow_uv_environments_are_frozen() -> None:
    """The committed lockfile governs every GitHub Actions environment."""
    commands = _workflow_uv_environment_commands(REPO_ROOT)
    assert commands
    unfrozen_syncs = [
        f"{workflow}:{job}:{step}: {' '.join(tokens)}"
        for workflow, job, step, tokens, _frozen_sync_seen in commands
        if tokens[:2] == ["uv", "sync"] and "--frozen" not in tokens
    ]
    implicit_unfrozen_runs = [
        f"{workflow}:{job}:{step}: {' '.join(tokens)}"
        for workflow, job, step, tokens, frozen_sync_seen in commands
        if tokens[:2] == ["uv", "run"] and "--frozen" not in tokens and not frozen_sync_seen
    ]
    violations = unfrozen_syncs + implicit_unfrozen_runs
    assert violations == [], "unfrozen first-party workflow environments:\n" + (
        "\n".join(violations)
    )


def test_workflow_sync_guard_reports_invalid_shell_line(tmp_path: Path) -> None:
    """Malformed candidate commands identify their workflow location."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "broken.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - name: Broken sync\n"
        "        run: 'uv sync \"unterminated'\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AssertionError,
        match=r"broken\.yml:test:Broken sync: invalid shell line",
    ):
        _workflow_uv_environment_commands(tmp_path)


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
        workflow_job(ci, RATCHET_CONTRACT_JOB),
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


@pytest.mark.parametrize("job_id", RATCHET_EXECUTION_JOBS)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout-minutes", 4),
        ("timeout-minutes", 6),
        ("runs-on", "ubuntu-latest"),
        ("permissions", {"contents": "write"}),
        ("if", "false"),
        ("continue-on-error", True),
    ],
)
def test_ratchet_execution_job_drift_fails(
    provisional_ci: WorkflowNode, job_id: str, field: str, value: object
) -> None:
    workflow_job(provisional_ci, job_id)[field] = value
    with pytest.raises(AssertionError):
        _assert_ratchet_split(provisional_ci)


@pytest.mark.parametrize("job_id", RATCHET_EXECUTION_JOBS)
@pytest.mark.parametrize(
    "mutation", ["source", "python", "dependencies", "environment", "selection"]
)
def test_ratchet_execution_step_drift_fails(
    provisional_ci: WorkflowNode, job_id: str, mutation: str
) -> None:
    steps = _run_steps(workflow_job(provisional_ci, job_id))
    if mutation == "source":
        steps[0]["with"] = {"ref": "main"}
    elif mutation == "python":
        steps[1]["with"]["python-version"] = "3.11"
    elif mutation == "dependencies":
        steps[3]["run"] = "uv sync --extra dev"
    elif mutation == "environment":
        steps[4]["env"] = {}
    else:
        steps[4]["run"] += " -k false"
    with pytest.raises(AssertionError):
        _assert_ratchet_split(provisional_ci)


@pytest.mark.parametrize("job_id", RATCHET_EXECUTION_JOBS)
@pytest.mark.parametrize("field", ["if", "continue-on-error"])
def test_ratchet_execution_step_cannot_skip_or_suppress(
    provisional_ci: WorkflowNode, job_id: str, field: str
) -> None:
    _run_steps(workflow_job(provisional_ci, job_id))[-1][field] = "false" if field == "if" else True
    with pytest.raises(AssertionError):
        _assert_ratchet_split(provisional_ci)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("needs", [RATCHET_CONTRACT_JOB]),
        ("needs", [RATCHET_SCALING_JOB]),
        ("if", "success()"),
        ("name", "Renamed ratchet signal"),
        ("continue-on-error", True),
    ],
)
def test_ratchet_aggregate_topology_drift_fails(
    provisional_ci: WorkflowNode, field: str, value: object
) -> None:
    workflow_job(provisional_ci, RATCHET_JOB)[field] = value
    with pytest.raises(AssertionError):
        _assert_ratchet_split(provisional_ci)


@pytest.mark.parametrize("result_name", ["CONTRACT_RESULT", "SCALING_RESULT"])
def test_ratchet_aggregate_cannot_ignore_either_execution(
    provisional_ci: WorkflowNode, result_name: str
) -> None:
    step = workflow_step(
        workflow_job(provisional_ci, RATCHET_JOB), "Require both architecture executions"
    )
    step["run"] = step["run"].replace(f'[ "${result_name}" != success ]', "false")
    with pytest.raises(AssertionError, match="invalid results"):
        _assert_ratchet_split(provisional_ci)


def test_ratchet_aggregate_cannot_suppress_failure(provisional_ci: WorkflowNode) -> None:
    step = workflow_step(
        workflow_job(provisional_ci, RATCHET_JOB), "Require both architecture executions"
    )
    step["run"] = step["run"].replace("exit 1", ":")
    with pytest.raises(AssertionError, match="invalid results"):
        _assert_ratchet_split(provisional_ci)


# Windows units on pull requests (#4115, PERF-H2). Every required Windows
# gate shard runs its slice of the Windows unit suite through the
# ci-validation-topology owner (scripts/ci_selections.py), the same selection
# main push runs, plus its slice of the windows_compat integration family.
# No Windows job may spell a unit-suite run of its own.
WINDOWS_GATE_SHARD_JOBS = (
    "windows-compat-gate-1",
    "windows-compat-gate-2",
    "windows-compat-gate-3",
)
WINDOWS_UNIT_STEP = "Run Windows unit suite"
WINDOWS_COMPAT_STEP = "Run cross-platform contract family"
WINDOWS_FULL_UNIT_STEP = "Run unit tests (Windows diagnostics)"
WINDOWS_UNIT_OWNER = ("python", "-m", "scripts.ci_selections", "windows-unit")
WINDOWS_UNIT_ROOTS = ("tests/unit", "tests/test_console.py")
_PYTEST_VALUE_FLAGS = ("-p", "-m", "-n", "--dist", "--shard-count", "--shard-index")


def _owner_tail(command: Sequence[str]) -> list[str] | None:
    """Arguments after the owner invocation, or None when the command bypasses it."""
    width = len(WINDOWS_UNIT_OWNER)
    for index in range(len(command) - width + 1):
        if tuple(command[index : index + width]) == WINDOWS_UNIT_OWNER:
            return list(command[index + width :])
    return None


def _pytest_roots(command: Sequence[str]) -> list[str]:
    """Positional test paths of a pytest command (flag values excluded)."""
    if not any(token.endswith("pytest") for token in command):
        return []
    tokens = list(command)
    start = max(i for i, token in enumerate(tokens) if token.endswith("pytest")) + 1
    roots: list[str] = []
    skip = False
    for token in tokens[start:]:
        if skip:
            skip = False
        elif token in _PYTEST_VALUE_FLAGS or token.startswith("--shard-durations"):
            skip = "=" not in token
        elif not token.startswith("-"):
            roots.append(token)
    return roots


def _is_windows_job(job: WorkflowNode) -> bool:
    include = job.get("strategy", {}).get("matrix", {}).get("include", [])
    return "windows" in str(job.get("runs-on", "")) or any(
        isinstance(row, dict) and "windows" in str(row.get("os", "")) for row in include
    )


def _test_commands(step: WorkflowNode) -> list[list[str]]:
    """Tokenize the step's pytest and owner lines; other shell lines may be PowerShell."""
    commands: list[list[str]] = []
    for line in str(step.get("run", "")).replace("\\\n", " ").splitlines():
        if "pytest" not in line and "scripts.ci_selections" not in line:
            continue
        try:
            commands.append(shlex.split(line, comments=True, posix=True))
        except ValueError:
            commands.append(line.split())
    return commands


def _windows_unit_runs(job: WorkflowNode) -> list[tuple[WorkflowNode, list[str]]]:
    """Every step command that runs the unit suite on Windows, owner-routed or not."""
    runs: list[tuple[WorkflowNode, list[str]]] = []
    for step in _run_steps(job):
        if "run" not in step or "matrix.platform != 'windows'" in str(step.get("if", "")):
            continue
        for command in _test_commands(step):
            roots = _pytest_roots(command)
            if _owner_tail(command) is not None or set(roots) & set(WINDOWS_UNIT_ROOTS):
                runs.append((step, command))
    return runs


def _assert_windows_units_route_through_owner(workflow: WorkflowNode, label: str) -> None:
    for job_id, job in workflow["jobs"].items():
        if not _is_windows_job(job):
            continue
        for _step, command in _windows_unit_runs(job):
            assert _owner_tail(command) is not None, (
                f"{label} job {job_id!r} runs Windows units without scripts/ci_selections.py: "
                f"{shlex.join(command)}"
            )


def _assert_owner_shard_step(job: WorkflowNode, label: str, shard_count: int) -> WorkflowNode:
    runs = _windows_unit_runs(job)
    assert len(runs) == 1, f"{label} must run the Windows unit suite exactly once"
    step, command = runs[0]
    assert _owner_tail(command) == [
        "--shard-count",
        str(shard_count),
        "--shard-index",
        "$env:SHARD",
    ], f"{label} must run its Windows unit shard through scripts/ci_selections.py"
    assert "if" not in step and "continue-on-error" not in step, label
    return step


def _assert_pr_windows_units(ci: WorkflowNode, release: WorkflowNode) -> None:
    from scripts.ci_selections import SELECTIONS

    owner = SELECTIONS["windows-unit"]
    assert _pytest_roots(["pytest", *owner]) == list(WINDOWS_UNIT_ROOTS)
    assert "-m" not in owner and "-k" not in owner, "Windows units must not be filtered"
    assert ("-n", "4") in set(pairwise(owner))

    for workflow, label in ((ci, "ci.yml"), (release, "build-release.yml")):
        _assert_windows_units_route_through_owner(workflow, label)

    push_jobs = sorted(job_id for job_id in release["jobs"] if job_id.startswith("windows-unit-"))
    assert push_jobs, "main push must keep its Windows unit shards"
    for job_id in push_jobs:
        _assert_owner_shard_step(release["jobs"][job_id], job_id, len(push_jobs))
    full_step = workflow_step(workflow_job(release, "build-windows"), WINDOWS_FULL_UNIT_STEP)
    assert [_owner_tail(command) for command in _test_commands(full_step)] == [[]]

    count = len(WINDOWS_GATE_SHARD_JOBS)
    for index, job_id in enumerate(WINDOWS_GATE_SHARD_JOBS, 1):
        job = workflow_job(ci, job_id)
        assert job["env"] == {"SHARD": str(index)}
        unit_step = _assert_owner_shard_step(job, job_id, count)
        assert unit_step.get("name") == WINDOWS_UNIT_STEP
        compat = workflow_step(job, WINDOWS_COMPAT_STEP)
        # Integration contracts still run when a unit fails, for full signal.
        assert compat.get("if") == "${{ !cancelled() }}"
        assert "continue-on-error" not in compat
        [command] = [c for c in _test_commands(compat) if _pytest_roots(c)]
        assert ("-m", "windows_compat") in set(pairwise(command))
        assert _pytest_roots(command) == [f"@{SHARD_FILE_LIST}"]
        select = shard_file_list_args(workflow_step(job, SHARD_SELECT_STEP))
        pairs = set(pairwise(select))
        assert ("-m", "windows_compat") in pairs
        assert ("--shard-count", str(count)) in pairs
        assert ("--shard-index", "$env:SHARD") in pairs
        assert _shard_list_roots(select) == ["tests/integration"]


@pytest.fixture
def provisional_release() -> WorkflowNode:
    return deepcopy(load_workflow(RELEASE_WORKFLOW))


def test_pr_windows_gate_runs_every_windows_unit_node() -> None:
    _assert_pr_windows_units(
        load_workflow(REPO_ROOT / ".github/workflows/ci.yml"),
        load_workflow(RELEASE_WORKFLOW),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "marker-only-units",
        "drop-unit-step",
        "unit-shard-count",
        "skip-unit-step",
        "tolerate-unit-failure",
        "skip-compat-step",
        "unit-root-in-compat",
        "compat-root-dropped",
        "push-bypasses-owner",
        "full-run-bypasses-owner",
        "owner-marker-filter",
    ],
)
def test_pr_windows_unit_drift_fails(
    provisional_ci: WorkflowNode,
    provisional_release: WorkflowNode,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    shard = workflow_job(provisional_ci, WINDOWS_GATE_SHARD_JOBS[-1])
    steps = shard["steps"]
    compat = workflow_step(shard, SHARD_SELECT_STEP)
    if mutation == "marker-only-units":
        # The pre-#4115 selection: windows_compat units only.
        steps.remove(workflow_step(shard, WINDOWS_UNIT_STEP))
        compat["run"] = compat["run"].replace("tests/integration", "tests/unit tests/integration")
    elif mutation == "drop-unit-step":
        steps.remove(workflow_step(shard, WINDOWS_UNIT_STEP))
    elif mutation == "unit-shard-count":
        unit = workflow_step(shard, WINDOWS_UNIT_STEP)
        unit["run"] = unit["run"].replace("--shard-count 3", "--shard-count 2")
    elif mutation == "skip-unit-step":
        workflow_step(shard, WINDOWS_UNIT_STEP)["if"] = "github.event_name != 'pull_request'"
    elif mutation == "tolerate-unit-failure":
        workflow_step(shard, WINDOWS_UNIT_STEP)["continue-on-error"] = True
    elif mutation == "skip-compat-step":
        workflow_step(shard, WINDOWS_COMPAT_STEP)["if"] = "success()"
    elif mutation == "unit-root-in-compat":
        compat["run"] = compat["run"].replace("tests/integration", "tests/integration tests/unit")
    elif mutation == "compat-root-dropped":
        compat["run"] = compat["run"].replace("tests/integration", "tests/integration/x.py")
    elif mutation == "push-bypasses-owner":
        step = workflow_step(
            workflow_job(provisional_release, "windows-unit-1"), WINDOWS_FULL_UNIT_STEP
        )
        step["run"] = step["run"].replace(
            " ".join(WINDOWS_UNIT_OWNER), "python -m pytest tests/unit tests/test_console.py"
        )
    elif mutation == "full-run-bypasses-owner":
        step = workflow_step(
            workflow_job(provisional_release, "build-windows"), WINDOWS_FULL_UNIT_STEP
        )
        step["run"] = "uv run pytest tests/unit tests/test_console.py -n auto"
    else:
        from scripts import ci_selections

        monkeypatch.setitem(
            ci_selections.SELECTIONS,
            "windows-unit",
            (*ci_selections.SELECTIONS["windows-unit"], "-m", "windows_compat"),
        )
    with pytest.raises(AssertionError):
        _assert_pr_windows_units(provisional_ci, provisional_release)


def _assert_lifecycle_smoke_marker_registered(markers: list[str]) -> None:
    """The `lifecycle_smoke` marker must be registered in pyproject.toml's
    canonical markers list. The CI invocation's `--strict-markers` flag
    turns an unregistered marker into a hard collection error, so this is
    a load-bearing prerequisite for the job to run at all -- not cosmetic
    documentation."""
    assert any(marker.startswith(f"{LIFECYCLE_SMOKE_MARKER}:") for marker in markers), (
        f"{LIFECYCLE_SMOKE_MARKER} marker must be registered in "
        "pyproject.toml's [tool.pytest.ini_options].markers so "
        "--strict-markers does not reject it"
    )
    assert any(marker.startswith(f"{LIFECYCLE_SMOKE_MERGE_GROUP_MARKER}:") for marker in markers), (
        f"{LIFECYCLE_SMOKE_MERGE_GROUP_MARKER} marker must be registered"
    )


def _assert_lifecycle_smoke_command(job: WorkflowNode) -> None:
    """The run step must select the complete lifecycle marker family."""
    step = workflow_step(job, LIFECYCLE_SMOKE_RUN_STEP)
    assert "if" not in step, "lifecycle-smoke test execution must be unconditional"
    assert "continue-on-error" not in step, "lifecycle-smoke test failures must fail the shard"
    pytest_commands = [tokens for tokens in shell_commands(step) if "pytest" in tokens]
    assert len(pytest_commands) == 1, (
        "lifecycle-smoke run step must contain exactly one pytest invocation"
    )
    command = pytest_commands[0]
    pytest_index = command.index("pytest")
    assert command[:pytest_index] == ["uv", "run", "--extra", "dev", "python", "-m"], (
        "lifecycle-smoke must use python -m pytest to load its repository plugin"
    )
    tokens = command[pytest_index + 1 :]
    assert "--strict-markers" in tokens, (
        "lifecycle-smoke must pass --strict-markers so an unregistered "
        "or typo'd marker name fails loudly instead of silently "
        "selecting zero tests"
    )
    worker_flags = [token for token in tokens if token.startswith(("-n", "--numprocesses"))]
    assert worker_flags == ["-n"], "lifecycle-smoke must use four bounded xdist workers"
    assert not any(token.startswith("--shard") for token in tokens), (
        "lifecycle-smoke must run its shard file list, not re-shard the collection"
    )
    assert "scripts.ci_sharding" not in tokens
    worker_index = tokens.index("-n")
    assert tokens[worker_index : worker_index + 2] == ["-n", "4"], (
        "lifecycle-smoke must use four bounded xdist workers"
    )
    assert [token for token in tokens if token.startswith("--dist")] == ["--dist"], (
        "lifecycle-smoke must use loadgroup to preserve xdist_group serialization"
    )
    scheduler_index = tokens.index("--dist")
    assert tokens[scheduler_index : scheduler_index + 2] == ["--dist", "loadgroup"], (
        "lifecycle-smoke must use loadgroup to preserve xdist_group serialization"
    )
    emitter = shard_file_list_args(workflow_step(job, SHARD_SELECT_STEP))
    pairs = [emitter[index : index + 2] for index in range(len(emitter) - 1)]
    for flag, value in (
        ("--shard-count", str(len(LIFECYCLE_SMOKE_SHARD_JOBS))),
        ("--shard-index", "$LIFECYCLE_SHARD"),
        ("--shard-durations", LIFECYCLE_SMOKE_DURATIONS),
        ("-m", LIFECYCLE_SMOKE_REQUIRED_EXPRESSION),
        ("--out", SHARD_FILE_LIST),
    ):
        assert emitter.count(flag) == 1 and [flag, value] in pairs, (
            f"lifecycle-smoke must select one shard's files with {flag} {value}"
        )
    assert _shard_list_roots(emitter) == [LIFECYCLE_SMOKE_ROOT], (
        f"lifecycle-smoke must bound marker collection to {LIFECYCLE_SMOKE_ROOT!r}"
    )
    assert not any(token.startswith(("--splits", "--group")) for token in tokens)
    assert not any(
        token.startswith(("--deselect", "--ignore", "--maxfail")) or token == "-k"
        for token in tokens
    ), "lifecycle-smoke must not narrow the required selection"
    assert "-m" in tokens
    marker_index = tokens.index("-m")
    assert tokens[marker_index + 1] == LIFECYCLE_SMOKE_REQUIRED_EXPRESSION
    targets = [token for token in tokens if token.startswith(("tests", "@"))]
    assert targets == [f"@{SHARD_FILE_LIST}"], (
        "lifecycle-smoke must run only its shard's explicit test file list"
    )


def _replace_in_lifecycle_commands(job: WorkflowNode, before: str, after: str) -> None:
    """Apply one edit to the shard's file-list step and its pytest step."""
    for name in (SHARD_SELECT_STEP, LIFECYCLE_SMOKE_RUN_STEP):
        step = workflow_step(job, name)
        step["run"] = step["run"].replace(before, after)


def _assert_lifecycle_smoke_hermetic(job: WorkflowNode) -> None:
    assert "secrets" not in job

    # Job-level `env:` is inherited by every step in GitHub Actions, so a
    # credential bound there would bypass a step-only check (panel review
    # of #2247: python-architect, supply-chain-security-expert, and
    # test-coverage-expert independently found and proved this gap).
    job_env = job.get("env")
    if isinstance(job_env, dict):
        for key in (*FORBIDDEN_CREDENTIAL_ENV, *FORBIDDEN_NETWORK_ENV):
            assert key not in job_env, f"lifecycle-smoke job-level env must not bind {key}"

    for step in _run_steps(job):
        env = step.get("env")
        if isinstance(env, dict):
            for key in (*FORBIDDEN_CREDENTIAL_ENV, *FORBIDDEN_NETWORK_ENV):
                assert key not in env, f"lifecycle-smoke step must not bind {key}"
        run = step.get("run")
        if isinstance(run, str):
            for key in (*FORBIDDEN_CREDENTIAL_ENV, *FORBIDDEN_NETWORK_ENV):
                assert key not in run, f"lifecycle-smoke step must not reference {key}"
        # `with:` values can smuggle a credential expression (e.g. a
        # checkout `token:` input) past the env/run-only checks above.
        with_block = step.get("with")
        if isinstance(with_block, dict):
            for value in with_block.values():
                if not isinstance(value, str):
                    continue
                for key in FORBIDDEN_CREDENTIAL_EXPRESSIONS:
                    assert key not in value, (
                        f"lifecycle-smoke step `with:` must not reference {key}"
                    )


def _assert_lifecycle_smoke_e2e_mode(job: WorkflowNode) -> None:
    """Pin the non-network E2E opt-in to the pytest step only."""
    job_env = job.get("env")
    assert not isinstance(job_env, dict) or LIFECYCLE_SMOKE_E2E_ENV not in job_env, (
        f"{LIFECYCLE_SMOKE_E2E_ENV} must not be job-level"
    )

    run_step = workflow_step(job, LIFECYCLE_SMOKE_RUN_STEP)
    assert run_step.get("env") == {
        LIFECYCLE_SMOKE_E2E_ENV: LIFECYCLE_SMOKE_E2E_VALUE,
    }, (
        f"{LIFECYCLE_SMOKE_RUN_STEP!r} must bind exactly "
        f"{LIFECYCLE_SMOKE_E2E_ENV}={LIFECYCLE_SMOKE_E2E_VALUE!r}"
    )
    for step in _run_steps(job):
        if step is run_step:
            continue
        env = step.get("env")
        assert not isinstance(env, dict) or LIFECYCLE_SMOKE_E2E_ENV not in env, (
            f"{LIFECYCLE_SMOKE_E2E_ENV} must appear only on {LIFECYCLE_SMOKE_RUN_STEP!r}"
        )


def _assert_lifecycle_smoke_required(merge_gate: WorkflowNode) -> None:
    gate = workflow_job(merge_gate, "gate")
    wait_step = workflow_step(gate, "Wait for all required checks")
    expected_checks = wait_step["env"]["EXPECTED_CHECKS"]
    assert isinstance(expected_checks, str)
    assert LIFECYCLE_SMOKE_CHECK in expected_checks


def _assert_marker_collection_non_empty(marker: str) -> None:
    """Runs a real `--collect-only` subprocess for the given marker
    expression and asserts pytest's exit code is 0, not 5 ("no tests
    collected"). Parameterized so the mutation test below can prove a
    marker guaranteed to match zero tests is caught by the exact same
    code path the real check exercises."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--collect-only",
            "--strict-markers",
            "-m",
            marker,
            LIFECYCLE_SMOKE_ROOT,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"marker {marker!r} family must not be empty -- pytest exit "
        f"code {result.returncode} (5 == no tests collected):\n{result.stdout}\n{result.stderr}"
    )
    assert "collected" in result.stdout, f"expected a collection summary, got: {result.stdout}"


def _collect_lifecycle_nodes(expression: str, shard: int | None = None) -> tuple[str, ...]:
    """Collect one lifecycle marker expression under the required environment."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--collect-only",
            "--strict-markers",
            "-m",
            expression,
            *(
                [LIFECYCLE_SMOKE_ROOT]
                if shard is None
                else ci_sharding.shard_test_files(
                    [LIFECYCLE_SMOKE_ROOT],
                    expression,
                    len(LIFECYCLE_SMOKE_SHARD_JOBS),
                    ci_sharding.load_durations(REPO_ROOT / LIFECYCLE_SMOKE_DURATIONS),
                    REPO_ROOT,
                ).shards[shard - 1]
            ),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **{key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            LIFECYCLE_SMOKE_E2E_ENV: LIFECYCLE_SMOKE_E2E_VALUE,
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return tuple(line for line in result.stdout.splitlines() if line.startswith("tests/"))


def _validated_lifecycle_node_set(nodes: Sequence[str], label: str) -> set[str]:
    """Return a non-empty node set, rejecting duplicate collection output."""
    assert nodes, f"{label} lifecycle selection must not be empty"
    node_set = set(nodes)
    assert len(node_set) == len(nodes), f"{label} lifecycle selection contains duplicate node IDs"
    return node_set


def _assert_lifecycle_partition_sets(
    full_nodes: Sequence[str],
    merge_group_nodes: Sequence[str],
    required_nodes: Sequence[str],
) -> None:
    """Validate independently collected lifecycle marker set relationships."""
    full = _validated_lifecycle_node_set(full_nodes, "full")
    merge_group = _validated_lifecycle_node_set(merge_group_nodes, "merge-group")
    required = _validated_lifecycle_node_set(required_nodes, "required")
    assert merge_group < full, "merge-group lifecycle selection must be a strict subset of full"
    assert required.isdisjoint(merge_group), (
        "required lifecycle selection must be disjoint from merge-group"
    )
    assert required == full - merge_group, (
        "required lifecycle selection must equal full minus merge-group"
    )
    assert full == required | merge_group, (
        "full lifecycle selection must equal required union merge-group"
    )


def _assert_lifecycle_marker_partition() -> None:
    """Validate three independently collected lifecycle marker selections."""
    _assert_lifecycle_partition_sets(
        _collect_lifecycle_nodes(LIFECYCLE_SMOKE_FULL_EXPRESSION),
        _collect_lifecycle_nodes(LIFECYCLE_SMOKE_MERGE_GROUP_MARKER),
        _collect_lifecycle_nodes(LIFECYCLE_SMOKE_REQUIRED_EXPRESSION),
    )


def _assert_merge_group_full_lifecycle_path() -> None:
    """Pin the merge-group workflow path that executes the full integration root."""
    workflow = load_workflow(MERGE_GROUP_INTEGRATION_WORKFLOW)
    job = workflow_job(workflow, MERGE_GROUP_INTEGRATION_JOB)
    step = workflow_step(job, MERGE_GROUP_INTEGRATION_STEP)
    assert step["env"][LIFECYCLE_SMOKE_E2E_ENV] == LIFECYCLE_SMOKE_E2E_VALUE
    assert "uv run ./scripts/test-integration.sh" in step["run"]
    script = MERGE_GROUP_INTEGRATION_SCRIPT.read_text(encoding="utf-8")
    assert "pytest tests/integration/ -v" in script


def _assert_lifecycle_smoke_timeout(job: WorkflowNode) -> None:
    """Preserve measured setup headroom and the bounded whole-job deadline."""
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int)
    assert timeout == LIFECYCLE_SMOKE_TIMEOUT_MINUTES, (
        "lifecycle-smoke needs its measured 10-minute whole-job budget, "
        "without an inadequate deadline or an unbounded increase"
    )


def _assert_lifecycle_smoke_aggregate(ci: WorkflowNode) -> None:
    """Require every explicit shard result, including fail-closed absent results."""
    job = workflow_job(ci, LIFECYCLE_SMOKE_JOB)
    assert job["name"] == LIFECYCLE_SMOKE_CHECK
    assert job["needs"] == list(LIFECYCLE_SMOKE_AGGREGATE_NEEDS)
    assert job["if"] == "always()"
    assert job["permissions"] == {}
    assert "continue-on-error" not in job
    step = workflow_step(job, LIFECYCLE_SMOKE_AGGREGATE_STEP)
    assert len(job["steps"]) == 1
    assert "if" not in step
    assert "continue-on-error" not in step
    assert step["env"] == {
        **{
            f"SHARD_{index}_RESULT": "${{ needs." + job_id + ".result }}"
            for index, job_id in enumerate(LIFECYCLE_SMOKE_SHARD_JOBS, 1)
        },
        "GATE_COVERAGE_RESULT": "${{ needs." + LIFECYCLE_SMOKE_GATE_COVERAGE_JOB + ".result }}",
    }
    _assert_success_results(
        step,
        (
            *(f"SHARD_{shard}_RESULT" for shard in LIFECYCLE_SMOKE_SHARDS),
            "GATE_COVERAGE_RESULT",
        ),
    )


def _assert_lifecycle_shard_partition(
    required_nodes: Sequence[str],
    shard_nodes: Sequence[Sequence[str]],
) -> None:
    required = _validated_lifecycle_node_set(required_nodes, "required")
    shards = [
        _validated_lifecycle_node_set(nodes, f"shard {index}")
        for index, nodes in enumerate(shard_nodes, 1)
    ]
    assert len(shards) == len(LIFECYCLE_SMOKE_SHARD_JOBS), "every lifecycle shard must be checked"
    union: set[str] = set()
    for shard in shards:
        assert union.isdisjoint(shard), "lifecycle shards must not overlap"
        union |= shard
    assert union == required, "lifecycle shard union must retain the required selection"


def test_lifecycle_shards_collect_exact_required_union() -> None:
    expression = LIFECYCLE_SMOKE_REQUIRED_EXPRESSION
    _assert_lifecycle_shard_partition(
        _collect_lifecycle_nodes(expression),
        [_collect_lifecycle_nodes(expression, shard) for shard in LIFECYCLE_SMOKE_SHARDS],
    )


@pytest.mark.parametrize(
    "shards",
    [
        [[], ["a"], ["b"], ["c"]],
        [["a"], ["a"], ["b"], ["c"]],
        [["a", "a"], ["b"], ["c"], ["d"]],
        [["a"], ["b"], ["c"], ["e"]],
        [["a"], ["b"], ["c", "d"]],
    ],
)
def test_lifecycle_shard_partition_drift_fails(shards: list[list[str]]) -> None:
    with pytest.raises(AssertionError):
        _assert_lifecycle_shard_partition(["a", "b", "c", "d"], shards)


@pytest.mark.parametrize("missing_job", LIFECYCLE_SMOKE_AGGREGATE_NEEDS)
def test_lifecycle_aggregate_missing_dependency_fails(
    provisional_ci: WorkflowNode, missing_job: str
) -> None:
    job = workflow_job(provisional_ci, LIFECYCLE_SMOKE_JOB)
    job["needs"].remove(missing_job)
    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_aggregate(provisional_ci)


def test_lifecycle_aggregate_without_always_fails(provisional_ci: WorkflowNode) -> None:
    job = workflow_job(provisional_ci, LIFECYCLE_SMOKE_JOB)
    job["if"] = "success()"
    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_aggregate(provisional_ci)


def test_lifecycle_aggregate_ignored_result_fails(provisional_ci: WorkflowNode) -> None:
    job = workflow_job(provisional_ci, LIFECYCLE_SMOKE_JOB)
    step = workflow_step(job, LIFECYCLE_SMOKE_AGGREGATE_STEP)
    step["run"] = step["run"].replace("exit 1", ":")
    with pytest.raises(AssertionError, match="invalid results"):
        _assert_lifecycle_smoke_aggregate(provisional_ci)


@pytest.mark.parametrize("shard", LIFECYCLE_SMOKE_SHARDS)
def test_lifecycle_aggregate_cannot_ignore_any_shard(
    provisional_ci: WorkflowNode, shard: int
) -> None:
    job = workflow_job(provisional_ci, LIFECYCLE_SMOKE_JOB)
    step = workflow_step(job, LIFECYCLE_SMOKE_AGGREGATE_STEP)
    step["run"] = step["run"].replace(f'[ "$SHARD_{shard}_RESULT" != success ]', "false")
    with pytest.raises(AssertionError, match="invalid results"):
        _assert_lifecycle_smoke_aggregate(provisional_ci)


def _assert_lifecycle_smoke_shard(ci: WorkflowNode, shard: int) -> None:
    job = workflow_job(ci, LIFECYCLE_SMOKE_SHARD_JOBS[shard - 1])
    assert job["name"] == f"Lifecycle Smoke Shard {shard} (Linux)"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["permissions"] == {"contents": "read"}
    assert job["env"] == {"LIFECYCLE_SHARD": str(shard)}
    assert "if" not in job
    assert "continue-on-error" not in job
    _assert_lifecycle_smoke_timeout(job)
    _assert_lifecycle_smoke_command(job)
    _assert_lifecycle_smoke_e2e_mode(job)
    _assert_lifecycle_smoke_hermetic(job)


@pytest.mark.parametrize("shard", LIFECYCLE_SMOKE_SHARDS)
@pytest.mark.parametrize("field", ["selector", "conditional", "ignore-failure"])
def test_lifecycle_shard_binding_drift_fails(
    provisional_ci: WorkflowNode, shard: int, field: str
) -> None:
    job = workflow_job(provisional_ci, LIFECYCLE_SMOKE_SHARD_JOBS[shard - 1])
    if field == "selector":
        job["env"]["LIFECYCLE_SHARD"] = str(shard % len(LIFECYCLE_SMOKE_SHARDS) + 1)
    elif field == "conditional":
        job["if"] = "false"
    else:
        job["continue-on-error"] = True
    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_shard(provisional_ci, shard)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("python -m pytest", "pytest"),
        ('--shard-index "$LIFECYCLE_SHARD"', ""),
        (f"@{SHARD_FILE_LIST}", LIFECYCLE_SMOKE_ROOT),
        ("--durations=20", "--durations=20 -p scripts.ci_sharding --shard-count 4"),
        ('--shard-index "$LIFECYCLE_SHARD"', "--shard-index 1"),
        ("--shard-count 4", "--shard-count 3"),
        (f"--shard-durations {LIFECYCLE_SMOKE_DURATIONS}", ""),
        ("--durations=20", "--durations=20 --splits 2 --group 1"),
        ("--durations=20", "--durations=20 -k partial"),
    ],
)
def test_lifecycle_shard_command_drift_fails(
    provisional_lifecycle_job: WorkflowNode, before: str, after: str
) -> None:
    _replace_in_lifecycle_commands(provisional_lifecycle_job, before, after)
    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


@pytest.mark.parametrize("field", ["if", "continue-on-error"])
def test_lifecycle_shard_step_cannot_suppress_execution_or_failure(
    provisional_lifecycle_job: WorkflowNode, field: str
) -> None:
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step[field] = True
    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_is_required_and_hermetic() -> None:
    """Pins the new PR-time gate as a semantic contract, not an exact
    target list: the marker is registered, the command selects
    declaratively via that marker within a bounded root, the job stays
    hermetic and required, and the marker family actually collects tests
    right now. This is the positive half of the guard -- the mutation
    tests below prove it actually catches drift rather than trivially
    passing."""
    ci = load_workflow(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    for shard in LIFECYCLE_SMOKE_SHARDS:
        _assert_lifecycle_smoke_shard(ci, shard)

    _assert_lifecycle_smoke_marker_registered(_pytest_ini_markers())
    _assert_lifecycle_marker_partition()
    _assert_merge_group_full_lifecycle_path()
    _assert_lifecycle_smoke_aggregate(ci)

    merge_gate = load_workflow(REPO_ROOT / ".github" / "workflows" / "merge-gate.yml")
    _assert_lifecycle_smoke_required(merge_gate)


@pytest.fixture(params=LIFECYCLE_SMOKE_SHARD_JOBS)
def provisional_lifecycle_job(
    provisional_ci: WorkflowNode, request: pytest.FixtureRequest
) -> WorkflowNode:
    return workflow_job(provisional_ci, request.param)


@pytest.mark.parametrize("timeout", [None, 0, 8, 11])
def test_lifecycle_smoke_timeout_drift_fails(
    provisional_lifecycle_job: WorkflowNode, timeout: int | None
) -> None:
    """Reject missing, inadequate, and inflated whole-job budgets."""
    provisional_lifecycle_job["timeout-minutes"] = timeout
    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_timeout(provisional_lifecycle_job)


def test_lifecycle_smoke_exclusion_dropped_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    _replace_in_lifecycle_commands(
        provisional_lifecycle_job,
        LIFECYCLE_SMOKE_REQUIRED_EXPRESSION,
        LIFECYCLE_SMOKE_FULL_EXPRESSION,
    )

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_marker_expression_substitution_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    _replace_in_lifecycle_commands(
        provisional_lifecycle_job,
        LIFECYCLE_SMOKE_REQUIRED_EXPRESSION,
        "lifecycle_smoke or lifecycle_merge_group",
    )

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_unbounded_root_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    _replace_in_lifecycle_commands(provisional_lifecycle_job, "tests/integration", "tests")

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_strict_markers_flag_dropped_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    """Proves the guard fails if --strict-markers is silently dropped
    from the invocation -- without it, a future typo in the marker name
    would silently collect zero tests (or the wrong tests) instead of
    erroring loudly at collection time."""
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace("--strict-markers ", "")

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_e2e_flag_dropped_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step.pop("env")

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_e2e_mode(provisional_lifecycle_job)


@pytest.mark.parametrize(
    "parallel_args",
    [
        "",
        "-n 0 --dist loadgroup",
        "-n auto --dist loadgroup",
        "-n 2 --dist loadgroup",
        "-n 3 --dist loadgroup",
        "-n 4 --dist worksteal",
        "-n 4 --dist loadgroup --numprocesses=auto",
        "-n 4 --dist loadgroup --dist=load",
    ],
)
def test_lifecycle_smoke_parallelism_drift_fails(
    provisional_lifecycle_job: WorkflowNode, parallel_args: str
) -> None:
    """Keep the runtime budget without dropping grouping or allowing unbounded workers."""
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace("-n 4 --dist loadgroup", parallel_args)

    with pytest.raises(AssertionError, match=r"workers|loadgroup"):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_e2e_flag_wrong_value_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["env"][LIFECYCLE_SMOKE_E2E_ENV] = "true"

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_e2e_mode(provisional_lifecycle_job)


def test_lifecycle_smoke_e2e_flag_at_job_level_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step.pop("env")
    provisional_lifecycle_job["env"] = {
        LIFECYCLE_SMOKE_E2E_ENV: LIFECYCLE_SMOKE_E2E_VALUE,
    }

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_e2e_mode(provisional_lifecycle_job)


def test_lifecycle_smoke_marker_not_registered_fails() -> None:
    """Proves the guard fails if the lifecycle_smoke marker is silently
    dropped from pyproject.toml's registered markers list -- the state
    that would make --strict-markers reject every
    @pytest.mark.lifecycle_smoke decorator at collection time."""
    markers = [m for m in _pytest_ini_markers() if not m.startswith(f"{LIFECYCLE_SMOKE_MARKER}:")]

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_marker_registered(markers)


def test_lifecycle_smoke_marker_family_empty_fails() -> None:
    """Proves the collection-non-empty check would fail loudly (pytest
    exit code 5, "no tests collected") if the lifecycle_smoke family ever
    became empty -- e.g. if every @pytest.mark.lifecycle_smoke were
    stripped from the four modules and the AC14 function. Exercises the
    exact same subprocess code path as
    _assert_lifecycle_smoke_collection_non_empty, with a marker name
    guaranteed to match zero tests today."""
    with pytest.raises(AssertionError):
        _assert_marker_collection_non_empty("lifecycle_smoke_nonexistent_probe_marker")


@pytest.mark.parametrize("empty_index", (0, 1, 2))
def test_lifecycle_marker_partition_rejects_empty_selection(empty_index: int) -> None:
    selections = [["a", "b"], ["b"], ["a"]]
    selections[empty_index] = []

    with pytest.raises(AssertionError, match="must not be empty"):
        _assert_lifecycle_partition_sets(*selections)


def test_lifecycle_marker_partition_rejects_merge_group_outside_full() -> None:
    with pytest.raises(AssertionError, match="strict subset"):
        _assert_lifecycle_partition_sets(["a", "b"], ["outside"], ["a", "b"])


def test_lifecycle_marker_partition_rejects_required_merge_overlap() -> None:
    with pytest.raises(AssertionError, match="must be disjoint"):
        _assert_lifecycle_partition_sets(["a", "b"], ["b"], ["a", "b"])


@pytest.mark.parametrize("required", (["a"], ["a", "outside"]))
def test_lifecycle_marker_partition_rejects_missing_or_extra_required(
    required: list[str],
) -> None:
    with pytest.raises(AssertionError, match="full minus merge-group"):
        _assert_lifecycle_partition_sets(["a", "b", "c"], ["c"], required)


@pytest.mark.parametrize("duplicate_index", (0, 1, 2))
def test_lifecycle_marker_partition_rejects_duplicate_node_ids(
    duplicate_index: int,
) -> None:
    selections = [["a", "b"], ["b"], ["a"]]
    selections[duplicate_index].append(selections[duplicate_index][0])

    with pytest.raises(AssertionError, match="duplicate node IDs"):
        _assert_lifecycle_partition_sets(*selections)


def test_lifecycle_smoke_duplicate_pytest_invocation_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = f"{step['run']}\n{step['run']}"

    with pytest.raises(AssertionError, match="exactly one pytest invocation"):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_credential_env_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    """Proves the guard fails if this job is ever wired to a credential,
    which would break the hermetic/no-credentials contract the PR-time
    critical path depends on."""
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["env"] = {"GITHUB_APM_PAT": "${{ secrets.GITHUB_APM_PAT }}"}

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_hermetic(provisional_lifecycle_job)


def test_lifecycle_smoke_network_integration_env_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["env"]["APM_RUN_INTEGRATION_TESTS"] = "1"

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_hermetic(provisional_lifecycle_job)


def test_lifecycle_smoke_job_level_credential_env_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    """Proves the guard fails if a credential is bound at job-level `env:`
    rather than step-level -- job-level env is inherited by every step in
    GitHub Actions, so this is a distinct injection vector from the
    step-level case above, not a duplicate of it. Panel review of #2247
    found this gap independently in three lenses (architecture, supply
    chain security, test coverage) and it was empirically confirmed:
    without this fix, the same mutation on `job["env"]` passed silently."""
    provisional_lifecycle_job["env"] = {"GITHUB_APM_PAT": "${{ secrets.GITHUB_APM_PAT }}"}

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_hermetic(provisional_lifecycle_job)


def test_lifecycle_smoke_with_block_credential_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    """Proves the guard fails if a credential expression is smuggled
    through a step's `with:` block (e.g. an actions/checkout `token:`
    input) instead of `env:`/`run:` -- a third distinct injection vector
    flagged by the supply-chain-security-expert panelist."""
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["with"] = {"token": "${{ secrets.GITHUB_APM_PAT }}"}

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_hermetic(provisional_lifecycle_job)


def test_lifecycle_smoke_github_token_expression_alias_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    """Proves the guard catches the `${{ github.token }}` context alias,
    which resolves to the same automatic GITHUB_TOKEN but does not
    contain the `GITHUB_TOKEN` substring FORBIDDEN_CREDENTIAL_ENV alone
    would catch."""
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["with"] = {"token": "${{ github.token }}"}

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_hermetic(provisional_lifecycle_job)


def test_lifecycle_smoke_removed_from_expected_checks_fails() -> None:
    """Proves the guard fails if Lifecycle Smoke is dropped from
    merge-gate's EXPECTED_CHECKS -- i.e. the job could exist and pass
    while silently no longer blocking merges."""
    merge_gate = deepcopy(load_workflow(REPO_ROOT / ".github" / "workflows" / "merge-gate.yml"))
    gate = workflow_job(merge_gate, "gate")
    wait_step = workflow_step(gate, "Wait for all required checks")
    wait_step["env"]["EXPECTED_CHECKS"] = wait_step["env"]["EXPECTED_CHECKS"].replace(
        LIFECYCLE_SMOKE_CHECK, "REMOVED"
    )

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_required(merge_gate)


# Shard layout (#4030): PR unit parallelism, main-push Windows unit shards and
# the macOS arm64 unit/build split. Same selections; only the layout changes.
RELEASE_WORKFLOW = REPO_ROOT / ".github/workflows/build-release.yml"
UNIT_SHARD_STEP = "Run tests with coverage"
WINDOWS_UNIT_SHARD_JOBS = ("windows-unit-1", "windows-unit-2", "windows-unit-3")
WINDOWS_PUSH_BUILD_JOB = "build-windows-push"
WINDOWS_PUSH_GATE_STEP = "Require Windows push tests"
MACOS_ARM_UNIT_JOB = "unit-macos-arm"
MACOS_ARM_BUILD_JOB = "build-and-validate-macos-arm"
MACOS_UNIT_COMMAND = "uv run pytest tests/unit tests/test_console.py -n auto --dist worksteal"
PROMOTED_SKIP = "needs.release-plan.outputs.mode == 'promoted' && needs.{job}.result == 'skipped'"


@pytest.fixture
def release_workflow() -> WorkflowNode:
    return deepcopy(load_workflow(RELEASE_WORKFLOW))


def _assert_unit_shard_layout(ci: WorkflowNode) -> None:
    job = workflow_job(ci, "build-and-test-shard")
    assert job["strategy"]["matrix"]["shard"] == [1, 2]
    tokens = shlex.split(workflow_step(job, UNIT_SHARD_STEP)["run"].replace("\\\n", " "))
    pairs = list(pairwise(tokens))
    assert ("--shard-count", "2") in pairs
    worker_flags = [t for t in tokens if t.startswith(("-n", "--numprocesses", "--dist"))]
    assert worker_flags == ["-n", "--dist"], "PR unit shards must set xdist exactly once"
    assert ("-n", "auto") in pairs and ("--dist", "worksteal") in pairs


def _assert_windows_unit_layout(release: WorkflowNode) -> None:
    jobs = release["jobs"]
    shard_jobs = tuple(sorted(j for j in jobs if j.startswith("windows-unit-")))
    assert shard_jobs == WINDOWS_UNIT_SHARD_JOBS
    count = len(WINDOWS_UNIT_SHARD_JOBS)
    for index, job_id in enumerate(WINDOWS_UNIT_SHARD_JOBS, 1):
        job = jobs[job_id]
        assert job["name"] == f"Windows units ({index}/{count})"
        assert job["env"] == {"SHARD": str(index)}
        assert "continue-on-error" not in job
        step = workflow_step(job, "Run unit tests (Windows diagnostics)")
        assert f"--shard-count {count} --shard-index $env:SHARD" in step["run"]
    build = jobs[WINDOWS_PUSH_BUILD_JOB]
    assert build["needs"] == ["release-plan", "windows-installer", *WINDOWS_UNIT_SHARD_JOBS]
    expected = ["${{ needs.release-plan.outputs.mode }}"] + [
        "${{ needs." + dep + ".result }}" for dep in build["needs"]
    ]
    assert build["env"]["PUSH_RESULTS"].split(":") == expected
    gate = workflow_step(build, WINDOWS_PUSH_GATE_STEP)
    values = ["push"] + ["success"] * len(build["needs"])

    def accepts(parts: list[str]) -> bool:
        return run_bash(gate["run"], env={"PUSH_RESULTS": ":".join(parts)}).returncode == 0

    assert accepts(values), "Windows push gate must accept every shard succeeding"
    for index in range(1, len(values)):
        for result in ("failure", "cancelled", "skipped", ""):
            assert not accepts([*values[:index], result, *values[index + 1 :]]), (
                f"Windows push gate accepted {result!r} at position {index}"
            )
    assert not accepts(values[:-1]), "Windows push gate accepted a missing shard"


def _assert_macos_arm_split(release: WorkflowNode) -> None:
    jobs = release["jobs"]
    unit = jobs[MACOS_ARM_UNIT_JOB]
    build = jobs[MACOS_ARM_BUILD_JOB]
    assert unit["runs-on"] == build["runs-on"] == "macos-latest"
    assert unit["needs"] == build["needs"] == ["release-plan"]
    assert unit["if"] == (
        "needs.release-plan.outputs.mode != 'push' && needs.release-plan.outputs.run_unit == 'true'"
    )
    assert "continue-on-error" not in unit
    unit_steps = _run_steps(unit)
    step = unit_steps[-1]
    assert step["run"] == MACOS_UNIT_COMMAND
    assert "if" not in step and "continue-on-error" not in step
    setup = [s for s in _run_steps(build) if s.get("name") != "Build binary"]
    assert unit_steps[:-1] == setup[: len(unit_steps) - 1], "unit job must keep the build setup"
    for job_id in (MACOS_ARM_BUILD_JOB, "build-and-validate-macos-intel"):
        assert all("tests/unit" not in str(s.get("run", "")) for s in _run_steps(jobs[job_id]))
    for consumer in ("create-release", "daily-release-smoke-signal"):
        assert MACOS_ARM_UNIT_JOB in jobs[consumer]["needs"], consumer
    condition = " ".join(jobs["create-release"]["if"].split())
    assert (
        f"(needs.{MACOS_ARM_UNIT_JOB}.result == 'success' || "
        f"({PROMOTED_SKIP.format(job=MACOS_ARM_UNIT_JOB)}))"
    ) in condition


def test_shard_layout_matches_issue_4030() -> None:
    _assert_unit_shard_layout(load_workflow(REPO_ROOT / ".github/workflows/ci.yml"))
    release = load_workflow(RELEASE_WORKFLOW)
    _assert_windows_unit_layout(release)
    _assert_macos_arm_split(release)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("-n auto --dist worksteal", "-n 2 --dist worksteal"),
        ("-n auto --dist worksteal", "-n auto --dist load"),
        ("-n auto --dist worksteal", "-n auto --dist worksteal -n 2"),
        ("--shard-count 2", "--shard-count 3"),
    ],
)
def test_unit_shard_layout_drift_fails(
    provisional_ci: WorkflowNode, before: str, after: str
) -> None:
    step = workflow_step(workflow_job(provisional_ci, "build-and-test-shard"), UNIT_SHARD_STEP)
    step["run"] = step["run"].replace(before, after)
    with pytest.raises(AssertionError):
        _assert_unit_shard_layout(provisional_ci)


@pytest.mark.parametrize(
    "mutation", ["drop-shard", "drop-need", "drop-result", "count", "ignore-gate"]
)
def test_windows_unit_layout_drift_fails(release_workflow: WorkflowNode, mutation: str) -> None:
    jobs = release_workflow["jobs"]
    build = jobs[WINDOWS_PUSH_BUILD_JOB]
    if mutation == "drop-shard":
        del jobs["windows-unit-3"]
    elif mutation == "drop-need":
        build["needs"].remove("windows-unit-3")
    elif mutation == "drop-result":
        build["env"]["PUSH_RESULTS"] = build["env"]["PUSH_RESULTS"].rsplit(":", 1)[0]
    elif mutation == "count":
        step = workflow_step(jobs["windows-unit-1"], "Run unit tests (Windows diagnostics)")
        step["run"] = step["run"].replace("--shard-count 3", "--shard-count 2")
    else:
        gate = workflow_step(build, WINDOWS_PUSH_GATE_STEP)
        gate["run"] = gate["run"].replace("test ", "true ")
    with pytest.raises(AssertionError):
        _assert_windows_unit_layout(release_workflow)


@pytest.mark.parametrize(
    "mutation", ["drop-unit", "unit-selection", "release-need", "release-gate", "unit-in-build"]
)
def test_macos_arm_split_drift_fails(release_workflow: WorkflowNode, mutation: str) -> None:
    jobs = release_workflow["jobs"]
    if mutation == "drop-unit":
        del jobs[MACOS_ARM_UNIT_JOB]
    elif mutation == "unit-selection":
        jobs[MACOS_ARM_UNIT_JOB]["steps"][-1]["run"] += " -k smoke"
    elif mutation == "release-need":
        jobs["create-release"]["needs"].remove(MACOS_ARM_UNIT_JOB)
    elif mutation == "release-gate":
        jobs["create-release"]["if"] = jobs["create-release"]["if"].replace(
            f"needs.{MACOS_ARM_UNIT_JOB}.result == 'success'", "true"
        )
    else:
        jobs[MACOS_ARM_BUILD_JOB]["steps"].append({"run": MACOS_UNIT_COMMAND})
    with pytest.raises((AssertionError, KeyError)):
        _assert_macos_arm_split(release_workflow)


# Shard file lists (#4120, PERF-H8). Every sharded job runs an explicit,
# disjoint list of test files (or, for a module too heavy for one shard, its
# top-level test node IDs) written by `python -m scripts.ci_sharding files`,
# so a shard imports only its own modules. The drift guard collects every
# sharded root once on this SHA and requires each job's shards to cover
# every selected node exactly once.
SHARD_SEED_DIR = ".github/ci-durations/"
# Sharded jobs whose shard count is planned at run time: check the planner's cap.
PLANNED_SHARD_COUNTS = {"integration-linux-x86": 8, "integration-linux-arm": 6}
SHARD_FILE_LIST_JOBS = (
    ("ci.yml", "windows-compat-gate-1"),
    ("ci.yml", "build-and-test-shard"),
    ("ci.yml", "lifecycle-smoke-shard-1"),
    ("build-release.yml", "integration-linux-x86"),
    ("build-release.yml", "integration-linux-arm"),
    ("ci-integration.yml", "integration-tests-shard"),
)
SHARDED_ROOTS = (
    "tests/unit",
    "tests/integration",
    "tests/test_console.py",
    "tests/red_team",
    "tests/fixtures/policy",
)


def shard_file_list_args(step: WorkflowNode) -> list[str]:
    """Arguments of the one `scripts.ci_sharding files` command in a step."""
    commands = [tokens for tokens in shell_commands(step) if "scripts.ci_sharding" in tokens]
    assert len(commands) == 1, f"{step.get('name')!r} must run the shard file-list emitter once"
    command = commands[0]
    index = command.index("scripts.ci_sharding")
    assert command[index + 1 : index + 2] == ["files"], "the emitter must run its files mode"
    return command[index + 2 :]


def _shard_list_roots(args: Sequence[str]) -> list[str]:
    return list(ci_sharding._files_parser().parse_args([*_resolved_index(args)]).targets)


def _resolved_index(args: Sequence[str]) -> list[str]:
    tokens = list(args)
    if "--shard-index" in tokens:
        tokens[tokens.index("--shard-index") + 1] = "1"
    return tokens


def _resolve_job_tokens(args: Sequence[str], job: WorkflowNode, job_id: str) -> list[str]:
    """Bind the workflow expressions an emitter command uses to checkable values."""
    env = {key: str(value) for key, value in (job.get("env") or {}).items()}
    resolved: list[str] = []
    for token in _resolved_index(args):
        for key, value in env.items():
            token = token.replace("${{ env." + key + " }}", value).replace("$" + key, value)
        if token.startswith("${{ needs.integration-plan.outputs."):
            token = str(PLANNED_SHARD_COUNTS[job_id])
        # scripts/ci-shard-durations.sh seeds shard-plan/ from the committed seeds.
        resolved.append(token.replace("shard-plan/", SHARD_SEED_DIR))
    return resolved


def _job_shards(workflow: str, job_id: str) -> tuple[argparse.Namespace, list[tuple[str, ...]]]:
    """Every shard's file list for one sharded job, through the canonical emitter."""
    job = workflow_job(load_workflow(REPO_ROOT / ".github/workflows" / workflow), job_id)
    raw = shard_file_list_args(workflow_step(job, SHARD_SELECT_STEP))
    args = ci_sharding._files_parser().parse_args(_resolve_job_tokens(raw, job, job_id))
    plan = ci_sharding.shard_test_files(
        args.targets,
        args.marker_expression,
        args.shard_count,
        ci_sharding.load_durations(REPO_ROOT / args.shard_durations),
        REPO_ROOT,
    )
    return args, [tuple(shard) for shard in plan.shards]


def _windows_unit_shards() -> list[tuple[str, ...]]:
    from scripts import ci_selections

    shards: list[tuple[str, ...]] = []
    for index in range(1, len(WINDOWS_GATE_SHARD_JOBS) + 1):
        args = ci_selections.shard_pytest_args(
            "windows-unit",
            ["--shard-count", str(len(WINDOWS_GATE_SHARD_JOBS)), "--shard-index", str(index)],
        )
        shards.append(tuple(arg for arg in args if arg.startswith("tests/")))
    return shards


def _selected_nodes(
    inventory: dict[str, list[str]], roots: Sequence[str], expression: str | None
) -> set[str]:
    from _pytest.mark.expression import Expression

    compiled = Expression.compile(expression) if expression else None
    selected: set[str] = set()
    for node, markers in inventory.items():
        path = node.split("::", 1)[0]
        if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots):
            continue
        if compiled is None or compiled.evaluate(_marker_matcher(set(markers))):
            selected.add(node)
    return selected


def _marker_matcher(names: set[str]):
    return lambda name, /, **_kwargs: name in names


def _assert_shard_entries_partition(
    selected: set[str],
    collected: set[str],
    shards: Sequence[Sequence[str]],
    label: str,
) -> None:
    """Every selected node runs on exactly one shard; every node ID entry exists."""
    owners: dict[str, list[int]] = {}
    for index, shard in enumerate(shards, 1):
        assert shard, f"{label}: shard {index} has no test files"
        for entry in shard:
            assert entry not in owners, f"{label}: {entry} is listed on two shards"
            owners[entry] = [index]
            if "::" in entry:
                assert any(
                    node == entry or node.startswith((entry + "::", entry + "["))
                    for node in collected
                ), f"{label}: shard {index} names {entry}, which pytest does not collect"
    dropped: list[str] = []
    duplicated: list[str] = []
    for node in sorted(selected):
        module, _, rest = node.partition("::")
        top = re.split(r"::|\[", rest, maxsplit=1)[0]
        hits = [owner for key in (module, f"{module}::{top}") for owner in owners.get(key, [])]
        if not hits:
            dropped.append(node)
        elif len(hits) > 1:
            duplicated.append(node)
    assert not dropped, (
        f"{label}: shard file lists drop {len(dropped)} selected tests a full collect "
        f"finds, e.g. {dropped[:3]}; add their modules to the selection"
    )
    assert not duplicated, f"{label}: tests run on two shards, e.g. {duplicated[:3]}"


@pytest.fixture(scope="module")
def full_collection(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str]]:
    """One full collect of every sharded root on this SHA, with each node's markers."""
    out = tmp_path_factory.mktemp("shard-inventory") / "inventory.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--collect-only",
            "-p",
            "scripts.ci_sharding",
            "--shard-inventory-out",
            str(out),
            # Override addopts' -m so every job's own expression is evaluated here.
            "-m",
            "",
            *SHARDED_ROOTS,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env={
            **{key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            LIFECYCLE_SMOKE_E2E_ENV: LIFECYCLE_SMOKE_E2E_VALUE,
        },
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("workflow", "job_id"), SHARD_FILE_LIST_JOBS)
def test_shard_file_lists_match_full_collection(
    full_collection: dict[str, list[str]], workflow: str, job_id: str
) -> None:
    args, shards = _job_shards(workflow, job_id)
    _assert_shard_entries_partition(
        _selected_nodes(full_collection, args.targets, args.marker_expression),
        set(full_collection),
        shards,
        f"{workflow}:{job_id}",
    )


def test_windows_unit_shard_file_lists_match_full_collection(
    full_collection: dict[str, list[str]],
) -> None:
    from scripts.ci_selections import selection_roots

    _assert_shard_entries_partition(
        _selected_nodes(full_collection, selection_roots("windows-unit"), None),
        set(full_collection),
        _windows_unit_shards(),
        "ci_selections:windows-unit",
    )


@pytest.mark.parametrize(("workflow", "job_id"), SHARD_FILE_LIST_JOBS)
def test_sharded_jobs_run_their_file_list(workflow: str, job_id: str) -> None:
    """The pytest step runs the emitted list, never a whole root or a node-level re-shard."""
    job = workflow_job(load_workflow(REPO_ROOT / ".github/workflows" / workflow), job_id)
    emitter = shard_file_list_args(workflow_step(job, SHARD_SELECT_STEP))
    assert emitter[emitter.index("--out") + 1] == SHARD_FILE_LIST
    runs = [
        (step, tokens)
        for step in _run_steps(job)
        for tokens in (shell_commands(step) if "run" in step else [])
        if "pytest" in tokens or "./scripts/test-integration.sh" in tokens
    ]
    assert len(runs) == 1, f"{job_id} must run one sharded pytest command"
    step, tokens = runs[0]
    if "pytest" in tokens:
        targets = [
            token
            for token in tokens[tokens.index("pytest") + 1 :]
            if token.startswith(("tests", "@"))
        ]
        extra = tokens
    else:
        targets = [step["env"]["PYTEST_TARGETS"]]
        extra = str(step["env"].get("PYTEST_EXTRA_ARGS", "")).split()
    assert targets == [f"@{SHARD_FILE_LIST}"], f"{job_id} must pass only its shard file list"
    assert not any(
        flag in extra for flag in ("--shard-count", "--shard-index", "--splits", "--group")
    )


@pytest.mark.parametrize(
    "drift", ["dropped-file", "dropped-test", "two-shards", "missing-node", "empty-shard"]
)
def test_shard_file_list_drift_fails(drift: str) -> None:
    selected = {"tests/a.py::t", "tests/b.py::T::t[1]", "tests/c.py::u"}
    collected = {*selected, "tests/b.py::Other::t"}
    baseline = [["tests/a.py", "tests/b.py::T"], ["tests/c.py"]]
    _assert_shard_entries_partition(selected, collected, baseline, "baseline")
    shards = deepcopy(baseline)
    if drift == "dropped-file":
        shards[1] = ["tests/d.py"]
    elif drift == "dropped-test":
        shards[0][1] = "tests/b.py::Other"
    elif drift == "two-shards":
        shards[1].append("tests/a.py")
    elif drift == "missing-node":
        shards[1].append("tests/c.py::gone")
    else:
        shards.append([])
    with pytest.raises(AssertionError):
        _assert_shard_entries_partition(selected, collected, shards, "drift")
