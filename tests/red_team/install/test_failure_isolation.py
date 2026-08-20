"""Vector 2 -- failure isolation under chaos.

A batch of three command scripts where the MIDDLE one is broken in some
way (non-zero exit, NUL byte, missing cwd, timeout). The OTHER two must
still run and fire() must never raise. One bad script must never poison
the batch or abort the install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    LifecycleScriptRunner,
    PackageInfo,
)

from .conftest import PYEXE, make_command_entry, touch_cmd


def _event() -> LifecycleEvent:
    return LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory="/tmp",
    )


def _run_batch(middle, apm_home: Path, tmp_path: Path):
    """Fire [good1, middle, good3]; return (sentinel1, sentinel3, raised)."""
    s1 = tmp_path / "S1"
    s3 = tmp_path / "S3"
    scripts = [
        make_command_entry(touch_cmd(s1)),
        middle,
        make_command_entry(touch_cmd(s3)),
    ]
    runner = LifecycleScriptRunner(scripts=scripts)
    raised = False
    try:
        runner.fire("post-install", _event())
    except Exception:
        raised = True
    return s1, s3, raised


def test_nonzero_exit_in_middle_isolated(apm_home: Path, tmp_path: Path) -> None:
    middle = make_command_entry(f'{PYEXE} -c "import sys; sys.exit(3)"')
    s1, s3, raised = _run_batch(middle, apm_home, tmp_path)
    assert s1.exists() and s3.exists(), "neighbours must run despite non-zero exit"
    assert not raised, "fire() must not raise on a failing script"


def test_nul_byte_command_isolated(apm_home: Path, tmp_path: Path) -> None:
    """A NUL byte makes subprocess.run raise ValueError -- must be caught."""
    middle = make_command_entry("echo " + chr(0) + " boom")
    s1, s3, raised = _run_batch(middle, apm_home, tmp_path)
    assert s1.exists() and s3.exists(), "neighbours must run despite NUL-byte script"
    assert not raised, "fire() must not raise on an embedded-NUL command"


def test_missing_cwd_isolated(apm_home: Path, tmp_path: Path) -> None:
    """cwd pointing at a nonexistent dir raises in subprocess -- isolated."""
    missing = tmp_path / "does-not-exist"
    middle = make_command_entry("echo hi", cwd=str(missing))
    s1, s3, raised = _run_batch(middle, apm_home, tmp_path)
    assert s1.exists() and s3.exists(), "neighbours must run despite bad cwd"
    assert not raised, "fire() must not raise on a nonexistent cwd"


def test_raising_executor_isolated(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if execute_script itself raises, fire()'s try/except contains it."""
    import apm_cli.core.script_executors as ex

    real = ex.execute_script
    calls = {"n": 0}

    def boom(script, event, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # the middle script
            raise RuntimeError("injected executor explosion")
        return real(script, event, **kw)

    monkeypatch.setattr(ex, "execute_script", boom)

    middle = make_command_entry("echo middle")
    s1, s3, raised = _run_batch(middle, apm_home, tmp_path)
    assert s1.exists() and s3.exists(), "neighbours must run despite executor raising"
    assert not raised, "fire() must swallow an executor-level exception"


@pytest.mark.slow
def test_timeout_in_middle_isolated(apm_home: Path, tmp_path: Path) -> None:
    """A timing-out middle script must not block the neighbours."""
    middle = make_command_entry(f'{PYEXE} -c "import time; time.sleep(10)"', timeout_sec=1)
    s1, s3, raised = _run_batch(middle, apm_home, tmp_path)
    assert s1.exists() and s3.exists(), "neighbours must run despite a timeout"
    assert not raised, "fire() must not raise on a timeout"
