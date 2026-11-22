"""Profile the 2nd 'apm install' on a perf fixture (CI only): spawns, cProfile, importtime."""

import io
import os
import pstats
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys  # noqa: E402

apm = Path(sys.argv[1])
work = Path(sys.argv[2]).resolve()
fixture = journeys.prepare_fixture("tree-10", work)
python = apm.read_bytes()[:200].split(b"\n", 1)[0][2:].decode().strip()
with journeys.open_world(fixture, work / "worlds" / "tree-10") as world:
    world.rtt_ms = 30
    ctx = journeys.Context(fixture, world, [str(apm)], work / "runs" / "tree-10")
    ctx.work.mkdir(parents=True, exist_ok=True)
    ctx.installed()
    root = ctx.roots()[0]
    env = dict(world.env)
    plain = subprocess.run([str(apm), "install"], cwd=root, env=env, capture_output=True, text=True)
    print("== OUTPUT rc", plain.returncode, "\n", plain.stdout[-3000:], plain.stderr[-3000:])
    log = work / "argv.log"
    env2 = dict(env, L5_ARGV_LOG=str(log), PYTHONPATH=str(Path(__file__).parent / "l5_argv"))
    subprocess.run([str(apm), "install"], cwd=root, env=env2, capture_output=True, text=True)
    print("== SPAWNS\n" + (log.read_text() if log.exists() else "(none)"))
    prof = work / "noop.prof"
    subprocess.run([python, "-m", "cProfile", "-o", str(prof), str(apm), "install"], cwd=root, env=env, capture_output=True)
    for key in ("tottime", "cumulative"):
        buf = io.StringIO()
        pstats.Stats(str(prof), stream=buf).sort_stats(key).print_stats(70)
        print(f"== PROFILE by {key}\n" + buf.getvalue()[-16000:])
    imp = subprocess.run([python, "-X", "importtime", str(apm), "install"], cwd=root, env=env, capture_output=True, text=True)
    rows = []
    for line in imp.stderr.splitlines():
        if line.startswith("import time:") and "|" in line:
            parts = [p.strip() for p in line[len("import time:"):].split("|")]
            if parts[1].isdigit():
                rows.append((int(parts[1]), parts[2]))
    rows.sort(reverse=True)
    print("== IMPORTTIME cumulative us (top 60), total modules", len(rows))
    for us, name in rows[:60]:
        print(f"{us:>9} {name}")
