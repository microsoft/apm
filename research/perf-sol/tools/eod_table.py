"""Build the EOD speed-of-light table from downloaded perf-bench results.

usage: eod_table.py <dir-with-run-subdirs>
Each subdir is a run id with perf-bench-result/ (summary.md + perf-bench-result.json).
"""

import json
import sys
from pathlib import Path

FLOOR = {"linux": 0.7, "windows": 1.5, "macos": 0.9}

root = Path(sys.argv[1])
rows = []
for run in sorted(root.iterdir()):
    res = run / "perf-bench-result" / "perf-bench-result.json"
    if not res.exists():
        continue
    data = json.loads(res.read_text())
    meta = data.get("meta", {})
    for row in data["rows"]:
        rows.append((run.name, meta, row))

for run, meta, row in rows:
    b, n = row["base"], row["new"]
    os_name = meta.get("os", "?")
    print(
        f"{run} {os_name} {meta.get('base', '?')[:8]}->{meta.get('new', '?')[:8]} "
        f"{row['fixture']} {row['journey']}: base {b['cpu_p50_s']} new {n['cpu_p50_s']} "
        f"paired {row['paired_new_over_base']} req {b['requests']}->{n['requests']} "
        f"spawns {b['spawns']}->{n['spawns']}"
    )
