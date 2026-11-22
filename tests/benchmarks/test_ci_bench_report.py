"""The CI bench report pairs base and new samples per runner and per pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tests.benchmarks import ci_bench

pytestmark = pytest.mark.component


def _write(root: Path, shard: int, side: str, pair: int, cpu: float, error: str = "") -> None:
    directory = root / f"perf-bench-shard-{shard}" / f"{side}-{pair:02d}"
    directory.mkdir(parents=True)
    record = {
        "fixture": "tree-10",
        "journey": "install-noop",
        "rtt_ms": 30.0,
        "cpu_p50_s": cpu,
        "traffic": {"requests": 4 if side == "base" else 0},
        "spawns": {"git": 8} if side == "base" else {},
        "modules": 900,
        "error": error,
    }
    (directory / "journeys-abc.json").write_text(json.dumps({"results": [record]}))


def test_paired_ratio_cancels_a_slower_runner(tmp_path: Path) -> None:
    # Runner 2 is twice as slow on both sides; new is exactly half of base everywhere.
    for shard, scale in ((1, 1.0), (2, 2.0)):
        for pair in range(2):
            _write(tmp_path, shard, "base", pair, 3.0 * scale)
            _write(tmp_path, shard, "new", pair, 1.5 * scale)

    (row,) = ci_bench.summarize(tmp_path)

    assert row["paired_new_over_base"] == 0.5
    assert row["base"]["samples"] == row["new"]["samples"] == 4
    assert (row["base"]["requests"], row["new"]["requests"]) == (4, 0)
    assert (row["base"]["spawns"], row["new"]["spawns"]) == (8, 0)


def test_failed_sample_is_excluded_and_fails_the_report(tmp_path: Path, capsys) -> None:
    _write(tmp_path, 1, "base", 0, 3.0)
    _write(tmp_path, 1, "new", 0, 1.0, error="install failed")
    args = argparse.Namespace(
        root=str(tmp_path), json=None, base="a", new="b", shards="1", pairs="1"
    )

    assert ci_bench.report(args) == 1
    (row,) = ci_bench.summarize(tmp_path)
    assert row["new"] == {**row["new"], "samples": 0, "errors": 1, "cpu_p50_s": None}
    assert row["paired_new_over_base"] is None
    assert "| 0/1 |" in capsys.readouterr().out
