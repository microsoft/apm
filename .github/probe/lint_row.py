"""Architecture-linter parallel row (PERF #5196): wall + CPU, base vs new, on CI.

Runs `bash scripts/lint-architecture-boundaries.sh` (the exact CI Lint entry) in
BASE_ROOT and NEW_ROOT, interleaved (AB, BA, AB), at the default worker count,
then one NEW reference run at W=4 and one at W=1. CPU is the child's rusage from
wait4 (includes the worker processes it waited for). Every run's stdout and
exit code must be identical. Then a copy of NEW gets one owner-guard mutation
per rule group and is linted at W=1, W=2 and W=4: outputs must match byte for byte.
"""
import os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

BASE = Path(os.environ["BASE_ROOT"]).resolve()
NEW = Path(os.environ["NEW_ROOT"]).resolve()
PAIRS = int(os.environ.get("PAIRS", "3"))


def lint(root: Path, workers: str | None, label: str) -> tuple[float, float, int, bytes]:
    env = {k: v for k, v in os.environ.items() if k != "APM_ARCH_LINT_WORKERS"}
    if workers is not None:
        env["APM_ARCH_LINT_WORKERS"] = workers
    out = tempfile.TemporaryFile()
    started = time.perf_counter()
    proc = subprocess.Popen(["bash", "scripts/lint-architecture-boundaries.sh"], cwd=root,
                            stdout=out, stderr=subprocess.STDOUT, env=env)
    _, status, usage = os.wait4(proc.pid, 0)
    wall = time.perf_counter() - started
    proc.returncode = os.waitstatus_to_exitcode(status)
    out.seek(0)
    data = out.read()
    cpu = usage.ru_utime + usage.ru_stime
    print(f"LINTROW run {label} W={workers or 'default'} exit={proc.returncode} "
          f"wall={wall:.1f}s cpu={cpu:.1f}s out_bytes={len(data)}", flush=True)
    return wall, cpu, proc.returncode, data


def med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


print(f"LINTROW cpu_count={os.cpu_count()} base={BASE} new={NEW}", flush=True)
rows = {"base": [], "new": []}
outputs = []
for i in range(PAIRS):
    order = ("base", "new") if i % 2 == 0 else ("new", "base")
    for side in order:
        r = lint(BASE if side == "base" else NEW, None, f"{side}#{i + 1}")
        rows[side].append(r)
        outputs.append((f"{side}#{i + 1}", r[2], r[3]))
ref4 = lint(NEW, "4", "new-ref")
ref1 = lint(NEW, "1", "new-single")
outputs += [("new-W4", ref4[2], ref4[3]), ("new-W1", ref1[2], ref1[3])]
first = outputs[0]
same = all(o[1] == first[1] and o[2] == first[2] for o in outputs)
for side in ("base", "new"):
    w = [r[0] for r in rows[side]]; c = [r[1] for r in rows[side]]
    print(f"LINTROW {side} wall_p50={med(w):.1f}s cpu_p50={med(c):.1f}s walls={[round(x,1) for x in w]} cpus={[round(x,1) for x in c]}", flush=True)
bw, nw = med([r[0] for r in rows["base"]]), med([r[0] for r in rows["new"]])
bc, nc = med([r[1] for r in rows["base"]]), med([r[1] for r in rows["new"]])
print(f"LINTROW RESULT wall {bw:.1f}s -> {nw:.1f}s ({bw / nw:.2f}x)  cpu {bc:.1f}s -> {nc:.1f}s "
      f"({(nc / bc - 1) * 100:+.0f}%)  W4 wall={ref4[0]:.1f}s cpu={ref4[1]:.1f}s  "
      f"W1 wall={ref1[0]:.1f}s cpu={ref1[1]:.1f}s  output={'same' if same else 'DIFFERENT'} exit={first[1]}", flush=True)
if not same:
    for label, code, data in outputs:
        print(f"LINTROW DIFF {label} exit={code} head={data[:300]!r}", flush=True)

# Injected violations in every group: W=1 vs W=2 vs W=4 on a copy of NEW.
sys.path.insert(0, str(NEW))
os.chdir(NEW)
from scripts.architecture_linter import runner  # noqa: E402
from tests.integration.test_architecture_owner_rule_mutations import MUTATIONS  # noqa: E402

group_by_guard = {g: r.group for r in runner.registered_rules() for g in r.guard_ids}
copy = Path(tempfile.mkdtemp()) / "repo"
shutil.copytree(NEW, copy, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__"))
picked, used = {}, set()
for case in MUTATIONS:
    grp = group_by_guard[case.guard_id]
    p = copy / case.path
    if grp in picked or case.path in used or not p.is_file():
        continue
    text = p.read_text(encoding="utf-8")
    if case.old not in text:
        continue
    p.write_text(text.replace(case.old, case.new, -1 if case.replace_all else 1), encoding="utf-8")
    picked[grp] = case.rule_id
    used.add(case.path)
print(f"LINTROW injected groups={sorted(picked)}", flush=True)
inj = [lint(copy, w, f"inj-W{w}") for w in ("1", "2", "4")]
inj_same = all(r[2] == inj[0][2] and r[3] == inj[0][3] for r in inj)
lines = inj[0][3].decode("ascii", "replace").splitlines()
hit = sorted({rid for rid in picked.values() if any(f" {rid} " in ln for ln in lines)})
print(f"LINTROW INJECTED output={'same' if inj_same else 'DIFFERENT'} exit={inj[0][2]} lines={len(lines)} "
      f"injected_rules_reported={len(hit)}/{len(picked)}", flush=True)
