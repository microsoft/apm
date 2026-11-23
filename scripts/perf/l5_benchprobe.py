"""Replicate a bench journey (console-script apm, spawn log) with verdict diagnostics.

Usage: l5_benchprobe.py APM WORK FIXTURE JOURNEY
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys  # noqa: E402

apm, work, fixture_name, journey_name = sys.argv[1], Path(sys.argv[2]).resolve(), sys.argv[3], sys.argv[4]
fixture = journeys.prepare_fixture(fixture_name, work)
with journeys.open_world(fixture, work / "worlds" / fixture_name) as world:
    world.rtt_ms = 30
    ctx = journeys.Context(fixture, world, [apm], work / "runs" / fixture_name)
    ctx.work.mkdir(parents=True, exist_ok=True)
    journey = journeys.JOURNEYS[journey_name]
    journey.setup(ctx)
    print("roots", ctx.roots())
    ctx.spawn_log = ctx.work / "spawns.jsonl"
    world.env["APM_INSTALL_FAST_PATH"] = "verdict"
    for i in range(3):
        journey.setup(ctx)
        for argv, cwd in journey.steps(ctx):
            ctx.spawn_log.unlink(missing_ok=True)
            code, tail, cpu = ctx.timed_run(argv, cwd)
            lines = [l for l in tail.splitlines() if "fast path" in l or "verdict" in l]
            print(f"== {fixture_name} {journey_name} sample {i} {cwd.name} rc={code} cpu={cpu:.3f}s")
            for line in lines:
                print("   ", line)
            if ctx.spawn_log.is_file():
                print("   spawns:", ctx.spawn_log.read_text(encoding="utf-8")[:1500])
            argv_log = Path(str(ctx.spawn_log) + ".argv")
            if argv_log.is_file():
                print("   argv:", argv_log.read_text(encoding="utf-8")[:2500])
                argv_log.unlink()
            if i == 0:
                print("   TAIL:", tail[-1500:])
