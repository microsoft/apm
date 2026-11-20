"""Process-state isolation between tests on one xdist worker.

Regression for the Windows units (1/3) failure on main (run 44705583099):
``test_output_mode.py`` runs ``cli audit --format=json`` in-process, which
routes console output to stderr for the whole process. Only the integration
conftest reset that routing, so a later unit test on the same worker
(``test_config_default_target_provenance_names_config_source``) captured empty
stdout. The root conftest now resets console routing for every suite, and the
leak detector (``tests/utils/state_leaks.py``) fails any test that leaves
tracked process state changed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.utils import console, git_env
from tests.utils import state_leaks

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
POLLUTER = (
    "tests/unit/core/test_output_mode.py::"
    "test_failing_machine_output_keeps_update_notice_off_stdout"
)
VICTIM = (
    "tests/unit/install/phases/test_targets_phase.py::"
    "test_config_default_target_provenance_names_config_source"
)


def test_machine_output_polluter_then_stdout_victim_pass_on_one_worker() -> None:
    """Run the polluter, then the victim, in one process and in that order."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-n",
            "0",
            "-q",
            POLLUTER,
            VICTIM,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "2 passed" in result.stdout


def _key(module: str, name: str) -> str:
    return f"{module}.{name}"


def test_detector_names_env_cwd_and_tracked_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    before = state_leaks.take_snapshot()
    monkeypatch.setenv("APM_LEAK_PROBE", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x (teardown)")
    monkeypatch.delenv("PATH")
    moved = state_leaks.take_snapshot()
    values = dict(moved.values)
    values[_key("apm_cli.utils.console", "_console_stderr")] = True
    values[_key("apm_cli.config", "_config_cache")] = {"default_client": "x"}
    after = state_leaks.Snapshot(moved.env, "/elsewhere", values)

    assert state_leaks.leaked_keys(before, after) == [
        "env:APM_LEAK_PROBE",
        "env:PATH",
        "cwd",
        "apm_cli.utils.console._console_stderr",
        "apm_cli.config._config_cache",
    ]


def test_lazy_executable_memo_may_fill_but_never_change() -> None:
    key = _key("apm_cli.utils.git_env", "_git_executable")
    before = state_leaks.take_snapshot()
    unset = state_leaks.Snapshot(before.env, before.cwd, {**before.values, key: None})
    filled = state_leaks.Snapshot(before.env, before.cwd, {**before.values, key: "/usr/bin/git"})
    faked = state_leaks.Snapshot(before.env, before.cwd, {**before.values, key: "/tmp/fake/git"})

    assert state_leaks.leaked_keys(unset, filled) == []
    assert state_leaks.leaked_keys(filled, faked) == [key]


def test_restore_undoes_every_tracked_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # monkeypatch keeps the outer detector clean if an assertion fails midway.
    monkeypatch.setenv("APM_LEAK_PROBE_KEEP", "1")
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(console, "_console_stderr", False)
    monkeypatch.setattr(git_env, "_git_executable", "/usr/bin/git")
    before = state_leaks.take_snapshot()

    os.environ["APM_LEAK_PROBE"] = "1"
    del os.environ["APM_LEAK_PROBE_KEEP"]
    os.chdir(tmp_path)
    console.set_console_stderr(True)
    git_env._git_executable = "/tmp/fake/git"
    assert state_leaks.leaked_keys(before, state_leaks.take_snapshot())

    state_leaks.restore(before)

    assert state_leaks.leaked_keys(before, state_leaks.take_snapshot()) == []
    assert "APM_LEAK_PROBE" not in os.environ
    assert console._console_stderr is False


@pytest.mark.parametrize(
    ("leaks", "baseline", "expected"),
    [
        pytest.param([], {}, None, id="clean"),
        pytest.param(["cwd"], {}, "leaked process state ['cwd']", id="new-leak"),
        pytest.param(["cwd"], {"t": frozenset({"cwd"})}, None, id="baselined"),
        pytest.param(
            ["cwd", "env:X"], {"t": frozenset({"cwd"})}, "['env:X']", id="baselined-grows"
        ),
        pytest.param([], {"t": frozenset({"cwd"})}, "stale state-leak baseline", id="fixed"),
        pytest.param(
            ["cwd"], {"t": frozenset({"cwd", "env:X"})}, "stale state-leak", id="partly-fixed"
        ),
    ],
)
def test_baseline_only_shrinks(
    leaks: list[str], baseline: dict[str, frozenset[str]], expected: str | None
) -> None:
    message = state_leaks.verdict("t", leaks, baseline)
    if expected is None:
        assert message is None
    else:
        assert message is not None and expected in message


def test_committed_baseline_is_well_formed() -> None:
    payload = json.loads(state_leaks.BASELINE_PATH.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    baseline = state_leaks.load_baseline()
    assert len(baseline) <= state_leaks.BASELINE_CEILING, "the leak baseline may only shrink"
    for node, keys in baseline.items():
        assert "::" in node and keys, node
        assert state_leaks.applies_to(node), f"{node} is outside the detector's scope"


def test_detector_wraps_every_other_function_autouse_fixture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detector must judge state after the resets and monkeypatch undo."""
    names = request.fixturenames
    detector = names.index("_apm_state_leak_detector")
    for other in (
        "_reset_console_state",
        "_reset_config_cache",
        "_reset_marketplace_registry_cache",
        "_isolate_discovery_state",
        "monkeypatch",
    ):
        assert detector < names.index(other), other
    function_scoped = [
        name
        for name in names
        if name != "request"
        and (defs := request._fixturemanager.getfixturedefs(name, request.node))
        and defs[-1].scope == "function"
    ]
    assert function_scoped[0] == "_apm_state_leak_detector"
