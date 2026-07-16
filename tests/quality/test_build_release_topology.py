"""Topology contracts for .github/workflows/build-release.yml.

Guards the measured critical-path optimization that prevents macOS Intel
unit tests from re-running on tag/schedule/dispatch events (where
integration tests already provide stronger macOS-specific signal).

Measured evidence (run 29521519891, workflow_dispatch):
  - macOS Intel unit tests: 418 s  (vs 115 s on Linux for the same suite)
  - macOS Intel critical path (dispatch): unit(7 min) + build(1.3 min) + integration(variable)
  - Optimization saves ~7 min off every dispatch/schedule/tag run.

Correctness preserved:
  - Unit tests still run on push events (fast macOS regression detection).
  - macOS ARM unit tests unchanged (ARM job is already promotion-gated).
  - Both macOS integration and release-validation phases preserved.
  - Windows integration preserved.
  - Secrets boundaries unchanged.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import (
    WorkflowNode,
    load_workflow,
    workflow_job,
    workflow_step,
)

RATCHET_TEST_SCOPE = "repository"

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_RELEASE = REPO_ROOT / ".github" / "workflows" / "build-release.yml"

# Promotion-boundary condition that gates integration+release jobs.
PROMOTION_CONDITION = (
    "github.ref_type == 'tag' || "
    "github.event_name == 'schedule' || "
    "github.event_name == 'workflow_dispatch'"
)

# The inverse: unit tests on macOS Intel run only when NOT at a promotion boundary.
MACOS_INTEL_UNIT_TEST_GUARD = (
    "github.ref_type != 'tag' && "
    "github.event_name != 'schedule' && "
    "github.event_name != 'workflow_dispatch'"
)

MACOS_INTEL_JOB = "build-and-validate-macos-intel"
MACOS_ARM_JOB = "build-and-validate-macos-arm"
INTEGRATION_JOB = "integration-tests"
RELEASE_VALIDATION_JOB = "release-validation"
UNIT_TEST_STEP = "Run unit tests"
RUN_INTEGRATION_STEP = "Run integration tests"


@pytest.fixture
def build_release() -> WorkflowNode:
    return load_workflow(BUILD_RELEASE)


@pytest.fixture
def mutable_build_release() -> WorkflowNode:
    return deepcopy(load_workflow(BUILD_RELEASE))


# ---------------------------------------------------------------------------
# Positive contracts (the real workflow must satisfy these)
# ---------------------------------------------------------------------------


def test_macos_intel_unit_tests_skipped_on_promotion_events(
    build_release: WorkflowNode,
) -> None:
    """macOS Intel unit test step must carry the push-only guard.

    On tag/schedule/dispatch, integration tests run and cover macOS signal
    more reliably than the unit suite (which is ~3.6x slower than Linux for
    the same platform-agnostic tests). Measured savings: ~7 min per release
    run. The guard must be exactly the complement of the promotion condition
    so there is no ambiguous third state.
    """
    job = workflow_job(build_release, MACOS_INTEL_JOB)
    step = workflow_step(job, UNIT_TEST_STEP)
    guard = step.get("if")
    assert guard is not None, (
        f"'{UNIT_TEST_STEP}' in {MACOS_INTEL_JOB} must carry an 'if:' guard to "
        "skip redundant unit-test work on promotion events. "
        "Without it, macOS Intel pays ~7 min extra on every dispatch/schedule/tag run."
    )
    assert guard == MACOS_INTEL_UNIT_TEST_GUARD, (
        f"'{UNIT_TEST_STEP}' guard must be exactly:\n"
        f"  {MACOS_INTEL_UNIT_TEST_GUARD!r}\n"
        f"got:\n  {guard!r}"
    )


def test_macos_arm_job_is_promotion_gated(
    build_release: WorkflowNode,
) -> None:
    """macOS ARM job must remain gated to tag/schedule/dispatch.

    ARM runners have 2-4+ hour queue waits; running on every push would
    block the merge feedback loop. This contract ensures the gate is never
    accidentally removed.
    """
    job = workflow_job(build_release, MACOS_ARM_JOB)
    gate = job.get("if")
    assert gate is not None, (
        f"{MACOS_ARM_JOB} must have a top-level 'if:' gate (ARM runners "
        "are extremely scarce -- 2-4+ hour queue waits are common)"
    )
    assert PROMOTION_CONDITION in gate, (
        f"{MACOS_ARM_JOB} gate must include the promotion condition: "
        f"{PROMOTION_CONDITION!r}, got: {gate!r}"
    )


def test_macos_arm_unit_tests_have_no_extra_guard(
    build_release: WorkflowNode,
) -> None:
    """macOS ARM unit tests must NOT carry the push-only guard.

    Because the ARM job itself is already promotion-gated, adding the
    push-only guard to its unit test step would make ARM unit tests
    unreachable (never run). ARM unit tests should run unconditionally
    within the already-gated job.
    """
    job = workflow_job(build_release, MACOS_ARM_JOB)
    step = workflow_step(job, UNIT_TEST_STEP)
    guard = step.get("if")
    assert guard is None, (
        f"'{UNIT_TEST_STEP}' in {MACOS_ARM_JOB} must NOT carry an 'if:' guard "
        f"(the job is already promotion-gated; a step guard would make it unreachable). "
        f"Got: {guard!r}"
    )


def test_integration_tests_job_is_promotion_gated(
    build_release: WorkflowNode,
) -> None:
    """Downstream integration-tests job must remain promotion-gated."""
    job = workflow_job(build_release, INTEGRATION_JOB)
    gate = job.get("if")
    assert gate is not None, (
        f"{INTEGRATION_JOB} must have a top-level 'if:' gate "
        "(integration tests run only at tag/schedule/dispatch)"
    )
    assert PROMOTION_CONDITION in gate, (
        f"{INTEGRATION_JOB} gate must include the promotion condition: "
        f"{PROMOTION_CONDITION!r}, got: {gate!r}"
    )


def test_release_validation_job_is_promotion_gated(
    build_release: WorkflowNode,
) -> None:
    """Downstream release-validation job must remain promotion-gated."""
    job = workflow_job(build_release, RELEASE_VALIDATION_JOB)
    gate = job.get("if")
    assert gate is not None, f"{RELEASE_VALIDATION_JOB} must have a top-level 'if:' gate"
    assert PROMOTION_CONDITION in gate, (
        f"{RELEASE_VALIDATION_JOB} gate must include the promotion condition: "
        f"{PROMOTION_CONDITION!r}, got: {gate!r}"
    )


def test_macos_intel_integration_phase_is_promotion_gated(
    build_release: WorkflowNode,
) -> None:
    """Integration step within macOS Intel job must remain promotion-gated.

    The macOS Intel consolidated job runs integration inline (not via a
    downstream job) to avoid re-queueing scarce runners. This step-level
    gate is the mechanism that prevents integration work on push events.
    """
    job = workflow_job(build_release, MACOS_INTEL_JOB)
    step = workflow_step(job, RUN_INTEGRATION_STEP)
    guard = step.get("if")
    assert guard is not None, (
        f"'{RUN_INTEGRATION_STEP}' in {MACOS_INTEL_JOB} must have a step-level 'if:' gate"
    )
    assert PROMOTION_CONDITION in guard, (
        f"'{RUN_INTEGRATION_STEP}' gate must include the promotion condition: "
        f"{PROMOTION_CONDITION!r}, got: {guard!r}"
    )


# ---------------------------------------------------------------------------
# Negative (mutation) contracts -- prove the guard is load-bearing
# ---------------------------------------------------------------------------


def test_removing_macos_intel_unit_test_guard_fails(
    mutable_build_release: WorkflowNode,
) -> None:
    """Proves the guard check fails when the 'if:' is silently dropped.

    Without the guard, macOS Intel unit tests would run on dispatch/schedule/tag
    events, wasting ~7 min of macOS Intel runner time on work already covered
    by Linux and the integration suite.
    """
    job = workflow_job(mutable_build_release, MACOS_INTEL_JOB)
    step = workflow_step(job, UNIT_TEST_STEP)
    del step["if"]

    with pytest.raises(AssertionError, match="must carry an 'if:' guard"):
        test_macos_intel_unit_tests_skipped_on_promotion_events(mutable_build_release)


def test_wrong_macos_intel_unit_test_guard_fails(
    mutable_build_release: WorkflowNode,
) -> None:
    """Proves the guard check fails when the condition is subtly wrong.

    Swapping != for == would invert the semantics: unit tests would run
    *only* on dispatch/schedule/tag (where integration tests already cover
    macOS) and skip on push (where fast feedback is needed).
    """
    job = workflow_job(mutable_build_release, MACOS_INTEL_JOB)
    step = workflow_step(job, UNIT_TEST_STEP)
    step["if"] = (
        "github.ref_type == 'tag' && "
        "github.event_name == 'schedule' && "
        "github.event_name == 'workflow_dispatch'"
    )

    with pytest.raises(AssertionError, match="guard must be exactly"):
        test_macos_intel_unit_tests_skipped_on_promotion_events(mutable_build_release)


def test_removing_macos_arm_job_gate_fails(
    mutable_build_release: WorkflowNode,
) -> None:
    """Proves the ARM gate check fails when the top-level 'if:' is removed."""
    job = workflow_job(mutable_build_release, MACOS_ARM_JOB)
    del job["if"]

    with pytest.raises(AssertionError, match="must have a top-level 'if:' gate"):
        test_macos_arm_job_is_promotion_gated(mutable_build_release)


def test_adding_step_guard_to_macos_arm_unit_tests_fails(
    mutable_build_release: WorkflowNode,
) -> None:
    """Proves the ARM unit test unconditional check fails if a guard is added.

    Adding the push-only guard to ARM's unit test step would make it
    unreachable (ARM job runs only at promotion boundaries, so the push-only
    guard is always false within it).
    """
    job = workflow_job(mutable_build_release, MACOS_ARM_JOB)
    step = workflow_step(job, UNIT_TEST_STEP)
    step["if"] = MACOS_INTEL_UNIT_TEST_GUARD

    with pytest.raises(AssertionError, match="must NOT carry an 'if:' guard"):
        test_macos_arm_unit_tests_have_no_extra_guard(mutable_build_release)
