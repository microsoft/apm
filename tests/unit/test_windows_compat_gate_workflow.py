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
_COLLECTION_ENV_BASELINE = os.environ.copy()

GATE_JOB = "windows-compat-gate"
GATE_CHECK_NAME = "Windows Compatibility Gate"
GATE_STEP = "Run cross-platform contract family"
GATE_MARKER = "windows_compat"

# The full-suite roots already covered by build-and-test-shard (Linux).
# The gate must never invoke these WITHOUT a marker filter -- that
# would silently regress into a duplicate full-suite run.
FULL_SUITE_ROOTS = ("tests/unit", "tests/test_console.py", "tests/red_team")


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
_VALUE_FLAGS = ("-p", "-m", "-n", "--dist")


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
    collection_env = _COLLECTION_ENV_BASELINE.copy()
    collection_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "--color=no",
            "-q",
            *args,
        ],
        cwd=ROOT,
        env=collection_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _without_execution_only_args(args: list[str]) -> list[str]:
    """Strip scheduling flags that are irrelevant to collect-only selection."""
    stripped: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-n", "--numprocesses", "--dist"}:
            skip_next = True
            continue
        if arg.startswith(("--numprocesses=", "--dist=")):
            continue
        stripped.append(arg)
    return stripped


def _assert_bounded_loadgroup_parallelism(args: list[str]) -> None:
    assert "-n" in args, "Windows compatibility gate must declare bounded xdist workers"
    worker_index = args.index("-n")
    assert args[worker_index : worker_index + 2] == ["-n", "2"], (
        "Windows compatibility gate must use exactly two xdist workers"
    )
    assert "--dist" in args, "Windows compatibility gate must declare an xdist scheduler"
    dist_index = args.index("--dist")
    assert args[dist_index : dist_index + 2] == ["--dist", "loadgroup"], (
        "Windows compatibility gate must use loadgroup so xdist_group fixture affinity is honored"
    )
    assert "--numprocesses=auto" not in args and "-nauto" not in args, (
        "Windows compatibility gate must not use unbounded automatic worker selection"
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
    with one explicit integration contract outside the unit-test root.

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
    """Run the unit root plus the one load-bearing subprocess integration contract."""
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    args = _gate_pytest_args(step)
    positional = _positional_test_paths(args)
    expected = ["tests/unit", "tests/integration/test_lifecycle_workspace_lock.py"]
    assert positional == expected, (
        f"{GATE_STEP!r} must scope to the unit contracts and lifecycle subprocess "
        f"contract {expected!r}, got: {positional!r}"
    )


def test_windows_compat_gate_uses_bounded_loadgroup_parallelism() -> None:
    """The required gate may parallelize, but only with bounded worker count
    and the scheduler that preserves any tests carrying xdist_group affinity."""
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    _assert_bounded_loadgroup_parallelism(_gate_pytest_args(step))


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


def _assert_gate_family_collection(result: subprocess.CompletedProcess[str]) -> None:
    """Require live marker selection that does not select the entire collected suite."""
    assert result.returncode == 0, (
        f"collection failed for the gate's own declared invocation "
        f"(args={result.args!r}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    match = re.search(r"^(\d+)(?:/(\d+))?\s+tests?\s+collected\b", result.stdout, re.MULTILINE)
    assert match, f"could not parse a collected-test count from:\n{result.stdout}"
    selected = int(match.group(1))
    # Pytest omits the denominator when every collected test is selected.
    total = int(match.group(2)) if match.group(2) is not None else selected
    assert 0 < selected < total, (
        f"expected a non-empty {GATE_MARKER!r} strict subset, "
        f"got {selected} selected out of {total} collected tests"
    )


def test_windows_compat_gate_marker_selects_nonempty_subset() -> None:
    """Allow marked regressions to grow without duplicating the full collected suite.

    The workflow's root and timeout guards bound scope and runtime, not an
    arbitrary test-count ceiling that breaks when legitimate coverage grows.
    """
    job = workflow_job(_ci_workflow(), GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    _assert_gate_family_collection(
        _collect_gate_family(_without_execution_only_args(_gate_pytest_args(step)))
    )


@pytest.mark.parametrize(
    "summary",
    [
        pytest.param("1/2 test collected\n", id="single-test"),
        pytest.param("351/1000 tests collected\n", id="former-ceiling"),
        pytest.param("1000/10000 tests collected\n", id="family-growth"),
        pytest.param(
            "tests/unit/test_example.py::test_summary[200 tests collected]\n"
            "351/1000 tests collected\n",
            id="ignore-node-id-text",
        ),
    ],
)
def test_gate_collection_accepts_growing_proper_subsets(summary: str) -> None:
    """Growth past the old ceiling is valid while marker selection stays narrower."""
    _assert_gate_family_collection(subprocess.CompletedProcess([], 0, summary, ""))


@pytest.mark.parametrize(
    ("summary", "returncode", "message"),
    [
        pytest.param("0/1000 tests collected\n", 0, "strict subset", id="empty-selection"),
        pytest.param("351 tests collected\n", 0, "strict subset", id="entire-suite"),
        pytest.param("351/351 tests collected\n", 0, "strict subset", id="equal-counts"),
        pytest.param("351/350 tests collected\n", 0, "strict subset", id="invalid-counts"),
        pytest.param("unexpected output\n", 0, "could not parse", id="missing-summary"),
        pytest.param(
            "tests/unit/test_example.py::test_summary[1/2 tests collected]\n",
            0,
            "could not parse",
            id="node-id-is-not-summary",
        ),
        pytest.param("no tests collected\n", 5, "collection failed", id="collection-failure"),
    ],
)
def test_gate_collection_rejects_invalid_selections(
    summary: str, returncode: int, message: str
) -> None:
    """Empty, unfiltered, malformed and failed collection cannot satisfy the gate."""
    with pytest.raises(AssertionError, match=message):
        _assert_gate_family_collection(subprocess.CompletedProcess([], returncode, summary, ""))


def test_nested_collection_disables_plugin_autoload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested collection must not import unrelated third-party plugins."""
    expected_path = os.environ.get("PATH")
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
    monkeypatch.delenv("PATH", raising=False)

    _collect_gate_family(["-m", GATE_MARKER, "tests/unit"])

    assert captured_env is not None
    assert captured_env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    assert captured_env.get("PATH") == expected_path


@pytest.mark.parametrize(
    "parallel_args",
    [
        "",
        "-n 0 --dist loadgroup",
        "-n auto --dist loadgroup",
        "-n 3 --dist loadgroup",
        "-n 2 --dist worksteal",
        "-n 2 --dist load",
        "-n 2",
        "--dist loadgroup",
        "-n 2 --dist loadgroup --numprocesses=auto",
    ],
)
def test_windows_compat_gate_parallelism_drift_fails(parallel_args: str) -> None:
    ci = deepcopy(_ci_workflow())
    job = workflow_job(ci, GATE_JOB)
    step = workflow_step(job, GATE_STEP)
    step["run"] = re.sub(
        r"\s*-n 2 --dist loadgroup\s*",
        f"\n        {parallel_args}\n",
        step["run"],
    )

    with pytest.raises(AssertionError, match=r"xdist|workers|loadgroup|automatic"):
        _assert_bounded_loadgroup_parallelism(_gate_pytest_args(step))


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
