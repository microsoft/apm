from __future__ import annotations

import shlex
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
import tomllib

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

# Lifecycle Smoke (Linux): the smallest stable hermetic subset of the real
# Consume/Produce/Govern lifecycle contracts, promoted to a PR-time required
# check. See ci.yml's lifecycle-smoke job comment for full rationale.
#
# Selection is declarative via the `lifecycle_smoke` pytest marker (see
# pyproject.toml's [tool.pytest.ini_options].markers), not an explicit
# file/node-id list. The marker is applied at the source: module-level
# `pytestmark` on the four lifecycle-contract modules, and a single
# function-level `@pytest.mark.lifecycle_smoke` on the one #2226 AC14
# static guard inside test_architecture_authorities.py (that module's
# other 32 tests are deliberately NOT marked). This intentionally means
# no target list lives here or in any other test constant/manifest --
# the marker registration + a live collection count are the only source
# of truth, so a maintainer adding/removing a lifecycle_smoke-marked test
# anywhere under tests/integration changes the required job's scope
# without ever touching this file or ci.yml.
LIFECYCLE_SMOKE_JOB = "lifecycle-smoke"
LIFECYCLE_SMOKE_CHECK = "Lifecycle Smoke (Linux)"
LIFECYCLE_SMOKE_RUN_STEP = "Run required lifecycle smoke subset"
LIFECYCLE_SMOKE_MAX_TIMEOUT_MINUTES = 5
LIFECYCLE_SMOKE_MARKER = "lifecycle_smoke"
LIFECYCLE_SMOKE_ROOT = "tests/integration"
PYPROJECT = REPO_ROOT / "pyproject.toml"
FORBIDDEN_CREDENTIAL_ENV = ("GITHUB_APM_PAT", "ADO_APM_PAT", "GITHUB_TOKEN")
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


def _workflow_uv_sync_commands(root: Path) -> list[tuple[str, str, str, list[str]]]:
    """Collect every first-party workflow command that invokes ``uv sync``."""
    commands: list[tuple[str, str, str, list[str]]] = []
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
            for step in _run_steps(job):
                step_name = str(step.get("name", "<unnamed>"))
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                logical = run.replace("\\\n", " ")
                for line in logical.splitlines():
                    if "uv sync" not in line or line.lstrip().startswith("#"):
                        continue
                    try:
                        tokens = shlex.split(line, comments=True, posix=True)
                    except ValueError as exc:
                        location = f"{workflow_path.name}:{job_name}:{step_name}"
                        raise AssertionError(f"{location}: invalid shell line {line!r}") from exc
                    for index in range(len(tokens) - 1):
                        if tokens[index : index + 2] != ["uv", "sync"]:
                            continue
                        commands.append(
                            (
                                workflow_path.name,
                                str(job_name),
                                step_name,
                                tokens[index:],
                            )
                        )
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


def test_first_party_workflow_uv_syncs_are_frozen() -> None:
    """The committed lockfile governs every GitHub Actions environment."""
    sync_commands = _workflow_uv_sync_commands(REPO_ROOT)
    assert sync_commands
    unfrozen = [
        f"{workflow}:{job}:{step}: {' '.join(tokens)}"
        for workflow, job, step, tokens in sync_commands
        if "--frozen" not in tokens
    ]
    assert unfrozen == [], "unfrozen first-party workflow syncs:\n" + "\n".join(unfrozen)


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
        _workflow_uv_sync_commands(tmp_path)


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


def _assert_lifecycle_smoke_command(job: WorkflowNode) -> None:
    """The run step must select declaratively via the `lifecycle_smoke`
    marker expression, pass `--strict-markers`, and bound collection to
    `tests/integration` -- never the bare `tests` root or full repo.
    Dropping the marker expression (or widening the root) would silently
    make this job re-run the entire integration suite inside the PR-time
    critical path, duplicating ci-integration.yml's merge_group-only job
    rather than staying the smallest stable hermetic subset."""
    step = workflow_step(job, LIFECYCLE_SMOKE_RUN_STEP)
    for tokens in shell_commands(step):
        if "pytest" not in tokens:
            continue
        assert "--strict-markers" in tokens, (
            "lifecycle-smoke must pass --strict-markers so an unregistered "
            "or typo'd marker name fails loudly instead of silently "
            "selecting zero tests"
        )
        assert "-m" in tokens, (
            "lifecycle-smoke must select tests declaratively via a marker "
            "expression (-m), not explicit file/node-id targets"
        )
        marker_index = tokens.index("-m")
        assert marker_index + 1 < len(tokens), "lifecycle-smoke's -m flag is missing its value"
        assert tokens[marker_index + 1] == LIFECYCLE_SMOKE_MARKER, (
            f"lifecycle-smoke's -m expression must be exactly {LIFECYCLE_SMOKE_MARKER!r}, "
            f"got {tokens[marker_index + 1]!r}"
        )
        targets = [token for token in tokens if token.startswith("tests/")]
        assert targets == [LIFECYCLE_SMOKE_ROOT], (
            f"lifecycle-smoke must bound collection to exactly {LIFECYCLE_SMOKE_ROOT!r} "
            f"(never the full repo or bare tests/) -- got {targets!r}"
        )
        return
    raise AssertionError("lifecycle-smoke run step contains no pytest invocation")


def _assert_lifecycle_smoke_hermetic(job: WorkflowNode) -> None:
    assert "secrets" not in job

    # Job-level `env:` is inherited by every step in GitHub Actions, so a
    # credential bound there would bypass a step-only check (panel review
    # of #2247: python-architect, supply-chain-security-expert, and
    # test-coverage-expert independently found and proved this gap).
    job_env = job.get("env")
    if isinstance(job_env, dict):
        for key in FORBIDDEN_CREDENTIAL_ENV:
            assert key not in job_env, f"lifecycle-smoke job-level env must not bind {key}"

    for step in _run_steps(job):
        env = step.get("env")
        if isinstance(env, dict):
            for key in FORBIDDEN_CREDENTIAL_ENV:
                assert key not in env, f"lifecycle-smoke step must not bind {key}"
        run = step.get("run")
        if isinstance(run, str):
            for key in FORBIDDEN_CREDENTIAL_ENV:
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


def _assert_lifecycle_smoke_collection_non_empty() -> None:
    """Proves the marker-based family is non-empty right now: a real
    `--collect-only` subprocess run against the exact CI invocation. If
    every `@pytest.mark.lifecycle_smoke` were stripped from the four
    modules and the AC14 function, pytest would exit 5 ("no tests
    collected") -- exactly what the CI job itself would do, per
    test_lifecycle_smoke_marker_family_empty_fails below."""
    _assert_marker_collection_non_empty(LIFECYCLE_SMOKE_MARKER)


def test_lifecycle_smoke_is_required_and_hermetic() -> None:
    """Pins the new PR-time gate as a semantic contract, not an exact
    target list: the marker is registered, the command selects
    declaratively via that marker within a bounded root, the job stays
    hermetic and required, and the marker family actually collects tests
    right now. This is the positive half of the guard -- the mutation
    tests below prove it actually catches drift rather than trivially
    passing."""
    ci = load_workflow(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    job = workflow_job(ci, LIFECYCLE_SMOKE_JOB)
    assert job["name"] == LIFECYCLE_SMOKE_CHECK
    assert job["runs-on"] == "ubuntu-24.04"

    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int)
    assert 0 < timeout <= LIFECYCLE_SMOKE_MAX_TIMEOUT_MINUTES, (
        "lifecycle-smoke must keep a hard, tight timeout so a regression "
        "toward slowness (e.g. an accidental network retry loop) fails "
        "fast and loud rather than silently eating the PR-time budget"
    )

    _assert_lifecycle_smoke_marker_registered(_pytest_ini_markers())
    _assert_lifecycle_smoke_command(job)
    _assert_lifecycle_smoke_hermetic(job)
    _assert_lifecycle_smoke_collection_non_empty()

    merge_gate = load_workflow(REPO_ROOT / ".github" / "workflows" / "merge-gate.yml")
    _assert_lifecycle_smoke_required(merge_gate)


@pytest.fixture
def provisional_lifecycle_job(provisional_ci: WorkflowNode) -> WorkflowNode:
    return workflow_job(provisional_ci, LIFECYCLE_SMOKE_JOB)


def test_lifecycle_smoke_marker_expression_dropped_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    """Proves the guard fails if the `-m lifecycle_smoke` marker
    expression is silently dropped -- without it, this job would
    silently re-run the ENTIRE tests/integration suite inside the
    PR-time critical path, duplicating ci-integration.yml's
    merge_group-only job and blowing the 60-90s time budget."""
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace("-m lifecycle_smoke ", "")

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_wrong_marker_expression_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    """Proves the guard fails if the marker expression is swapped for a
    different, broader, pre-existing marker (e.g. `integration`) --
    which would still select *some* tests but silently change this job's
    curated scope away from the lifecycle_smoke family."""
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace("lifecycle_smoke", "integration")

    with pytest.raises(AssertionError):
        _assert_lifecycle_smoke_command(provisional_lifecycle_job)


def test_lifecycle_smoke_unbounded_root_fails(
    provisional_lifecycle_job: WorkflowNode,
) -> None:
    """Proves the guard fails if the collection root is silently widened
    from tests/integration to the full tests/ tree -- even with the
    marker expression intact, collecting over the entire suite adds
    unbounded, unbudgeted collection overhead to every PR."""
    step = workflow_step(provisional_lifecycle_job, LIFECYCLE_SMOKE_RUN_STEP)
    step["run"] = step["run"].replace("tests/integration", "tests")

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
