"""Semantic contracts for the release pipeline's per-platform lanes.

build-release.yml fans out per platform: each platform's integration starts
when its own binary is built, Linux integration is duration-sharded, network
tests run once in a retriable lane, architecture contracts run once on Linux
x86_64, and the two macOS jobs (never more) run a platform-sensitive subset.
These tests pin those invariants rather than individual flag strings where
possible.
"""

from __future__ import annotations

import itertools
import re
from copy import deepcopy
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

from tests.workflow_contracts import (
    assert_exact_command,
    assert_unconditional,
    effective_env,
    load_workflow,
    shell_commands,
    shell_tokens,
    workflow_job,
    workflow_step,
    workflow_step_index,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-release.yml"
WINDOWS_RELEASE_VALIDATION = ROOT / "scripts" / "windows" / "test-release-validation.ps1"
MACOS_VERSION_TEST_ID = (
    "tests/integration/test_core_smoke.py::TestBinaryStartup::test_apm_version_runs"
)
MACOS_RICH_TABLE_TEST_ID = (
    "tests/integration/test_core_smoke.py::TestBinaryStartup::test_apm_rich_table_runs"
)
WINDOWS_TEST_ID = "tests/integration/test_windows_installer_launchers.py"
RELEASE_BOUND = "needs.release-plan.outputs.mode != 'push'"
RUN_UNIT = "needs.release-plan.outputs.run_unit == 'true'"
FULL = "needs.release-plan.outputs.full == 'true'"
MACOS_JOBS = {
    "build-and-validate-macos-intel": ("macos-15-intel", "apm-darwin-x86_64"),
    "build-and-validate-macos-arm": ("macos-latest", "apm-darwin-arm64"),
}
MACOS_RUN_UNIT = {"build-and-validate-macos-intel": "false", "build-and-validate-macos-arm": "true"}
MACOS_SUBSET_STEP = "Run platform-sensitive integration tests"
HERMETIC = "not live and not requires_github_token and not requires_network_integration"
NETWORK = "(requires_github_token or requires_network_integration) and not live"
ARM = f"{HERMETIC} and not architecture_contract"
MACOS = (
    "(lifecycle_smoke or windows_compat or platform_sensitive) and "
    f"{HERMETIC} and not architecture_contract"
)
MACOS_INTEL = f"(windows_compat or platform_sensitive) and {HERMETIC} and not architecture_contract"
MACOS_SUBSETS = {
    "build-and-validate-macos-intel": MACOS_INTEL,
    "build-and-validate-macos-arm": MACOS,
}
LANES = {
    # job id: (step, marker expression, own build job)
    "integration-linux-x86": ("Run integration tests (Unix)", HERMETIC, "build-linux-x86"),
    "integration-linux-arm": ("Run integration tests (Unix)", ARM, "build-linux-arm"),
    "integration-network": ("Run network-dependent integration tests", NETWORK, "build-linux-x86"),
    "integration-windows": ("Run integration tests (Windows)", None, "build-windows"),
}
BUILD_JOBS = ("build-linux-x86", "build-linux-arm", "build-windows")
RUNTIME_SETUP_STEPS = (
    ("build-linux-x86", "Run smoke tests"),
    ("build-and-validate-macos-intel", MACOS_SUBSET_STEP),
    ("build-and-validate-macos-intel", "Run release validation tests"),
    ("build-and-validate-macos-arm", MACOS_SUBSET_STEP),
    ("build-and-validate-macos-arm", "Run release validation tests"),
    ("integration-linux-x86", "Run integration tests (Unix)"),
    ("integration-linux-arm", "Run integration tests (Unix)"),
    ("integration-network", "Run network-dependent integration tests"),
    ("integration-windows", "Run integration tests (Windows)"),
    ("release-validation", "Run release validation tests (Unix)"),
    ("release-validation-windows", "Run release validation tests (Windows)"),
)
SELECTION_MARKERS = (
    "live",
    "requires_github_token",
    "requires_network_integration",
    "architecture_contract",
    "lifecycle_smoke",
    "windows_compat",
    "platform_sensitive",
)


def _workflow() -> dict:
    return load_workflow(WORKFLOW)


def _selects(expression: str, markers: set[str]) -> bool:
    return Expression.compile(expression).evaluate(lambda name, **kwargs: name in markers)


def _assert_macos_startup_steps(workflow: dict) -> None:
    for job_id, (runner, binary) in MACOS_JOBS.items():
        job = workflow_job(workflow, job_id)
        step = workflow_step(job, "Test macOS non-shell binary startup")
        assert job.get("if") == RELEASE_BOUND
        assert "strategy" not in job, "macOS capacity: one job per architecture, no matrix"
        assert_unconditional(step, label=f"{job_id} startup step")
        assert job["runs-on"] == runner
        assert job["env"]["MACOS_BINARY"] == binary
        assert job["env"]["MACOS_RUN_UNIT"] == MACOS_RUN_UNIT[job_id]
        assert effective_env(workflow, job, step).get("GITHUB_TOKEN") is None
        assert step["env"] == {
            "APM_E2E_TESTS": "1",
            "APM_BINARY_PATH": "${{ github.workspace }}/dist/${{ env.MACOS_BINARY }}/apm",
        }
        tokens = shell_tokens(step)
        assert tokens[:3] == ["test", "-x", "$APM_BINARY_PATH"]
        assert_exact_command(
            shell_commands(step),
            [
                "uv",
                "run",
                "--frozen",
                "pytest",
                MACOS_VERSION_TEST_ID,
                MACOS_RICH_TABLE_TEST_ID,
                "-vv",
                "-ra",
                "--tb=short",
            ],
            label=f"{job_id} startup step",
        )
        assert workflow_step_index(job, "Build binary") < workflow_step_index(
            job,
            "Test macOS non-shell binary startup",
        )
        assert workflow_step_index(
            job,
            "Test macOS non-shell binary startup",
        ) < workflow_step_index(job, "Upload binary as workflow artifact")


def _assert_windows_installer_step(workflow: dict) -> None:
    job = workflow_job(workflow, "build-windows")
    step = workflow_step(job, "Test install.ps1 end-to-end (Windows)")
    assert step.get("if") == f"matrix.platform == 'windows' && {RUN_UNIT}"
    assert effective_env(workflow, job, step).get("GITHUB_TOKEN") is None
    assert step["env"] == {"APM_E2E_TESTS": "1"}
    tokens = shell_tokens(step)
    assert tokens[:4] == ["uv", "run", "--frozen", "pytest"]
    assert WINDOWS_TEST_ID in tokens
    assert "-vv" in tokens
    assert "-ra" in tokens
    assert "--tb=short" in tokens


def _assert_lane_fan_out(workflow: dict) -> None:
    """Every lane waits only for its own platform's binary, never the whole matrix."""
    for job_id, (_step, _expression, build) in LANES.items():
        needs = workflow_job(workflow, job_id)["needs"]
        assert build in needs, f"{job_id} must start from {build}"
        assert not set(BUILD_JOBS) - {build} & set(needs), f"{job_id} waits on other builds"
    for job_id in BUILD_JOBS:
        job = workflow_job(workflow, job_id)
        assert job["needs"] == ["release-plan"]
        assert len(job["strategy"]["matrix"]["include"]) == 1
    assert workflow_job(workflow, "release-validation")["needs"] == [
        "release-plan",
        "build-linux-x86",
        "build-linux-arm",
    ]
    assert workflow_job(workflow, "release-validation-windows")["needs"] == [
        "release-plan",
        "build-windows",
    ]


def _assert_lane_selection(workflow: dict) -> None:
    """Lanes select by marker; hermetic and network lanes partition non-live."""
    assert workflow["env"]["HERMETIC_MARK_EXPR"] == HERMETIC
    assert workflow["env"]["NETWORK_MARK_EXPR"] == NETWORK
    assert workflow["env"]["ARM_MARK_EXPR"] == ARM
    assert workflow["env"]["MACOS_MARK_EXPR"] == MACOS
    for job_id, (step_name, expression, _build) in LANES.items():
        if expression is None:
            continue
        job = workflow_job(workflow, job_id)
        step = workflow_step(job, step_name)
        assert effective_env(workflow, job, step)["PYTEST_MARK_EXPR"] == expression, job_id
        assert not step.get("continue-on-error", False)
    for job_id in MACOS_JOBS:
        job = workflow_job(workflow, job_id)
        step = workflow_step(job, MACOS_SUBSET_STEP)
        assert step.get("if") == FULL
        assert effective_env(workflow, job, step)["PYTEST_MARK_EXPR"] == MACOS_SUBSETS[job_id]
        assert step["env"]["PYTEST_EXTRA_ARGS"] == "-n 4 --dist loadgroup"
        assert step["timeout-minutes"] == 60
        assert_exact_command(
            shell_commands(step), ["uv", "run", "./scripts/test-integration.sh"], label=job_id
        )
    for combo in itertools.product((False, True), repeat=len(SELECTION_MARKERS)):
        markers = {name for name, on in zip(SELECTION_MARKERS, combo, strict=True) if on}
        hermetic, network = _selects(HERMETIC, markers), _selects(NETWORK, markers)
        assert not (hermetic and network), markers
        assert (hermetic or network) == ("live" not in markers), markers
        for subset in (ARM, MACOS, MACOS_INTEL):
            assert not _selects(subset, markers) or hermetic, (subset, markers)
            assert not _selects(subset, markers) or "architecture_contract" not in markers


def _assert_sharded_linux_lanes(workflow: dict) -> None:
    """Linux lanes run the planner's partition with grouped xdist workers."""
    for job_id, plan_key in (("integration-linux-x86", "x86"), ("integration-linux-arm", "arm")):
        job = workflow_job(workflow, job_id)
        assert job["strategy"]["matrix"]["shard"] == (
            "${{ fromJSON(needs.integration-plan.outputs." + plan_key + "_shards) }}"
        )
        assert job["env"]["SHARD_COUNT"] == (
            "${{ needs.integration-plan.outputs." + plan_key + "_count }}"
        )
        assert job["strategy"]["fail-fast"] is False
        assert job["timeout-minutes"] == 30
        step = workflow_step(job, "Run integration tests (Unix)")
        assert step["timeout-minutes"] == 25
        args = re.findall(r"\$\{\{.*?\}\}|\S+", step["env"]["PYTEST_EXTRA_ARGS"])
        assert args[:2] == ["-p", "scripts.ci_sharding"]
        assert args[args.index("--shard-count") + 1] == "${{ env.SHARD_COUNT }}"
        assert args[args.index("--shard-index") + 1] == "${{ matrix.shard }}"
        assert args[args.index("-n") + 1] == "4"
        assert args[args.index("--dist") + 1] == "loadgroup"
        assert args.count("-n") == 1 and args.count("--dist") == 1
    network = workflow_step(
        workflow_job(workflow, "integration-network"), "Run network-dependent integration tests"
    )
    assert network["env"]["PYTEST_RETRY_FAILED"] == "2"
    windows = workflow_step(
        workflow_job(workflow, "integration-windows"), "Run integration tests (Windows)"
    )
    assert windows["timeout-minutes"] == 20


def _assert_publication_gates(workflow: dict) -> None:
    """Only pushed tags publish, and only when every release-bound lane passed."""
    create = workflow_job(workflow, "create-release")
    condition = " ".join(create["if"].split())
    assert condition.startswith("always() && github.ref_type == 'tag' &&")
    assert "needs.release-plan.outputs.publish == 'true'" in condition
    for job_id in (
        "release-plan",
        *BUILD_JOBS,
        *MACOS_JOBS,
        "release-validation",
        "release-validation-windows",
    ):
        assert f"needs.{job_id}.result == 'success'" in condition, job_id
        assert job_id in create["needs"]
    assert (
        "needs.release-plan.outputs.mode == 'promoted' && "
        "needs.integration-tests.result == 'skipped'"
    ) in condition
    for job_id in ("deploy-docs", "gh-aw-compat", "build-pypi-distributions"):
        assert workflow_job(workflow, job_id)["if"].startswith(
            "github.event_name == 'push' && github.ref_type == 'tag'"
        ), job_id


def test_macos_jobs_run_non_shell_binary_startup_after_build() -> None:
    """Both macOS jobs execute the exact generated artifact before upload."""
    _assert_macos_startup_steps(_workflow())


def test_macos_capacity_is_two_jobs_with_no_fan_out() -> None:
    """Scarce macOS runners: exactly one Intel and one ARM job, never a matrix."""
    workflow = _workflow()
    macos = [
        job_id
        for job_id, job in workflow["jobs"].items()
        if str(job.get("runs-on", "")).startswith("macos")
    ]
    assert sorted(macos) == sorted(MACOS_JOBS)
    for job_id in macos:
        assert "strategy" not in workflow["jobs"][job_id]


def test_windows_installer_contract_is_windows_only_and_tokenless() -> None:
    """The Windows E2E has exact gating and no effective repository token."""
    _assert_windows_installer_step(_workflow())


def test_integration_lanes_fan_out_per_platform() -> None:
    _assert_lane_fan_out(_workflow())


def test_lane_selection_partitions_the_non_live_corpus() -> None:
    _assert_lane_selection(_workflow())


def test_linux_lanes_are_duration_sharded_with_grouped_workers() -> None:
    _assert_sharded_linux_lanes(_workflow())


def test_only_verified_tags_publish() -> None:
    _assert_publication_gates(_workflow())


@pytest.mark.parametrize(("job_id", "step_name"), RUNTIME_SETUP_STEPS)
def test_runtime_setup_uses_builtin_github_api_token(
    job_id: str,
    step_name: str,
) -> None:
    """Public runtime metadata must not use the private-module PAT."""
    workflow = _workflow()
    step = workflow_step(workflow_job(workflow, job_id), step_name)
    assert step["env"]["GITHUB_API_TOKEN"] == "${{ github.token }}"
    assert step["env"]["GITHUB_APM_PAT"] == "${{ secrets.GH_CLI_PAT }}"


def test_release_validation_keeps_live_inference_decoupled() -> None:
    """Release gates install runtimes but do not invoke paid live inference."""
    workflow = _workflow()
    for job_id in ("release-validation", "release-validation-windows"):
        job = workflow_job(workflow, job_id)
        for step_name in (
            "Run release validation tests (Unix)",
            "Run release validation tests (Windows)",
        ):
            env = effective_env(workflow, job, workflow_step(job, step_name))
            assert "APM_RUN_INFERENCE_TESTS" not in env
            assert "GITHUB_TOKEN" not in env

    script = WINDOWS_RELEASE_VALIDATION.read_text(encoding="utf-8")
    assert script.count('$env:APM_RUN_INFERENCE_TESTS -eq "1"') >= 2
    assert '$env:APM_RUN_INFERENCE_TESTS -ne "1"' in script
    assert "$testsTotal = 4" in script
    assert "$env:GITHUB_APM_PAT -or $env:GITHUB_TOKEN" in script


@pytest.mark.parametrize(
    ("job_id", "expression"),
    [
        ("integration-linux-x86", "not live"),
        ("integration-linux-x86", ARM),
        ("integration-linux-arm", HERMETIC),
        ("integration-network", "requires_github_token"),
        ("integration-network", HERMETIC),
    ],
)
def test_lane_selection_drift_is_rejected(job_id: str, expression: str) -> None:
    """A lane cannot silently widen, narrow or swap its selection."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, job_id)
    step = workflow_step(job, LANES[job_id][0])
    if job.get("env", {}).get("LANE_MARK_EXPR"):
        job["env"]["LANE_MARK_EXPR"] = expression
    else:
        step["env"]["PYTEST_MARK_EXPR"] = expression

    with pytest.raises(AssertionError):
        _assert_lane_selection(workflow)


@pytest.mark.parametrize("expression", ["not live", HERMETIC, "lifecycle_smoke and not live"])
@pytest.mark.parametrize("scope", ["workflow", "intel"])
def test_macos_subset_widening_or_narrowing_is_rejected(expression: str, scope: str) -> None:
    workflow = deepcopy(_workflow())
    if scope == "workflow":
        workflow["env"]["MACOS_MARK_EXPR"] = expression
    else:
        workflow_job(workflow, "build-and-validate-macos-intel")["env"]["MACOS_MARK_EXPR"] = (
            expression
        )

    with pytest.raises(AssertionError):
        _assert_lane_selection(workflow)


@pytest.mark.parametrize(
    "args",
    [
        "-n 4 --dist loadgroup",
        "-p scripts.ci_sharding --shard-count 4 --shard-index ${{ matrix.shard }} -n 4 --dist loadgroup",
        "-p scripts.ci_sharding --shard-count ${{ env.SHARD_COUNT }} "
        "--shard-index ${{ matrix.shard }} -n 4 --dist load",
        "-p scripts.ci_sharding --shard-count ${{ env.SHARD_COUNT }} "
        "--shard-index ${{ matrix.shard }} -n 8 --dist loadgroup",
    ],
)
def test_linux_shard_argument_drift_is_rejected(args: str) -> None:
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, "integration-linux-x86"), "Run integration tests (Unix)"
    )
    step["env"]["PYTEST_EXTRA_ARGS"] = args

    with pytest.raises(AssertionError):
        _assert_sharded_linux_lanes(workflow)


@pytest.mark.parametrize(
    ("job_id", "needs"),
    [
        (
            "integration-linux-x86",
            ["release-plan", "integration-plan", "build-linux-x86", "build-windows"],
        ),
        ("integration-linux-arm", ["release-plan", "integration-plan", "build-linux-x86"]),
        (
            "integration-windows",
            ["release-plan", "build-linux-x86", "build-linux-arm", "build-windows"],
        ),
    ],
)
def test_whole_matrix_barrier_is_rejected(job_id: str, needs: list[str]) -> None:
    workflow = deepcopy(_workflow())
    workflow_job(workflow, job_id)["needs"] = needs

    with pytest.raises(AssertionError):
        _assert_lane_fan_out(workflow)


@pytest.mark.parametrize(
    "condition",
    [
        "always() && needs.release-plan.outputs.publish == 'true'",
        "always() && github.ref_type == 'tag' && needs.release-plan.outputs.publish == 'true'",
    ],
)
def test_weakened_publication_gate_is_rejected(condition: str) -> None:
    workflow = deepcopy(_workflow())
    workflow_job(workflow, "create-release")["if"] = condition

    with pytest.raises(AssertionError):
        _assert_publication_gates(workflow)


@pytest.mark.parametrize("scope", ("workflow", "job", "step"))
def test_windows_token_scope_mutations_are_rejected(scope: str) -> None:
    """A token inherited from any Actions scope must fail the contract."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, "build-windows")
    step = workflow_step(job, "Test install.ps1 end-to-end (Windows)")
    {"workflow": workflow, "job": job, "step": step}[scope].setdefault("env", {})[
        "GITHUB_TOKEN"
    ] = "secret"

    with pytest.raises(AssertionError):
        _assert_windows_installer_step(workflow)


@pytest.mark.parametrize("job_id", list(MACOS_JOBS))
@pytest.mark.parametrize("scope", ("workflow", "job", "step"))
def test_macos_token_scope_mutations_are_rejected(job_id: str, scope: str) -> None:
    """A token inherited from any Actions scope must fail the macOS contract."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, job_id)
    step = workflow_step(job, "Test macOS non-shell binary startup")
    {"workflow": workflow, "job": job, "step": step}[scope].setdefault("env", {})[
        "GITHUB_TOKEN"
    ] = "secret"

    with pytest.raises(AssertionError):
        _assert_macos_startup_steps(workflow)


def test_windows_linux_gate_mutation_is_rejected() -> None:
    """A Linux condition cannot satisfy the Windows-only platform contract."""
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, "build-windows"),
        "Test install.ps1 end-to-end (Windows)",
    )
    step["if"] = "matrix.platform == 'linux'"

    with pytest.raises(AssertionError):
        _assert_windows_installer_step(workflow)


@pytest.mark.parametrize("job_id", list(MACOS_JOBS))
@pytest.mark.parametrize("mutation", ("disable-job", "disable-step", "on-push", "matrix"))
def test_macos_disabled_or_widened_mutations_are_rejected(job_id: str, mutation: str) -> None:
    """macOS evidence cannot be disabled, moved onto every push, or fanned out."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, job_id)
    step = workflow_step(job, "Test macOS non-shell binary startup")
    if mutation == "disable-job":
        job["if"] = False
    elif mutation == "disable-step":
        step["if"] = False
    elif mutation == "on-push":
        del job["if"]
    else:
        job["strategy"] = {"matrix": {"shard": [1, 2]}}

    with pytest.raises(AssertionError):
        _assert_macos_startup_steps(workflow)


@pytest.mark.parametrize("job_id", list(MACOS_JOBS))
def test_macos_echo_replacement_mutation_is_rejected(job_id: str) -> None:
    """An echo cannot replace the exact frozen pytest invocation."""
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, job_id),
        "Test macOS non-shell binary startup",
    )
    step["run"] = (
        'test -x "$APM_BINARY_PATH"\n'
        "echo uv run --frozen pytest "
        f"{MACOS_VERSION_TEST_ID} {MACOS_RICH_TABLE_TEST_ID} "
        "-vv -ra --tb=short\n"
    )

    with pytest.raises(AssertionError):
        _assert_macos_startup_steps(workflow)


def _assert_linux_audit_witness(workflow: dict) -> None:
    """The existing public binary job must execute the complete isolated family."""
    job = workflow_job(workflow, "pr-binary-smoke")
    step_name = "Test packaged audit workspace identity"
    step = workflow_step(job, step_name)
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["permissions"] == {"contents": "read"}
    assert_unconditional(step, label=step_name)
    assert not step.get("continue-on-error", False)
    assert step["timeout-minutes"] == 5
    assert effective_env(workflow, job, step) == {
        "PYTHON_VERSION": "3.12",
        "APM_BINARY_PATH": "${{ github.workspace }}/dist/apm-linux-x86_64/apm",
    }
    assert workflow_step_index(job, "Build binary") < workflow_step_index(job, step_name)
    assert_exact_command(
        shell_commands(step),
        [
            "uv",
            "run",
            "--frozen",
            "--extra",
            "dev",
            "pytest",
            "tests/integration/test_audit_workspace_identity_lifecycle.py",
            "-vv",
            "-ra",
            "--tb=long",
            "--show-capture=all",
            "--no-showlocals",
        ],
        label=step_name,
    )


def test_linux_binary_job_runs_isolated_audit_witness() -> None:
    """Packaged audit evidence needs no secret-bearing release run."""
    _assert_linux_audit_witness(load_workflow(ROOT / ".github" / "workflows" / "ci.yml"))


@pytest.mark.parametrize(
    ("key", "value"),
    (("if", False), ("continue-on-error", True), ("timeout-minutes", 60), ("run", "echo passed")),
)
def test_linux_audit_witness_bypass_mutation_is_rejected(key: str, value: object) -> None:
    """Disabled, unbounded or fabricated audit evidence must fail the contract."""
    workflow = load_workflow(ROOT / ".github" / "workflows" / "ci.yml")
    step = workflow_step(
        workflow_job(workflow, "pr-binary-smoke"),
        "Test packaged audit workspace identity",
    )
    step[key] = value
    with pytest.raises(AssertionError):
        _assert_linux_audit_witness(workflow)
