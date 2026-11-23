"""Summarize a TIA S0 probe run (#4508) as JSON on stdout.

usage: python -m scripts.tia.report PROBE_DIR WORKER_DATA_GLOB_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from coverage import CoverageData


def _lines(paths: list[Path]) -> tuple[set[str], int]:
    files: set[str] = set()
    count = 0
    for path in paths:
        data = CoverageData(basename=str(path))
        data.read()
        for name in data.measured_files():
            measured = data.lines(name) or []
            if measured:
                files.add(name)
                count += len(measured)
    return files, count


def main(probe_dir: str, worker_dir: str) -> int:
    root = Path(probe_dir)
    tests = sorted(root.glob("*.node"))
    with_subprocess = []
    subprocess_files: set[str] = set()
    for node in tests:
        key = node.stem
        data = sorted(p for p in root.glob(f"{key}.cov*") if p.is_file())
        if not data:
            continue
        files, lines = _lines(data)
        if lines:
            with_subprocess.append({"node": node.read_text(encoding="utf-8"), "files": len(files)})
            subprocess_files |= files
    worker_paths = sorted(Path(worker_dir).glob(".coverage.*"))
    contexts: set[str] = set()
    in_process_files: set[str] = set()
    for path in worker_paths:
        data = CoverageData(basename=str(path))
        data.read()
        contexts |= set(data.measured_contexts())
        in_process_files |= {name for name in data.measured_files() if data.lines(name)}
    summary = {
        "tests": len(tests),
        "tests_with_subprocess_data": len(with_subprocess),
        "subprocess_source_files": len(subprocess_files),
        "worker_data_files": len(worker_paths),
        "in_process_contexts": len(contexts - {""}),
        "in_process_source_files": len(in_process_files),
        "union_source_files": len(subprocess_files | in_process_files),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    out = root.parent / "tia-probe-tests.json"
    out.write_text(json.dumps(with_subprocess, indent=0), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
