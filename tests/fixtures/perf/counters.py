"""Deterministic performance counters for one ``apm`` command: counts, never time.

A lifecycle test (or the bench) runs a command with :func:`counted_env`, then
reads :func:`read_spawn_log`. The audit hook in
``tests/benchmarks/spawn_counter/sitecustomize.py`` records, summed over
every Python process of the command:

- ``spawns``: processes by executable (``git.exe`` counts as ``git``);
- ``git_verbs``: git spawns by subcommand (``-c``/``-C``/``--git-dir`` values skipped);
- ``modules``: the most modules one process imported;
- ``fs`` (with ``fs=True``): ``files_written``, ``lockfile_reads``,
  ``realpath_calls``, ``stat_calls``, ``lstat_calls``, ``hash_objects``,
  ``hashed_bytes``.

HTTP requests, round trips and connects come from the offline world
(``tests.fixtures.perf.serve_offline``) the command talks to.

The ``fs`` wrappers cost CPU: never compare timings taken with them.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SPAWN_COUNTER_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "spawn_counter"
_TABLES = ("spawns", "git_verbs", "fs")


def counted_env(env: dict[str, str], log: Path, *, fs: bool = True) -> dict[str, str]:
    """*env* with the counters on, appending one JSON line per process to *log*."""
    counted = dict(env)
    counted["APM_PERF_SPAWN_LOG"] = str(log)
    path = counted.get("PYTHONPATH")
    counted["PYTHONPATH"] = (
        str(SPAWN_COUNTER_DIR) if not path else os.pathsep.join((str(SPAWN_COUNTER_DIR), path))
    )
    if fs:
        counted["APM_PERF_COUNT_FS"] = "1"
    return counted


def read_spawn_log(path: Path | None) -> dict:
    """The counters summed over every process that wrote *path*; the log is removed."""
    counters: dict = {table: {} for table in _TABLES} | {"modules": 0}
    if path is None or not path.is_file():
        return counters
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        counters["modules"] = max(counters["modules"], record["modules"])
        for table in _TABLES:
            for name, count in record.get(table, {}).items():
                counters[table][name] = counters[table].get(name, 0) + count
    path.unlink()
    for table in _TABLES:
        counters[table] = dict(sorted(counters[table].items()))
    return counters


def run_counted(
    argv: list[str], *, cwd: Path, env: dict[str, str], log: Path, fs: bool = True
) -> tuple[subprocess.CompletedProcess, dict]:
    """Run *argv* with the counters on; return the result and its counters."""
    log.unlink(missing_ok=True)
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=counted_env(env, log, fs=fs),
        capture_output=True,
        text=True,
        check=False,
    )
    return result, read_spawn_log(log)
