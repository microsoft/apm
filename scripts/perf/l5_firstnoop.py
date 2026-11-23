"""First no-op after an install (engine no-op + record write) and the next (hit), base vs new.

Usage: l5_firstnoop.py BASE_APM NEW_APM WORK FIXTURE N
Per iteration and side: fresh project, the side's verdict records removed, a full
install (untimed), then the first no-op and the second no-op, timed (CPU incl.
waited children, the harness's own measurement).
"""

import shutil
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys  # noqa: E402

base_apm, new_apm, work = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
fixture_name, n = sys.argv[4], int(sys.argv[5])
fixture = journeys.prepare_fixture(fixture_name, work)
cpu: dict[str, list[tuple[float, float]]] = {"base": [], "new": []}
with journeys.open_world(fixture, work / "world") as world:
    world.rtt_ms = 30
    home = Path(world.env["APM_HOME"])
    for i in range(n):
        for side, apm in (("base", base_apm), ("new", new_apm)):
            ctx = journeys.Context(fixture, world, [apm], work / "runs" / f"{side}-{i}")
            ctx.work.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(home / "verdicts", ignore_errors=True)
            ctx.fresh_project()
            for root in ctx.roots():
                ctx.checked(["install"], root)
            first = ctx.timed_run(["install"], ctx.project)
            second = ctx.timed_run(["install"], ctx.project)
            assert first[0] == 0 and second[0] == 0, (first[1], second[1])
            cpu[side].append((first[2], second[2]))
for k, label in ((0, "first no-op after install"), (1, "second no-op")):
    b = statistics.median(x[k] for x in cpu["base"])
    nn = statistics.median(x[k] for x in cpu["new"])
    ratios = sorted(nw[k] / bs[k] for bs, nw in zip(cpu["base"], cpu["new"], strict=True))
    print(f"{fixture_name} {label}: base {b:.3f}s new {nn:.3f}s paired p50 {statistics.median(ratios):.4f} n={n}")
