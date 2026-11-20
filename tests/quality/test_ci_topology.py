from __future__ import annotations

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

from scripts.test_file_inventory import is_test_module_path
from tests.quality.repository_python_inventory import (
    INVENTORY_CALLS,
    PythonModuleFacts,
    tracked_python_inventory,
)
from tests.workflow_contracts import (
    WorkflowNode,
    assert_exact_command,
    assert_unconditional,
    effective_env,
    load_workflow,
    run_bash,
    shell_commands,
    shell_tokens,
    workflow_job,
    workflow_step,
)

pytest_plugins = ["pytester"]

RATCHET_TEST_SCOPE = "repository"

REPO_ROOT = Path(__file__).resolve().parents[2]
RATCHET_JOB = "test-architecture"
RATCHET_CHECK = "Test Architecture Ratchets"
RATCHET_CONTRACT_JOB = "test-architecture-contracts"
RATCHET_SCALING_JOB = "test-architecture-scaling"
RATCHET_EXECUTION_JOBS = (RATCHET_CONTRACT_JOB, RATCHET_SCALING_JOB)
# A job that hits its cap ends "cancelled", which hides the ratchet result.
RATCHET_TIMEOUT_MINUTES = {RATCHET_CONTRACT_JOB: 15, RATCHET_SCALING_JOB: 5}
RATCHET_AGGREGATE_STEP = "Require the architecture contracts"
RATCHET_SCALING_CHECK = "Architecture Scaling (Linux)"
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
LIFECYCLE_SMOKE_TIMEOUT_MINUTES = 15
LIFECYCLE_SMOKE_MARKER = "lifecycle_smoke"
# The lifecycle family core (#4093) runs beside the integration lifecycle modules.
LIFECYCLE_SMOKE_ROOTS = ("tests/integration", "tests/lifecycle")
LIFECYCLE_SMOKE_E2E_ENV = "APM_E2E_TESTS"
LIFECYCLE_SMOKE_E2E_VALUE = "1"
LIFECYCLE_SMOKE_FULL_EXPRESSION = "lifecycle_smoke"
LIFECYCLE_SMOKE_MERGE_GROUP_MARKER = "lifecycle_merge_group"
LIFECYCLE_SMOKE_REQUIRED_EXPRESSION = (
    f"{LIFECYCLE_SMOKE_FULL_EXPRESSION} and not {LIFECYCLE_SMOKE_MERGE_GROUP_MARKER}"
)
LIFECYCLE_MARKER_IMPLICATION_HOOK = "tests/conftest.py:pytest_collection_modifyitems"
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
    assert job["timeout-minutes"] == RATCHET_TIMEOUT_MINUTES[job_id]
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
    """The required aggregate gates only the contracts; scaling stays separate.

    The wall-clock scaling ratio is a non-required signal (timing asserts
    flake), so the required aggregate must not depend on its result.
    """
    aggregate = workflow_job(ci, RATCHET_JOB)
    assert aggregate.get("needs") == [RATCHET_CONTRACT_JOB], (
        "the required ratchet aggregate must depend only on the contracts job"
    )
    assert aggregate["name"] == RATCHET_CHECK
    assert aggregate["if"] == "always()"
    assert aggregate["permissions"] == {}
    assert "continue-on-error" not in aggregate
    assert len(aggregate["steps"]) == 1
    step = workflow_step(aggregate, RATCHET_AGGREGATE_STEP)
    assert "if" not in step and "continue-on-error" not in step
    assert step["env"] == {
        "CONTRACT_RESULT": "${{ needs.test-architecture-contracts.result }}",
    }
    assert RATCHET_SCALING_JOB not in str(aggregate), "the aggregate must not read scaling"
    _assert_success_results(step, ("CONTRACT_RESULT",))
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
    contexts = _expected_checks(merge_gate)
    for context, names in contexts.items():
        assert RATCHET_CHECK in names, f"{RATCHET_CHECK} must gate {context}"
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


def test_test_architecture_ratchets_are_required_in_every_context(
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
    """Both original workloads retain independent, pinned execution caps."""
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
        ("timeout-minutes", -1),
        ("timeout-minutes", 1),
        ("runs-on", "ubuntu-latest"),
        ("permissions", {"contents": "write"}),
        ("if", "false"),
        ("continue-on-error", True),
    ],
)
def test_ratchet_execution_job_drift_fails(
    provisional_ci: WorkflowNode, job_id: str, field: str, value: object
) -> None:
    if field == "timeout-minutes":
        value = RATCHET_TIMEOUT_MINUTES[job_id] + value
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
        ("needs", []),
        ("needs", [RATCHET_SCALING_JOB]),
        ("needs", [RATCHET_CONTRACT_JOB, RATCHET_SCALING_JOB]),
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


def test_ratchet_aggregate_cannot_ignore_the_contracts(provisional_ci: WorkflowNode) -> None:
    step = workflow_step(workflow_job(provisional_ci, RATCHET_JOB), RATCHET_AGGREGATE_STEP)
    step["run"] = step["run"].replace('[ "$CONTRACT_RESULT" != success ]', "false")
    with pytest.raises(AssertionError, match="invalid results"):
        _assert_ratchet_split(provisional_ci)


def test_ratchet_aggregate_reading_the_scaling_result_fails(provisional_ci: WorkflowNode) -> None:
    """Negative control: gating the required aggregate on the timing ratio fails."""
    step = workflow_step(workflow_job(provisional_ci, RATCHET_JOB), RATCHET_AGGREGATE_STEP)
    step["env"]["SCALING_RESULT"] = "${{ needs.test-architecture-scaling.result }}"
    step["run"] = step["run"].replace(
        '[ "$CONTRACT_RESULT" != success ]',
        '[ "$CONTRACT_RESULT" != success ] || [ "$SCALING_RESULT" != success ]',
    )
    with pytest.raises(AssertionError):
        _assert_ratchet_split(provisional_ci)


def test_architecture_scaling_is_never_required() -> None:
    merge_gate = load_workflow(REPO_ROOT / ".github" / "workflows" / "merge-gate.yml")
    for context, names in _expected_checks(merge_gate).items():
        assert RATCHET_SCALING_CHECK not in names, f"{RATCHET_SCALING_CHECK} gates {context}"


def test_ratchet_aggregate_cannot_suppress_failure(provisional_ci: WorkflowNode) -> None:
    step = workflow_step(workflow_job(provisional_ci, RATCHET_JOB), RATCHET_AGGREGATE_STEP)
    step["run"] = step["run"].replace("exit 1", ":")
    with pytest.raises(AssertionError, match="invalid results"):
        _assert_ratchet_split(provisional_ci)


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
    pairs = [tokens[index : index + 2] for index in range(len(tokens) - 1)]
    assert ["-p", "scripts.ci_sharding"] in pairs, (
        "lifecycle-smoke must load the affinity-aware duration sharder"
    )
    for flag, value in (
        ("--shard-count", str(len(LIFECYCLE_SMOKE_SHARD_JOBS))),
        ("--shard-index", "$LIFECYCLE_SHARD"),
        ("--shard-durations", LIFECYCLE_SMOKE_DURATIONS),
    ):
        assert tokens.count(flag) == 1 and [flag, value] in pairs, (
            f"lifecycle-smoke must select one shard with {flag} {value}"
        )
    assert not any(token.startswith(("--splits", "--group")) for token in tokens)
    assert not any(
        token.startswith(("--deselect", "--ignore", "--maxfail")) or token == "-k"
        for token in tokens
    ), "lifecycle-smoke must not narrow the required selection"
    assert "-m" in tokens
    marker_index = tokens.index("-m")
    assert tokens[marker_index + 1] == LIFECYCLE_SMOKE_REQUIRED_EXPRESSION
    targets = [token for token in tokens if token.startswith("tests/")]
    assert targets == list(LIFECYCLE_SMOKE_ROOTS), (
        f"lifecycle-smoke must bound marker collection to {LIFECYCLE_SMOKE_ROOTS!r}"
    )


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
            *LIFECYCLE_SMOKE_ROOTS,
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
            *(
                [
                    "-p",
                    "scripts.ci_sharding",
                    "--shard-count",
                    str(len(LIFECYCLE_SMOKE_SHARD_JOBS)),
                    "--shard-index",
                    str(shard),
                    "--shard-durations",
                    LIFECYCLE_SMOKE_DURATIONS,
                ]
                if shard is not None
                else []
            ),
            "-m",
            expression,
            *LIFECYCLE_SMOKE_ROOTS,
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
    assert merge_group < full, (
        "merge-group lifecycle selection must be a strict subset of full; "
        f"{sorted(merge_group - full)[:5]} carry {LIFECYCLE_SMOKE_MERGE_GROUP_MARKER} "
        f"but are not collected by -m {LIFECYCLE_SMOKE_FULL_EXPRESSION}. Fix: the auto-add in "
        f"{LIFECYCLE_MARKER_IMPLICATION_HOOK} was removed or bypassed; restore it."
    )
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
        "lifecycle-smoke needs its measured 15-minute whole-job budget, "
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
        ("-p scripts.ci_sharding", ""),
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
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace(before, after)
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
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace(
        LIFECYCLE_SMOKE_REQUIRED_EXPRESSION,
        LIFECYCLE_SMOKE_FULL_EXPRESSION,
    )

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_marker_expression_substitution_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace(
        LIFECYCLE_SMOKE_REQUIRED_EXPRESSION,
        "lifecycle_smoke or lifecycle_merge_group",
    )

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_unbounded_root_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace(" ".join(LIFECYCLE_SMOKE_ROOTS), "tests")

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


# ---------------------------------------------------------------------------
# Merge-time gate liveness (#4116): merge-gate.yml's merge_group
# EXPECTED_CHECKS is the merge-time authority. Every name it waits for must
# be produced by a job that actually runs on merge_group, ci-integration.yml
# must shard with the build-release planner and stay secret-safe, the global
# coverage floor must gate the merge, and a scheduled alarm must fail when
# main moves without the queue.
# ---------------------------------------------------------------------------
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
MERGE_GATE_WAIT_STEP = "Wait for all required checks"
MERGE_GROUP_EVENT = "merge_group"
MERGE_TIME_TRIGGERS = frozenset({MERGE_GROUP_EVENT, "workflow_dispatch", "pull_request"})
MERGE_TIME_PLAN_JOB = "integration-plan"
MERGE_TIME_NETWORK_JOB = "integration-network"
MERGE_TIME_FAN_IN_JOB = "integration-tests"
MERGE_TIME_PLAN_STEP = "Plan shards"
MERGE_TIME_NETWORK_STEP = "Run network-dependent integration tests"
MERGE_TIME_SHARD_MATRIX = "${{ fromJSON(needs.integration-plan.outputs.shards) }}"
MERGE_TIME_SHARD_COUNT = "--shard-count ${{ needs.integration-plan.outputs.count }}"
MERGE_TIME_MAX_SHARDS = "16"
PLANNER_MARK_ENV = ("HERMETIC_MARK_EXPR", "NETWORK_MARK_EXPR")
GLOBAL_COVERAGE_CHECK = "Coverage Combine (Linux)"
GLOBAL_COVERAGE_JOB = "coverage-combine"
GLOBAL_COVERAGE_STEP = "Enforce unit coverage gate"
FRESHNESS_WORKFLOW = "merge-queue-freshness.yml"
FRESHNESS_JOB = "freshness"
FRESHNESS_STEP = "Require a recent merge_group run"
FRESHNESS_SCRIPT = "scripts/merge_queue_freshness.py"
FRESHNESS_WINDOW_DAYS = "3"
_EXPECTED_CHECKS_EXPRESSION = re.compile(
    r"^\$\{\{\s*github\.event_name == 'merge_group' && '([^']+)' \|\| "
    r"contains\(github\.event\.pull_request\.labels\.\*\.name, 'full-integration'\) "
    r"&& '([^']+)' \|\| '([^']+)'\s*\}\}$"
)
_MATRIX_REFERENCE = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")


def _expected_checks(merge_gate: WorkflowNode) -> dict[str, tuple[str, ...]]:
    """Split merge-gate.yml's EXPECTED_CHECKS into its two event contexts."""
    wait_step = workflow_step(workflow_job(merge_gate, "gate"), MERGE_GATE_WAIT_STEP)
    expression = wait_step["env"]["EXPECTED_CHECKS"]
    match = _EXPECTED_CHECKS_EXPRESSION.match(str(expression).strip())
    assert match, f"EXPECTED_CHECKS must branch on merge_group explicitly: {expression!r}"
    contexts = {
        MERGE_GROUP_EVENT: tuple(name.strip() for name in match.group(1).split(",")),
        "pull_request_labeled": tuple(name.strip() for name in match.group(2).split(",")),
        "pull_request": tuple(name.strip() for name in match.group(3).split(",")),
    }
    for event, names in contexts.items():
        assert all(names), f"{event} EXPECTED_CHECKS contains an empty name"
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        assert not duplicates, f"{event} EXPECTED_CHECKS repeats {duplicates}"
    return contexts


def _workflow_events(workflow: WorkflowNode) -> frozenset[str]:
    triggers = workflow.get("on")
    if isinstance(triggers, str):
        return frozenset({triggers})
    if isinstance(triggers, list):
        return frozenset(str(event) for event in triggers)
    if isinstance(triggers, dict):
        return frozenset(str(event) for event in triggers)
    return frozenset()


def _runs_on_merge_group(job: WorkflowNode) -> bool:
    """Only conditions known to let the job run on merge_group count.

    No condition, `always()`, the `full-integration` label gate, or
    `always() && (<label gate>)`. Anything else (`${{ false }}`,
    `github.event_name == 'workflow_dispatch'`, an unknown expression) is not
    a merge_group producer, because a skipped check would still pass the gate.
    """
    condition = str(job.get("if", "")).strip()
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = condition[3:-2].strip()
    return condition in {
        "",
        "always()",
        FULL_INTEGRATION_GATE,
        f"always() && ({FULL_INTEGRATION_GATE})",
    }


def _check_names(job_id: str, job: WorkflowNode) -> tuple[str, ...]:
    """Expand a job's check-run names over a static matrix; dynamic ones expand to nothing."""
    name = str(job.get("name", job_id))
    references = _MATRIX_REFERENCE.findall(name)
    if not references:
        return (name,)
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return ()
    values: list[list[object]] = []
    for reference in references:
        axis = matrix.get(reference)
        if not isinstance(axis, list):
            return ()
        values.append(axis)
    names = []
    for combination in product(*values):
        rendered = name
        for reference, value in zip(references, combination, strict=True):
            rendered = _MATRIX_REFERENCE.sub(
                lambda m, ref=reference, val=value: str(val) if m.group(1) == ref else m.group(0),
                rendered,
            )
        names.append(rendered)
    return tuple(names)


def _merge_group_producers(
    workflows: dict[str, WorkflowNode],
) -> dict[str, list[str]]:
    """Every check-run name a merge_group event produces, with its producers."""
    producers: dict[str, list[str]] = {}
    for workflow_name, workflow in sorted(workflows.items()):
        if MERGE_GROUP_EVENT not in _workflow_events(workflow):
            continue
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job, dict) or not _runs_on_merge_group(job):
                continue
            for check_name in _check_names(job_id, job):
                producers.setdefault(check_name, []).append(f"{workflow_name}:{job_id}")
    return producers


def _assert_merge_group_checks_are_produced(
    merge_gate: WorkflowNode, workflows: dict[str, WorkflowNode]
) -> None:
    """The merge-time gate may only wait for checks merge_group really runs."""
    contexts = _expected_checks(merge_gate)
    merge_time = contexts[MERGE_GROUP_EVENT]
    producers = _merge_group_producers(workflows)
    assert "gate" not in merge_time, "the gate would deadlock waiting for itself"
    missing = sorted(name for name in merge_time if name not in producers)
    assert not missing, (
        f"merge_group EXPECTED_CHECKS waits for checks no merge_group job produces: {missing}"
    )
    ambiguous = {name: producers[name] for name in merge_time if len(producers[name]) > 1}
    assert not ambiguous, f"merge_group checks produced by more than one job: {ambiguous}"
    weaker = sorted(
        (set(contexts["pull_request"]) | set(contexts["pull_request_labeled"])) - set(merge_time)
    )
    assert not weaker, f"PR-time required checks missing from the merge-time gate: {weaker}"


def _assert_merge_time_integration_authority(
    integration: WorkflowNode, release: WorkflowNode
) -> None:
    """ci-integration.yml shards with the build-release planner on merge_group."""
    events = _workflow_events(integration)
    assert events == MERGE_TIME_TRIGGERS, (
        f"ci-integration.yml must trigger on exactly {sorted(MERGE_TIME_TRIGGERS)}; "
        f"got {sorted(events)} (secrets must never reach pull_request code)"
    )
    for key in PLANNER_MARK_ENV:
        assert integration["env"][key] == release["env"][key], (
            f"{key} must match build-release.yml so both lanes partition the same corpus"
        )

    plan_step = workflow_step(workflow_job(integration, MERGE_TIME_PLAN_JOB), MERGE_TIME_PLAN_STEP)
    plan = " ".join(shell_tokens(plan_step))
    for needle in (
        "-p scripts.ci_sharding",
        "--shard-plan-out",
        "--shard-workers 4",
        f"--shard-max {MERGE_TIME_MAX_SHARDS}",
        "-m $HERMETIC_MARK_EXPR",
    ):
        assert needle in plan, f"merge-time shard plan lost {needle!r}"

    shard_job = workflow_job(integration, MERGE_GROUP_INTEGRATION_JOB)
    assert shard_job["strategy"]["matrix"]["shard"] == MERGE_TIME_SHARD_MATRIX
    assert MERGE_TIME_PLAN_JOB in shard_job["needs"]
    shard_step = workflow_step(shard_job, MERGE_GROUP_INTEGRATION_STEP)
    shard_env = effective_env(integration, shard_job, shard_step)
    assert shard_env["PYTEST_MARK_EXPR"] == release["env"]["HERMETIC_MARK_EXPR"]
    shard_args = " ".join(str(shard_env["PYTEST_EXTRA_ARGS"]).split())
    for needle in (MERGE_TIME_SHARD_COUNT, "--shard-index ${{ matrix.shard }}", "--cov"):
        assert needle in shard_args, f"merge-time shard lost {needle!r}"

    network_job = workflow_job(integration, MERGE_TIME_NETWORK_JOB)
    network_step = workflow_step(network_job, MERGE_TIME_NETWORK_STEP)
    network_env = effective_env(integration, network_job, network_step)
    assert network_env["PYTEST_MARK_EXPR"] == release["env"]["NETWORK_MARK_EXPR"]

    fan_in = workflow_job(integration, MERGE_TIME_FAN_IN_JOB)
    assert str(fan_in.get("if", "")).startswith("always() && ")
    assert set(fan_in["needs"]) >= {
        MERGE_TIME_PLAN_JOB,
        MERGE_GROUP_INTEGRATION_JOB,
        MERGE_TIME_NETWORK_JOB,
    }
    aggregate = workflow_step(fan_in, "Aggregate shard results")
    for job_id in (MERGE_TIME_PLAN_JOB, MERGE_GROUP_INTEGRATION_JOB, MERGE_TIME_NETWORK_JOB):
        assert f"needs.{job_id}.result" in str(aggregate["run"]), (
            f"the integration fan-in ignores {job_id}"
        )


def _assert_global_coverage_floor_gates_merge(merge_gate: WorkflowNode, ci: WorkflowNode) -> None:
    """The pyproject.toml floor is enforced by a check the merge-time gate requires."""
    with PYPROJECT.open("rb") as handle:
        floor = tomllib.load(handle)["tool"]["coverage"]["report"]["fail_under"]
    assert GLOBAL_COVERAGE_CHECK in _expected_checks(merge_gate)[MERGE_GROUP_EVENT]
    job = workflow_job(ci, GLOBAL_COVERAGE_JOB)
    assert job["name"] == GLOBAL_COVERAGE_CHECK
    step = workflow_step(job, GLOBAL_COVERAGE_STEP)
    assert_exact_command(
        shell_commands(step),
        ["coverage", "report", f"--fail-under={floor}"],
        label=GLOBAL_COVERAGE_STEP,
    )


def _assert_merge_queue_freshness_alarm(freshness: WorkflowNode) -> None:
    """A schedule fails when main moves but no merge_group run completed."""
    assert {"schedule", "workflow_dispatch"} <= _workflow_events(freshness)
    job = workflow_job(freshness, FRESHNESS_JOB)
    assert_unconditional(job, label=FRESHNESS_JOB)
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    step = workflow_step(job, FRESHNESS_STEP)
    assert_unconditional(step, label=FRESHNESS_STEP)
    assert "continue-on-error" not in step
    assert_exact_command(shell_commands(step), ["python3", FRESHNESS_SCRIPT], label=FRESHNESS_STEP)
    env = effective_env(freshness, job, step)
    assert env["WINDOW_DAYS"] == FRESHNESS_WINDOW_DAYS
    assert env["BRANCH"] == "main"


def _load_workflows() -> dict[str, WorkflowNode]:
    return {path.name: load_workflow(path) for path in sorted(WORKFLOWS_DIR.glob("*.yml"))}


@pytest.fixture(scope="module")
def repository_workflows() -> dict[str, WorkflowNode]:
    return _load_workflows()


def test_merge_group_expected_checks_are_produced_by_merge_group_jobs(
    repository_workflows: dict[str, WorkflowNode],
) -> None:
    _assert_merge_group_checks_are_produced(
        repository_workflows["merge-gate.yml"], repository_workflows
    )


def test_merge_time_integration_uses_the_release_planner(
    repository_workflows: dict[str, WorkflowNode],
) -> None:
    _assert_merge_time_integration_authority(
        repository_workflows["ci-integration.yml"], repository_workflows["build-release.yml"]
    )


def test_global_coverage_floor_gates_the_merge(
    repository_workflows: dict[str, WorkflowNode],
) -> None:
    _assert_global_coverage_floor_gates_merge(
        repository_workflows["merge-gate.yml"], repository_workflows["ci.yml"]
    )


def test_merge_queue_freshness_alarm_is_scheduled(
    repository_workflows: dict[str, WorkflowNode],
) -> None:
    _assert_merge_queue_freshness_alarm(repository_workflows[FRESHNESS_WORKFLOW])


@pytest.fixture
def mutable_workflows(repository_workflows: dict[str, WorkflowNode]) -> dict[str, WorkflowNode]:
    return deepcopy(repository_workflows)


def _gate_merge_time_checks(workflows: dict[str, WorkflowNode], names: str) -> None:
    wait_step = workflow_step(
        workflow_job(workflows["merge-gate.yml"], "gate"), MERGE_GATE_WAIT_STEP
    )
    contexts = _expected_checks(workflows["merge-gate.yml"])
    labeled = ",".join(contexts["pull_request_labeled"])
    unlabeled = ",".join(contexts["pull_request"])
    wait_step["env"]["EXPECTED_CHECKS"] = (
        f"${{{{ github.event_name == 'merge_group' && '{names}' || "
        "contains(github.event.pull_request.labels.*.name, 'full-integration') "
        f"&& '{labeled}' || '{unlabeled}' }}}}"
    )


def test_merge_group_check_without_a_producer_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    merge_time = _expected_checks(mutable_workflows["merge-gate.yml"])[MERGE_GROUP_EVENT]
    _gate_merge_time_checks(mutable_workflows, ",".join((*merge_time, "Phantom Check (Linux)")))

    with pytest.raises(AssertionError, match="no merge_group job produces"):
        _assert_merge_group_checks_are_produced(
            mutable_workflows["merge-gate.yml"], mutable_workflows
        )


def test_merge_group_check_from_a_pull_request_only_workflow_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    del mutable_workflows["ci-integration.yml"]["on"][MERGE_GROUP_EVENT]

    with pytest.raises(AssertionError, match="no merge_group job produces"):
        _assert_merge_group_checks_are_produced(
            mutable_workflows["merge-gate.yml"], mutable_workflows
        )


def test_merge_group_check_skipped_by_a_job_condition_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    job = workflow_job(mutable_workflows["ci-integration.yml"], "release-validation")
    job["if"] = "github.event_name == 'workflow_dispatch'"

    with pytest.raises(AssertionError, match="no merge_group job produces"):
        _assert_merge_group_checks_are_produced(
            mutable_workflows["merge-gate.yml"], mutable_workflows
        )


@pytest.mark.parametrize("condition", ["${{ false }}", "false", "github.repository == 'x/y'"])
def test_merge_group_check_from_an_always_false_producer_fails(
    mutable_workflows: dict[str, WorkflowNode], condition: str
) -> None:
    workflow_job(mutable_workflows["ci-integration.yml"], "release-validation")["if"] = condition

    with pytest.raises(AssertionError, match="no merge_group job produces"):
        _assert_merge_group_checks_are_produced(
            mutable_workflows["merge-gate.yml"], mutable_workflows
        )


def test_merge_group_check_from_a_dynamic_matrix_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    merge_time = _expected_checks(mutable_workflows["merge-gate.yml"])[MERGE_GROUP_EVENT]
    shard_check = "Integration Tests Shard 1 (Linux)"
    _gate_merge_time_checks(mutable_workflows, ",".join((*merge_time, shard_check)))

    with pytest.raises(AssertionError, match="no merge_group job produces"):
        _assert_merge_group_checks_are_produced(
            mutable_workflows["merge-gate.yml"], mutable_workflows
        )


def test_merge_group_check_with_two_producers_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    jobs = mutable_workflows["ci-integration.yml"]["jobs"]
    jobs["shadow-build"] = deepcopy(jobs["build"])

    with pytest.raises(AssertionError, match="more than one job"):
        _assert_merge_group_checks_are_produced(
            mutable_workflows["merge-gate.yml"], mutable_workflows
        )


def test_pr_time_check_missing_from_merge_time_gate_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    merge_time = _expected_checks(mutable_workflows["merge-gate.yml"])[MERGE_GROUP_EVENT]
    _gate_merge_time_checks(
        mutable_workflows, ",".join(name for name in merge_time if name != "APM Self-Check")
    )

    with pytest.raises(AssertionError, match="PR-time required checks missing"):
        _assert_merge_group_checks_are_produced(
            mutable_workflows["merge-gate.yml"], mutable_workflows
        )


@pytest.mark.parametrize("event", ["pull_request_target", "push"])
def test_merge_time_integration_secret_reaching_trigger_fails(
    mutable_workflows: dict[str, WorkflowNode], event: str
) -> None:
    mutable_workflows["ci-integration.yml"]["on"][event] = {"branches": ["main"]}

    with pytest.raises(AssertionError, match="must trigger on exactly"):
        _assert_merge_time_integration_authority(
            mutable_workflows["ci-integration.yml"], mutable_workflows["build-release.yml"]
        )


def test_merge_time_integration_without_dispatch_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    del mutable_workflows["ci-integration.yml"]["on"]["workflow_dispatch"]

    with pytest.raises(AssertionError, match="must trigger on exactly"):
        _assert_merge_time_integration_authority(
            mutable_workflows["ci-integration.yml"], mutable_workflows["build-release.yml"]
        )


def test_merge_time_static_shard_matrix_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    job = workflow_job(mutable_workflows["ci-integration.yml"], MERGE_GROUP_INTEGRATION_JOB)
    job["strategy"]["matrix"]["shard"] = [1, 2, 3, 4]

    with pytest.raises(AssertionError):
        _assert_merge_time_integration_authority(
            mutable_workflows["ci-integration.yml"], mutable_workflows["build-release.yml"]
        )


def test_merge_time_selection_drift_from_release_planner_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    mutable_workflows["ci-integration.yml"]["env"]["HERMETIC_MARK_EXPR"] = "not live"

    with pytest.raises(AssertionError, match=r"must match build-release\.yml"):
        _assert_merge_time_integration_authority(
            mutable_workflows["ci-integration.yml"], mutable_workflows["build-release.yml"]
        )


def test_merge_time_fan_in_ignoring_the_network_lane_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    fan_in = workflow_job(mutable_workflows["ci-integration.yml"], MERGE_TIME_FAN_IN_JOB)
    step = workflow_step(fan_in, "Aggregate shard results")
    step["run"] = step["run"].replace(f"needs.{MERGE_TIME_NETWORK_JOB}.result", "needs.x.result")

    with pytest.raises(AssertionError, match="ignores integration-network"):
        _assert_merge_time_integration_authority(
            mutable_workflows["ci-integration.yml"], mutable_workflows["build-release.yml"]
        )


def test_global_coverage_floor_drift_fails(mutable_workflows: dict[str, WorkflowNode]) -> None:
    job = workflow_job(mutable_workflows["ci.yml"], GLOBAL_COVERAGE_JOB)
    step = workflow_step(job, GLOBAL_COVERAGE_STEP)
    step["run"] = step["run"].replace("--fail-under=80", "--fail-under=70")

    with pytest.raises(AssertionError, match="must contain exact command"):
        _assert_global_coverage_floor_gates_merge(
            mutable_workflows["merge-gate.yml"], mutable_workflows["ci.yml"]
        )


def test_global_coverage_floor_dropped_from_merge_gate_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    merge_time = _expected_checks(mutable_workflows["merge-gate.yml"])[MERGE_GROUP_EVENT]
    _gate_merge_time_checks(
        mutable_workflows, ",".join(name for name in merge_time if name != GLOBAL_COVERAGE_CHECK)
    )

    with pytest.raises(AssertionError):
        _assert_global_coverage_floor_gates_merge(
            mutable_workflows["merge-gate.yml"], mutable_workflows["ci.yml"]
        )


def test_merge_queue_freshness_alarm_suppressed_fails(
    mutable_workflows: dict[str, WorkflowNode],
) -> None:
    job = workflow_job(mutable_workflows[FRESHNESS_WORKFLOW], FRESHNESS_JOB)
    workflow_step(job, FRESHNESS_STEP)["continue-on-error"] = True

    with pytest.raises(AssertionError):
        _assert_merge_queue_freshness_alarm(mutable_workflows[FRESHNESS_WORKFLOW])


# Opt-in full integration before the queue (#4116): ci-integration.yml runs on
# workflow_dispatch and on PRs labeled `full-integration`; merge-gate.yml
# requires its checks at PR time only when that label is present.
FULL_INTEGRATION_LABEL = "full-integration"
FULL_INTEGRATION_GATE = (
    "github.event_name != 'pull_request' || "
    f"contains(github.event.pull_request.labels.*.name, '{FULL_INTEGRATION_LABEL}')"
)
FULL_INTEGRATION_CHECKS = (
    "Build (Linux)",
    "Smoke Test (Linux)",
    "Integration Tests (Linux)",
    "Release Validation (Linux)",
)
_GATE_CONTEXTS = re.compile(
    r"^\$\{\{ github\.event_name == 'merge_group' && '([^']+)' \|\| "
    r"contains\(github\.event\.pull_request\.labels\.\*\.name, 'full-integration'\) "
    r"&& '([^']+)' \|\| '([^']+)' \}\}$"
)


def _integration_triggers(workflow: WorkflowNode) -> dict:
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    return triggers


def _assert_full_integration_opt_in(integration: WorkflowNode, merge_gate: WorkflowNode) -> None:
    triggers = _integration_triggers(integration)
    assert set(triggers) == {"merge_group", "workflow_dispatch", "pull_request"}, sorted(triggers)
    assert triggers["pull_request"] == {
        "branches": ["main"],
        "types": ["labeled", "synchronize", "reopened"],
    }
    for job_id, job in integration["jobs"].items():
        condition = str(job.get("if", ""))
        assert FULL_INTEGRATION_GATE in condition, (
            f"{job_id} must be skipped on PRs without the {FULL_INTEGRATION_LABEL} label"
        )

    wait = workflow_step(workflow_job(merge_gate, "gate"), "Wait for all required checks")
    match = _GATE_CONTEXTS.match(str(wait["env"]["EXPECTED_CHECKS"]))
    assert match, "EXPECTED_CHECKS must branch on merge_group, then on the label"
    merge_time, labeled, unlabeled = (
        tuple(name.strip() for name in group.split(",")) for group in match.groups()
    )
    assert not set(FULL_INTEGRATION_CHECKS) & set(unlabeled)
    assert labeled == (*unlabeled, *FULL_INTEGRATION_CHECKS)
    assert set(FULL_INTEGRATION_CHECKS) <= set(merge_time)


def test_ci_integration_can_be_dispatched() -> None:
    integration = load_workflow(MERGE_GROUP_INTEGRATION_WORKFLOW)
    assert "workflow_dispatch" in _integration_triggers(integration)


def test_full_integration_label_runs_and_gates_only_labeled_prs() -> None:
    _assert_full_integration_opt_in(
        load_workflow(MERGE_GROUP_INTEGRATION_WORKFLOW),
        load_workflow(REPO_ROOT / ".github" / "workflows" / "merge-gate.yml"),
    )


@pytest.mark.parametrize("mutation", ["ungated-job", "pull-request-target", "unlabeled-gate"])
def test_full_integration_opt_in_drift_fails(mutation: str) -> None:
    integration = deepcopy(load_workflow(MERGE_GROUP_INTEGRATION_WORKFLOW))
    merge_gate = deepcopy(load_workflow(REPO_ROOT / ".github" / "workflows" / "merge-gate.yml"))
    if mutation == "ungated-job":
        del integration["jobs"]["build"]["if"]
    elif mutation == "pull-request-target":
        integration["on"]["pull_request_target"] = {"branches": ["main"]}
    else:
        wait = workflow_step(workflow_job(merge_gate, "gate"), "Wait for all required checks")
        expression = str(wait["env"]["EXPECTED_CHECKS"])
        head, _, unlabeled = expression.rpartition(" || '")
        wait["env"]["EXPECTED_CHECKS"] = f"{head} || 'Build (Linux),{unlabeled}"
    with pytest.raises(AssertionError):
        _assert_full_integration_opt_in(integration, merge_gate)


def _license_review_steps(workflow: WorkflowNode) -> list[WorkflowNode]:
    job = next(iter(workflow["jobs"].values()))
    return [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/dependency-review-action@")
    ]


def _assert_license_review_retries_without_softening(workflow: WorkflowNode) -> None:
    attempts = _license_review_steps(workflow)
    assert len(attempts) == 3, "license policy check runs as exactly 3 attempts"
    assert all(step["with"] == attempts[0]["with"] for step in attempts), (
        "every attempt enforces the same license and severity policy"
    )
    *retries, final = attempts
    assert all(step.get("continue-on-error") is True for step in retries)
    assert "continue-on-error" not in final, "the last attempt must be able to fail the job"
    earlier_failed: list[str] = []
    for step in attempts:
        expected = " && ".join(["github.event_name == 'pull_request'", *earlier_failed])
        assert str(step.get("if")) == expected, (
            f"{step['id']}: runs on every pull request once every earlier attempt failed"
        )
        earlier_failed.append(f"steps.{step['id']}.outcome == 'failure'")


def test_license_review_retries_transient_failures_without_softening_policy() -> None:
    """NOTICE Drift is aggregated by the required gate; an upstream API flake must not block it."""
    workflow = load_workflow(REPO_ROOT / ".github" / "workflows" / "notice-drift.yml")
    _assert_license_review_retries_without_softening(workflow)

    softened = deepcopy(workflow)
    _license_review_steps(softened)[-1]["continue-on-error"] = True
    with pytest.raises(AssertionError):
        _assert_license_review_retries_without_softening(softened)

    weakened = deepcopy(workflow)
    _license_review_steps(weakened)[1]["with"]["fail-on-severity"] = "critical"
    with pytest.raises(AssertionError):
        _assert_license_review_retries_without_softening(weakened)

    disabled = deepcopy(workflow)
    _license_review_steps(disabled)[-1]["if"] += " && false"
    with pytest.raises(AssertionError):
        _assert_license_review_retries_without_softening(disabled)


def _assert_pr_runs_cancel_superseded_pushes_only(workflow: WorkflowNode, name: str) -> None:
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), f"{name}: a PR concurrency group is required"
    group = str(concurrency["group"])
    assert "github.event.pull_request.number" in group, f"{name}: group PR runs per PR"
    assert "github.run_id" in group, f"{name}: non-PR runs keep a unique group"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}", (
        f"{name}: only pull_request runs may be cancelled"
    )


def test_heavy_pr_workflows_spare_the_shared_actions_api_quota() -> None:
    """Gate polling and superseded PR runs share one installation API quota."""
    workflows = REPO_ROOT / ".github" / "workflows"
    for name in ("ci.yml", "codeql.yml"):
        workflow = load_workflow(workflows / name)
        _assert_pr_runs_cancel_superseded_pushes_only(workflow, name)

        unbounded = deepcopy(workflow)
        unbounded["concurrency"]["cancel-in-progress"] = True
        with pytest.raises(AssertionError):
            _assert_pr_runs_cancel_superseded_pushes_only(unbounded, name)

    gate = load_workflow(workflows / "merge-gate.yml")
    poll = [
        step["env"]["POLL_SEC"]
        for job in gate["jobs"].values()
        for step in job.get("steps", [])
        if "POLL_SEC" in step.get("env", {})
    ]
    assert poll and all(int(value) >= 30 for value in poll), poll


def test_merge_group_only_node_is_collected_as_lifecycle_smoke(
    pytester: pytest.Pytester,
) -> None:
    """A node marked only lifecycle_merge_group is selected by -m lifecycle_smoke."""
    pytester.makeini(
        "[pytest]\nmarkers =\n"
        f"    {LIFECYCLE_SMOKE_MARKER}: smoke\n"
        f"    {LIFECYCLE_SMOKE_MERGE_GROUP_MARKER}: merge group\n"
    )
    pytester.makeconftest("from tests.conftest import pytest_collection_modifyitems\n")
    pytester.makepyfile(
        f"""
        import pytest

        @pytest.mark.{LIFECYCLE_SMOKE_MERGE_GROUP_MARKER}
        def test_merge_group_only():
            pass

        def test_unmarked():
            pass
        """
    )
    result = pytester.runpytest(
        "--collect-only", "-q", "--strict-markers", "-m", LIFECYCLE_SMOKE_FULL_EXPRESSION
    )
    result.stdout.fnmatch_lines(["*::test_merge_group_only"])
    result.stdout.no_fnmatch_line("*::test_unmarked")
