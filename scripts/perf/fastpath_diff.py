"""Differential (research only): locked-warm install via the diff-apply fast path vs the full pipeline.

Usage: python scripts/perf/fastpath_diff.py <apm> <work> <out.json> [fixture]
Snapshots the project tree, apm_modules and the deployment receipts after each mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys as J  # noqa: E402


def snapshot(root: Path, home: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for base, prefix in ((root, "project/"), (home / "deployment-receipts", "receipts/")):
        if not base.exists():
            continue
        for directory, _dirs, names in os.walk(base):
            for name in names:
                path = Path(directory) / name
                rel = prefix + path.relative_to(base).as_posix()
                data = path.read_bytes()
                if rel.startswith("receipts/") and path.suffix == ".json":
                    doc = json.loads(data)
                    data = json.dumps(_scrub(doc), sort_keys=True).encode()
                out[rel] = hashlib.sha256(data).hexdigest()
    return out


def _scrub(node):
    if isinstance(node, dict):
        return {
            k: _scrub(v) for k, v in node.items() if k not in ("token", "created", "updated_at")
        }
    if isinstance(node, list):
        return [_scrub(v) for v in node]
    return node


def main() -> int:
    apm, work, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    name = sys.argv[4] if len(sys.argv) > 4 else "tree-50"
    fixture = J.prepare_fixture(name, work)
    report: dict = {}
    with J.open_world(fixture, work / "worlds" / name) as world:
        ctx = J.Context(fixture, world, [apm], work / "runs" / name)
        ctx.work.mkdir(parents=True, exist_ok=True)
        for mode, value in (("fast", "trace"), ("full", "off"), ("fast2", "trace")):
            J._locked_project(ctx)
            env = dict(world.env, APM_INSTALL_FAST_PATH=value)
            traces = []
            for root in ctx.roots():
                result = subprocess.run(
                    [apm, "install"], cwd=root, env=env, capture_output=True, text=True, check=False
                )
                traces += [
                    line for line in result.stderr.splitlines() if "install fast path" in line
                ]
                if result.returncode:
                    traces.append(f"EXIT {result.returncode}: {result.stderr[-800:]}")
            report[mode] = {"trace": traces, "files": snapshot(ctx.project, Path(world.apm_home))}
    a, b = report["fast"]["files"], report["full"]["files"]
    diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    report["diff"] = diff
    out.write_text(json.dumps(report, indent=1))
    for mode in ("fast", "full", "fast2"):
        print(mode, len(report[mode]["files"]), "files;", report[mode]["trace"][:3])
    print("DIFF fast vs full:", len(diff))
    for k in diff[:60]:
        print("  ", k, a.get(k, "-")[:10], b.get(k, "-")[:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
