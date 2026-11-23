"""Miss-correctness cell + miss cost for the verdict-digest hack (CI only).

For BASE and NEW builds, in identical offline worlds:
  install (full; NEW writes its record) -> install (NEW: digest hit)
  -> edit apm.yml (append a comment) -> install (NEW must MISS and run today's path).
Reports CPU of each step per build, the miss cost in seconds (NEW - BASE on the
edited step), and whether the edited step's stdout equals BASE's (elapsed
normalized). Usage: l5_miss.py BASE_PY NEW_PY WORK FIXTURE N
"""

from __future__ import annotations

import os
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.benchmarks import journeys  # noqa: E402

base_py, new_py, work, fixture_name, repeats = (
    sys.argv[1],
    sys.argv[2],
    Path(sys.argv[3]).resolve(),
    sys.argv[4],
    int(sys.argv[5]),
)


def run(python: str, cwd: Path, env: dict) -> tuple[int, float, str, str]:
    argv = [python, "-c", "import sys; from apm_cli.entrypoint import main; sys.argv=['apm','install']; main()"]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            proc.wait()
            t = [wintypes.FILETIME() for _ in range(4)]
            ctypes.windll.kernel32.GetProcessTimes(wintypes.HANDLE(int(proc._handle)), *[ctypes.byref(x) for x in t])
            cpu = sum(((x.dwHighDateTime << 32) | x.dwLowDateTime) / 1e7 for x in t[2:])
            code = proc.returncode
        else:
            _, status, usage = os.wait4(proc.pid, 0)
            code, cpu = os.waitstatus_to_exitcode(status), usage.ru_utime + usage.ru_stime
        out.seek(0)
        err.seek(0)
        return code, cpu, out.read().decode(errors="replace"), err.read().decode(errors="replace")


def norm(text: str) -> str:
    text = re.sub(r"in \d+\.\d+s\.", "in Xs.", text)
    return re.sub(r"\(cached, fetched \S+ ago\)", "", text)


fixture = journeys.prepare_fixture(fixture_name, work)
results: dict[str, dict[str, list]] = {}
outputs: dict[str, dict[str, str]] = {}
for label, python in (("base", base_py), ("new", new_py)):
    steps: dict[str, list] = {"install": [], "noop": [], "edited": []}
    for rep in range(repeats):
        with journeys.open_world(fixture, work / "worlds" / f"{label}-{rep}") as world:
            world.rtt_ms = 30
            ctx = journeys.Context(fixture, world, [python], work / "runs" / f"{label}-{rep}")
            ctx.work.mkdir(parents=True, exist_ok=True)
            ctx.fresh_project()
            root = ctx.roots()[0]
            env = dict(world.env, APM_INSTALL_FAST_PATH="trace")
            for step in ("install", "noop", "edited"):
                if step == "edited":
                    with (root / "apm.yml").open("a", encoding="utf-8") as handle:
                        handle.write("# edited\n")
                code, cpu, out, err = run(python, root, env)
                assert code == 0, (label, step, code, out[-2000:], err[-2000:])
                trace = [line for line in err.splitlines() if "fast path:" in line or "verdict digest" in line]
                steps[step].append((cpu, trace))
                outputs.setdefault(label, {})[step] = norm(out)
    results[label] = steps

print("== CPU p50 per step (s)")
for label, steps in results.items():
    for step, samples in steps.items():
        cpus = [c for c, _ in samples]
        print(f"{label:<5} {step:<8} p50 {statistics.median(cpus):.3f}s  {[round(c, 3) for c in cpus]}  trace={samples[0][1]}")
miss = statistics.median(c for c, _ in results["new"]["edited"]) - statistics.median(
    c for c, _ in results["base"]["edited"]
)
print(f"== MISS COST (new - base, edited step): {miss:+.3f}s")
new_trace = results["new"]["edited"][0][1]
cell = any("verdict digest: miss" in t for t in new_trace) and not any("verdict digest: hit" in t for t in new_trace)
print("== CELL edit-apm.yml -> miss and today's path:", "PASS" if cell else "FAIL", new_trace)
print("== HIT on noop:", results["new"]["noop"][0][1])
same = outputs["base"]["edited"] == outputs["new"]["edited"]
print("== edited-step stdout identical to base:", same)
if not same:
    print("--- base\n" + outputs["base"]["edited"] + "\n--- new\n" + outputs["new"]["edited"])
same_noop = outputs["base"]["noop"] == outputs["new"]["noop"]
print("== noop stdout identical to base (replay of the install's output today):", same_noop)
if not same_noop:
    print("--- base noop\n" + outputs["base"]["noop"] + "\n--- new noop\n" + outputs["new"]["noop"])
sys.exit(0 if cell else 1)
