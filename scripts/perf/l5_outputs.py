"""Print a fresh install's and the following no-op's stdout (tree-10, offline world)."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys  # noqa: E402

apm, work = sys.argv[1], Path(sys.argv[2]).resolve()
for name in sys.argv[3].split(","):
    fixture = journeys.prepare_fixture(name, work)
    with journeys.open_world(fixture, work / "worlds" / name) as world:
        ctx = journeys.Context(fixture, world, [apm], work / "runs" / name)
        ctx.work.mkdir(parents=True, exist_ok=True)
        ctx.fresh_project()
        root = ctx.roots()[0]
        for step in ("fresh install", "no-op", "frozen no-op"):
            argv = [apm, "install"] + (["--frozen"] if step.startswith("frozen") else [])
            done = subprocess.run(argv, cwd=root, env=world.env, capture_output=True, text=True)
            print(f"===== {name} {step} rc={done.returncode}\n{done.stdout}\n----- stderr\n{done.stderr}")
