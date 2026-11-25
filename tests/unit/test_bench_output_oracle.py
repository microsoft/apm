"""The perf bench flags a cell whose new side deploys different output than base.

A stub that skips work (for example hardlinked package files that the
integrators then refuse to deploy) looks faster on CPU alone; the output
oracle makes that row INVALID instead of a win.
"""

from __future__ import annotations

import io
import json
import tarfile
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


def _shard(root: Path, side: str, output: dict, pair: str = "00") -> None:
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
    run = root / "shard-1" / "runs" / f"{side}-{pair}"
    run.mkdir(parents=True)
    (run / "journeys-x.json").write_text(json.dumps({"results": [record]}))


def test_same_deploy_under_different_work_roots_is_the_same_output(tmp_path):
    base = _capture(tmp_path / "base", generated_at="2026-11-25T07:00:00")
    new = _capture(tmp_path / "new")

    assert output_oracle.compare([("p0", base)], [("p0", new)]) == {"verdict": "same", "files": 2}


def test_a_missing_deployed_file_makes_the_cell_invalid_and_fails_the_report(tmp_path, capsys):
    base = _capture(tmp_path / "base")
    new = _capture(tmp_path / "new", deploy=False)
    shards = tmp_path / "shards"
    _shard(shards, "base", base)
    _shard(shards, "new", new)

    status = ci_bench.main(["report", str(shards), "--shards", "1", "--pairs", "1"])

    table = capsys.readouterr().out
    assert status == 1
    assert "| INVALID (2->1 files) |" in table
    assert "[x] INVALID tree-50 install-cold rtt=30" in table
    assert "missing 1: .github/instructions/a.instructions.md" in table


def test_a_changed_lockfile_alone_makes_the_cell_invalid(tmp_path):
    base = _capture(tmp_path / "base")
    project = tmp_path / "new" / "runs" / "tree" / "project"
    new_work = tmp_path / "new"
    _install(new_work)
    (project / output_oracle.LOCKFILE).write_text("lockfile_version: '1'\ndependencies: []\n")
    new = output_oracle.capture([project], new_work)

    verdict = output_oracle.compare([("p0", base)], [("p0", new)])

    assert verdict["verdict"] == "INVALID"
    assert verdict["lock_differs"] is True
    assert verdict["changed"] == [output_oracle.LOCKFILE]


def test_every_pair_must_match_not_just_the_set_of_outputs(tmp_path):
    full = _capture(tmp_path / "base")
    partial = _capture(tmp_path / "new", deploy=False)

    verdict = output_oracle.compare(
        [("p0", full), ("p1", partial)], [("p0", partial), ("p1", full)]
    )

    assert verdict["verdict"] == "INVALID"
    assert verdict["mismatched_pairs"] == 2


def test_nested_lockfiles_ignore_volatile_keys(tmp_path):
    outputs = []
    for side in ("base", "new"):
        app = tmp_path / side / "apps" / "app-1"
        app.mkdir(parents=True)
        (app / output_oracle.LOCKFILE).write_text(f"apm_version: {side}\ndependencies: []\n")
        outputs.append(output_oracle.capture([tmp_path / side], tmp_path / side))

    assert output_oracle.key(outputs[0]) == output_oracle.key(outputs[1])


def test_pack_archives_compare_members_not_timestamps_or_cli_version(tmp_path):
    outputs = []
    for side, mtime in (("base", 1), ("new", 2)):
        dist = tmp_path / side / "dist"
        dist.mkdir(parents=True)
        with tarfile.open(dist / "bundle.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo("plugin/a.md")
            info.size, info.mtime = 1, mtime
            archive.addfile(info, io.BytesIO(b"x"))
            lock = f"pack:\n  packed_at: '{mtime}'\n  apm_version: v{mtime}\n".encode()
            entry = tarfile.TarInfo("plugin/apm.lock.yaml")
            entry.size = len(lock)
            archive.addfile(entry, io.BytesIO(lock))
        outputs.append(output_oracle.capture([dist], tmp_path / side, lock_dirs=[]))

    assert output_oracle.key(outputs[0]) == output_oracle.key(outputs[1])


def test_pooled_runs_keep_each_pairs_evidence(tmp_path):
    full = _capture(tmp_path / "base")
    partial = _capture(tmp_path / "new", deploy=False)
    pooled = tmp_path / "pooled"
    _shard(pooled / "run-a", "base", full)
    _shard(pooled / "run-a", "new", partial)
    _shard(pooled / "run-b", "base", full)
    _shard(pooled / "run-b", "new", full)

    (row,) = ci_bench.summarize(pooled)

    assert row["output"]["verdict"] == "INVALID"
    assert row["output"]["mismatched_pairs"] == 1


def test_a_pair_without_output_on_one_side_is_incomplete(tmp_path, capsys):
    full = _capture(tmp_path / "base")
    shards = tmp_path / "shards"
    _shard(shards, "base", full)
    _shard(shards, "new", full)
    _shard(shards, "base", full, pair="01")
    _shard(shards, "new", {}, pair="01")

    status = ci_bench.main(["report", str(shards), "--shards", "1", "--pairs", "2"])

    assert status == 1
    assert "INCOMPLETE (1/2 pairs uncaptured)" in capsys.readouterr().out
