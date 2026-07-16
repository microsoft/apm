"""Semantic contract for the PR-time Windows compatibility gate.

Regression coverage for microsoft/apm#2233: the Windows-only failure
class (CRLF text-mode writes, backslash path separators leaking into
diagnostics, bare "git" argv resolution, and the websockets.sync
shutdown race) was structurally invisible at PR time because ci.yml
was Linux-only for PR feedback and the full Windows matrix only runs
post-merge in build-release.yml. These tests pin the focused Windows
job that closes that gap.

Selection is declarative: the job runs `pytest -m windows_compat`
over the narrowest maintainable root (`tests/unit`) rather than
enumerating test files in this workflow. Adding a new Windows-relevant
regression test therefore only requires applying the `windows_compat`
marker (see pyproject.toml `[tool.pytest.ini_options].markers`) to the
test -- not editing ci.yml or this file. These tests assert the
*shape* of that contract (marker-scoped, bounded, non-empty, required,
non-duplicative) instead of pinning an exact file list, so they do not
need to change every time a test gains or loses the marker.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import (
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
GATE_MARKER = "windows_compat"

# The full-suite roots already covered by build-and-test-shard (Linux).
# The gate must never invoke these WITHOUT a marker filter -- that
# would silently regress into a duplicate full-suite run.
FULL_SUITE_ROOTS = ("tests/unit", "tests/test_console.py", "tests/red_team")

# A generous but real ceiling: the gate is a "load-bearing contract
# family", not a second full-suite run. If the marked set ever grows
# past this, that is a signal to re-examine scope, not to raise the
# ceiling reflexively.
MAX_BOUNDED_FAMILY_SIZE = 200


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


def _gate_pytest_command(step: dict) -> list[str]:
    """Return the one shell command in the step that invokes pytest."""
    matches = [command for command in shell_commands(step) if "pytest" in command]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one pytest invocation in step {step.get('name')!r}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _gate_pytest_args(step: dict) -> list[str]:
    """Extract pytest's own CLI arguments (marker + roots), stripped of
    the `uv run --extra dev` invocation prefix and the `-v` verbosity
    flag (irrelevant to selection semantics, and it would otherwise
    leak into a --collect-only re-invocation)."""
    command = _gate_pytest_command(step)
    index = command.index("pytest")
    return [arg for arg in command[index + 1 :] if arg != "-v"]


# Flags that take a value as the following token -- needed to tell
# "-p" and "-m"'s VALUES apart from genuine positional test-path
# arguments when computing the selected root(s).
_VALUE_FLAGS = ("-p", "-m")


def _positional_test_paths(args: list[str]) -> list[str]:
    """Return only the positional (non-flag, non-flag-value) arguments,
    i.e. the test paths/roots pytest will actually collect from."""
    positional: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _VALUE_FLAGS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        positional.append(arg)
    return positional


def _collect_gate_family(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Collect the declared gate family without loading unrelated plugins."""
    collection_env = os.environ.copy()
    collection_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q", *args],
        cwd=ROOT,
        env=collection_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_windows_compat_gate_runs_on_windows_with_bounded_timeout() -> None:
    job = workflow_job(_ci_workflow(), GATE_JOB)
    assert job["name"] == GATE_CHECK_NAME
    assert job["runs-on"] == "windows-latest"
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int) and 0 < timeout <= 30, (
        f"Windows compatibility gate must declare a small, hard timeout, got {timeout!r}"
    )


def test_windows_compat_gate_selects_tests_via_registered_marker() -> None:
    """The gate must select tests declaratively via `-m windows_compat`,
    not by enumerating file paths in the workflow.

    This is the core anti-pattern guard: a future edit that reverts to
    a hardcoded file list (functionally equivalent to the old
    EXPECTED_TARGETS enumeration, just moved rather than removed) must
    fail this test.
    """
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    args = _gate_pytest_args(step)

    assert "-m" in args, (
        f"{GATE_STEP!r} must select tests via a pytest -m marker expression, got args: {args!r}"
    )
    marker_index = args.index("-m")
    assert marker_index + 1 < len(args) and args[marker_index + 1] == GATE_MARKER, (
        f"{GATE_STEP!r} must select tests via `-m {GATE_MARKER}`, got: {args!r}"
    )


def test_windows_compat_gate_runs_over_narrowest_maintainable_root() -> None:
    """The gate's positional pytest arguments must be exactly the
    narrowest root that contains every `windows_compat`-marked test
    (`tests/unit`), not the repo-wide `tests/` root and not a
    per-file enumeration."""
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    args = _gate_pytest_args(step)
    positional = _positional_test_paths(args)
    assert positional == ["tests/unit"], (
        f"{GATE_STEP!r} must scope to exactly the narrowest maintainable "
        f"root ['tests/unit'], got: {positional!r}"
    )


def test_windows_compat_gate_does_not_duplicate_full_suite() -> None:
    """The gate must stay a marker-scoped subset, not a second full-suite
    run.

    A bare `tests/unit` (or `tests/test_console.py` / `tests/red_team`)
    invocation with no marker filter would duplicate build-and-test-shard's
    Linux coverage at double the PR-time cost with no new signal.
    """
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    command = _gate_pytest_command(step)
    tokens = set(command)

    overlapping_roots = set(FULL_SUITE_ROOTS) & tokens
    assert overlapping_roots, "sanity: gate should scope at least one known root"
    assert "-m" in tokens, (
        "Windows compatibility gate invokes a full-suite root "
        f"({sorted(overlapping_roots)}) without a `-m` marker filter -- "
        "this duplicates build-and-test-shard instead of running a "
        "focused, marker-scoped subset"
    )


def test_windows_compat_gate_marker_selects_nonempty_bounded_family() -> None:
    """The gate's own declared invocation must collect a real,
    non-empty, bounded set of tests when actually run.

    This proves the marker is wired to live test code (not just
    declared in the workflow with nothing behind it) and that the
    contract family stays a *focused* subset rather than silently
    growing into a second full-suite run.
    """
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    args = _gate_pytest_args(step)
    result = _collect_gate_family(args)
    assert result.returncode == 0, (
        f"collection failed for the gate's own declared invocation "
        f"(args={args!r}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    match = re.search(r"(\d+)(?:/\d+)?\s+tests?\s+collected", result.stdout)
    assert match, f"could not parse a collected-test count from:\n{result.stdout}"
    collected = int(match.group(1))
    assert 0 < collected <= MAX_BOUNDED_FAMILY_SIZE, (
        f"expected a non-empty, bounded {GATE_MARKER!r} contract family "
        f"(1..{MAX_BOUNDED_FAMILY_SIZE}), got {collected}"
    )


def test_nested_collection_disables_plugin_autoload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested collection must not import unrelated third-party plugins."""
    captured_env: dict[str, str] | None = None

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal captured_env
        env = kwargs.get("env")
        captured_env = env if isinstance(env, dict) else None
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout="1 test collected\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    _collect_gate_family(["-m", GATE_MARKER, "tests/unit"])

    assert captured_env is not None
    assert captured_env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    assert captured_env.get("PATH") == os.environ.get("PATH")


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


def test_dropping_marker_filter_is_detected_as_full_suite_duplication() -> None:
    """Mutation-break proof: if a future edit drops `-m windows_compat`
    from the run step while leaving `tests/unit` in place, the
    duplicate-full-suite guard must catch it."""
    ci = deepcopy(_ci_workflow())
    job = workflow_job(ci, GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    step["run"] = step["run"].replace(f"-m {GATE_MARKER}\n", "").replace(f"-m {GATE_MARKER}", "")

    command = _gate_pytest_command(step)
    tokens = set(command)
    overlapping_roots = set(FULL_SUITE_ROOTS) & tokens
    assert overlapping_roots and "-m" not in tokens, (
        "mutation setup sanity check failed -- marker was not actually removed"
    )


def test_narrowing_root_below_windows_compat_root_breaks_contract() -> None:
    """Mutation-break proof: if the declared root no longer matches
    `tests/unit`, the narrowest-root contract test must fail."""
    ci = deepcopy(_ci_workflow())
    job = workflow_job(ci, GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    step["run"] = step["run"].replace("tests/unit", "tests/unit/scripts")

    args = _gate_pytest_args(step)
    positional = [arg for arg in _positional_test_paths(args) if arg != GATE_MARKER]
    assert positional != ["tests/unit"], (
        "mutation setup sanity check failed -- root was not actually narrowed"
    )
