"""Replicate the bench's noop measurement (console-script apm, spawn log) and print traces."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys  # noqa: E402

apm, work = sys.argv[1], Path(sys.argv[2]).resolve()
fixture = journeys.prepare_fixture("tree-10", work)
with journeys.open_world(fixture, work / "worlds" / "t10") as world:
    world.rtt_ms = 30
    ctx = journeys.Context(fixture, world, [apm], work / "runs" / "t10")
    ctx.work.mkdir(parents=True, exist_ok=True)
    journey = journeys.JOURNEYS["install-noop"]
    journey.setup(ctx)
    print("roots", ctx.roots())
    ctx.spawn_log = ctx.work / "spawns.jsonl"
    world.env["APM_INSTALL_FAST_PATH"] = "trace"
    for i in range(4):
        journey.setup(ctx)
        for argv, cwd in journey.steps(ctx):
            code, tail, cpu = ctx.timed_run(argv, cwd)
            lines = [l for l in tail.splitlines() if "fast path" in l or "verdict" in l]
            print(f"== sample {i} {cwd.name} rc={code} cpu={cpu:.3f}s {lines}")
