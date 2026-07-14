from __future__ import annotations

import os
import sys
from pathlib import Path

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner


def test_default_runner_executes_real_apm_subprocess(tmp_path: Path) -> None:
    result = ApmLifecycleRunner().run(
        ("--help",),
        cwd=tmp_path,
        env=os.environ,
    )

    assert result.command[:3] == (sys.executable, "-m", "apm_cli.cli")
    assert result.returncode == 0
    assert result.cwd == tmp_path


def test_runner_passes_explicit_cwd_and_environment(tmp_path: Path) -> None:
    runner = ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            ("import os, pathlib; pathlib.Path('observed.txt').write_text(os.environ['OBSERVED'])"),
        )
    )

    result = runner.run(
        (),
        cwd=tmp_path,
        env={**os.environ, "OBSERVED": "isolated"},
    )

    assert result.returncode == 0
    assert (tmp_path / "observed.txt").read_text(encoding="utf-8") == "isolated"


def test_run_sequence_preserves_order_and_results(tmp_path: Path) -> None:
    runner = ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            ("import pathlib, sys; pathlib.Path('sequence.txt').open('a').write(sys.argv[1])"),
        )
    )

    results = runner.run_sequence(
        (("A",), ("B",), ("C",)),
        cwd=tmp_path,
        env=os.environ,
    )

    assert [result.returncode for result in results] == [0, 0, 0]
    assert (tmp_path / "sequence.txt").read_text(encoding="utf-8") == "ABC"
