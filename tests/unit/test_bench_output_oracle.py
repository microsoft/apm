"""The perf bench flags a cell whose new side deploys different output than base.

A stub that skips work (for example hardlinked package files that the
integrators then refuse to deploy) looks faster on CPU alone; the output
oracle makes that row INVALID instead of a win.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.benchmarks import ci_bench, output_oracle

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-PERF-bench-same-output")]


def _install(work: Path, *, deploy: bool = True, generated_at: str = "") -> Path:
    project = work / "runs" / "tree" / "project"
    (project / "apm_modules" / "pkg").mkdir(parents=True)
    (project / "apm_modules" / "pkg" / f"layout-{work.name}").write_text("store detail")
    (project / ".apm-local").mkdir()
    (project / ".apm-local" / "state").write_text(work.name)
    if deploy:
        target = project / ".github" / "instructions"
        target.mkdir(parents=True)
        (target / "a.instructions.md").write_text(f"see {work / 'home' / 'x'}\n")
    lock = f"lockfile_version: '1'\napm_version: {work.name}\ndependencies: [pkg]\n"
    if generated_at:
        lock += f"generated_at: '{generated_at}'\n"
    (project / output_oracle.LOCKFILE).write_text(lock)
    return project


def _capture(work: Path, **kwargs) -> dict:
    return output_oracle.capture([_install(work, **kwargs)], work)


def _shard(root: Path, side: str, output: dict) -> None:
    record = {
        "fixture": "tree-50",
        "journey": "install-cold",
        "rtt_ms": 30,
        "cpu_p50_s": 1.0 if side == "base" else 0.8,
        "p50_s": 1.0,
        "traffic": {"requests": 18},
        "spawns": {},
        "output": output,
        "error": "",
    }
    run = root / "shard-1" / "runs" / f"{side}-00"
    run.mkdir(parents=True)
    (run / "journeys-x.json").write_text(json.dumps({"results": [record]}))


def test_same_deploy_under_different_work_roots_is_the_same_output(tmp_path):
    base = _capture(tmp_path / "base", generated_at="2026-11-25T07:00:00")
    new = _capture(tmp_path / "new")

    assert output_oracle.compare([base], [new]) == {"verdict": "same", "files": 1}


def test_a_missing_deployed_file_makes_the_cell_invalid_and_fails_the_report(tmp_path, capsys):
    base = _capture(tmp_path / "base")
    new = _capture(tmp_path / "new", deploy=False)
    shards = tmp_path / "shards"
    _shard(shards, "base", base)
    _shard(shards, "new", new)

    status = ci_bench.main(["report", str(shards), "--shards", "1", "--pairs", "1"])

    table = capsys.readouterr().out
    assert status == 1
    assert "| INVALID (1->0 files) |" in table
    assert "[x] INVALID tree-50 install-cold rtt=30" in table
    assert "missing 1: .github/instructions/a.instructions.md" in table


def test_a_changed_lockfile_alone_makes_the_cell_invalid(tmp_path):
    base = _capture(tmp_path / "base")
    project = tmp_path / "new" / "runs" / "tree" / "project"
    new_work = tmp_path / "new"
    _install(new_work)
    (project / output_oracle.LOCKFILE).write_text("lockfile_version: '1'\ndependencies: []\n")
    new = output_oracle.capture([project], new_work)

    verdict = output_oracle.compare([base], [new])

    assert verdict["verdict"] == "INVALID"
    assert verdict["lock_differs"] is True
    assert not verdict["missing"] and not verdict["changed"]
