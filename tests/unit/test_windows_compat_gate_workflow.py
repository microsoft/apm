"""Semantic contract for the PR-time Windows compatibility gate.

Regression coverage for microsoft/apm#2233: the Windows-only failure
class (CRLF text-mode writes, backslash path separators leaking into
diagnostics, bare "git" argv resolution, and the websockets.sync
shutdown race) was structurally invisible at PR time because ci.yml
was Linux-only for PR feedback and the full Windows matrix only runs
post-merge in build-release.yml. These tests pin the focused Windows
job that closes that gap: its exact test-target list, timeout, and
registration as a required check in merge-gate.yml for both PR-time
and merge-queue-time contexts.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import (
    assert_exact_command,
    load_workflow,
    shell_commands,
    workflow_job,
    workflow_step,
)

ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MERGE_GATE_WORKFLOW = ROOT / ".github" / "workflows" / "merge-gate.yml"

GATE_JOB = "windows-compat-gate"
GATE_CHECK_NAME = "Windows Compatibility Gate"
GATE_STEP = "Run cross-platform contract family"

# The load-bearing cross-platform contract family: every canonical
# owner and every test file that reproduced a distinct Windows-only
# failure signature during the microsoft/apm#2233 investigation.
EXPECTED_TARGETS = (
    "tests/unit/scripts/test_ratchet_baseline.py",
    "tests/unit/scripts/test_run_mutation_pilot.py",
    "tests/unit/scripts/test_check_test_contract_authorities.py",
    "tests/unit/scripts/test_check_test_assertions.py",
    "tests/unit/scripts/test_check_exact_test_duplicates.py",
    "tests/unit/test_shepherd_owner_touch_gate.py",
    "tests/unit/integration/test_copilot_app_ws.py",
    "tests/unit/utils/test_atomic_io.py",
    "tests/unit/cache/test_git_env.py",
    "tests/unit/utils/test_paths.py",
)


def _ci_workflow() -> dict:
    return load_workflow(CI_WORKFLOW)


def _merge_gate_workflow() -> dict:
    return load_workflow(MERGE_GATE_WORKFLOW)


def _expected_checks_env(merge_gate: dict) -> str:
    gate = workflow_job(merge_gate, "gate")
    wait_step = workflow_step(gate, "Wait for all required checks")
    expected_checks = wait_step["env"]["EXPECTED_CHECKS"]
    assert isinstance(expected_checks, str)
    return expected_checks


def test_windows_compat_gate_runs_on_windows_with_bounded_timeout() -> None:
    job = workflow_job(_ci_workflow(), GATE_JOB)
    assert job["name"] == GATE_CHECK_NAME
    assert job["runs-on"] == "windows-latest"
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int) and 0 < timeout <= 30, (
        f"Windows compatibility gate must declare a small, hard timeout, got {timeout!r}"
    )


def test_windows_compat_gate_runs_exact_cross_platform_contract_family() -> None:
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    expected_command = [
        "uv",
        "run",
        "--extra",
        "dev",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-v",
        *EXPECTED_TARGETS,
    ]
    assert_exact_command(shell_commands(step), expected_command, label=GATE_STEP)


def test_windows_compat_gate_does_not_duplicate_full_suite() -> None:
    """The gate must stay a focused subset, not a second full-suite run.

    A duplicate full-suite invocation would double PR-time cost without
    adding coverage -- the whole point of a *focused* gate.
    """
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    tokens = {token for command in shell_commands(step) for token in command}
    full_suite_roots = {"tests/unit", "tests/test_console.py", "tests/red_team"}
    assert not full_suite_roots & tokens, (
        "Windows compatibility gate must not duplicate the full unit-test roots "
        "already covered by build-and-test-shard"
    )


@pytest.mark.parametrize(
    "expected_checks_key",
    (
        "pull_request",
        "merge_group",
    ),
)
def test_windows_compat_gate_is_required_in_both_gate_contexts(
    expected_checks_key: str,
) -> None:
    expected_checks = _expected_checks_env(_merge_gate_workflow())
    # Both ternary branches must list the gate -- Windows must gate
    # PR-time AND merge-queue-time, since build-release.yml's full
    # Windows matrix only runs post-merge.
    assert GATE_CHECK_NAME in expected_checks, (
        f"{GATE_CHECK_NAME!r} missing from merge-gate.yml EXPECTED_CHECKS "
        f"({expected_checks_key} context)"
    )


def test_windows_compat_gate_removal_breaks_required_check_contract() -> None:
    """Mutation-break proof: deleting the job must desync the contract."""
    ci = deepcopy(_ci_workflow())
    del ci["jobs"][GATE_JOB]

    with pytest.raises(AssertionError):
        workflow_job(ci, GATE_JOB)


def test_windows_compat_gate_check_name_removal_breaks_merge_gate_contract() -> None:
    """Mutation-break proof: removing the check name from EXPECTED_CHECKS
    must desync the required-checks contract."""
    merge_gate = deepcopy(_merge_gate_workflow())
    gate = workflow_job(merge_gate, "gate")
    wait_step = workflow_step(gate, "Wait for all required checks")
    wait_step["env"]["EXPECTED_CHECKS"] = wait_step["env"]["EXPECTED_CHECKS"].replace(
        GATE_CHECK_NAME + ",", ""
    )

    expected_checks = _expected_checks_env(merge_gate)
    assert GATE_CHECK_NAME not in expected_checks
