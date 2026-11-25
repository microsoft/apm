"""Lane-N replacement probe: import cost (importtime, import-only CPU, fork-server oracle) + pass counts."""
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.getcwd())
from tests.benchmarks.journeys import Context, _run_measured, open_world, prepare_fixture  # noqa: E402

HERE = Path(__file__).resolve().parent
NEW = Path(os.environ["NEW_APM"])
VENV = NEW.parent.parent
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
FIX = os.environ.get("PROBE_FIXTURE", "tree-50")
_pf = Path(__file__).with_name("parts")
PARTS = set((_pf.read_text().split() if _pf.exists() else ["imports", "counts", "fork"]))
PAIRS = int(os.environ.get("PROBE_PAIRS", "6"))


def say(tag, obj):
    print(f"PROBE {tag} {json.dumps(obj)}", flush=True)


def lock_digest(root: Path) -> str:
    p = root / "apm.lock.yaml"
    if not p.exists():
        return "missing"
    lines = [l for l in p.read_bytes().splitlines() if b"generated_at" not in l and b"timestamp" not in l]
    return hashlib.sha256(b"\n".join(lines)).hexdigest()[:12]


def deployed_digest(root: Path) -> str:
    """Every file outside apm_modules/.git/.apm-local, path + bytes + mode."""
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ("apm_modules", ".git", ".apm-local"))
        for name in sorted(files):
            if name == "apm.lock.yaml":
                continue
            p = Path(dirpath) / name
            h.update(p.relative_to(root).as_posix().encode())
            h.update(oct(p.lstat().st_mode & 0o777).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


work = Path(tempfile.mkdtemp())
fixture = prepare_fixture(FIX, work)
with open_world(fixture, work / "world") as world:
    ctx = Context(fixture=fixture, world=world, apm=[str(NEW)], work=work / "ctx")

    def cold() -> Path:
        world.clear_cache()
        ctx.fresh_project()
        return ctx.roots()[0]

    def measured(argv, root, env=None):
        with tempfile.TemporaryFile() as o, tempfile.TemporaryFile() as e:
            rc, cpu = _run_measured(argv, 900, cwd=root, env=env or world.env, stdout=o, stderr=e)
            e.seek(0)
            err = e.read().decode(errors="replace")
            if rc != 0:
                say("cmd_failed", {"argv": [str(a) for a in argv][:4], "rc": rc, "stderr_tail": err[-1500:]})
            return rc, cpu, err

    modfile = work / "modules.txt"
    if "imports" not in PARTS and "fork" not in PARTS:
        root = cold()
        out = work / "counts.json"
        rc, _, _ = measured([str(PY), str(HERE / "runner.py"), "count", str(out)], root)
        say("counts", {"rc": rc, **json.loads(out.read_text())})
        sys.exit(0)
    root = cold()
    rc, cpu_dump, _ = measured([str(PY), str(HERE / "runner.py"), "dump", str(modfile)], root)
    mods = modfile.read_text().split()
    say("modules", {"rc": rc, "count": len(mods), "apm_cli": sum(m.startswith("apm_cli") for m in mods)})

    root = cold()
    rc, cpu_it, err = measured([str(PY), "-X", "importtime", str(HERE / "runner.py"), "run"], root)
    rows = []
    for line in err.splitlines():
        m = re.match(r"import time:\s+(\d+) \|\s+(\d+) \|(\s*)(\S+)", line)
        if m:
            rows.append((int(m.group(1)), m.group(4).strip()))
    by_top = Counter()
    for us, name in rows:
        by_top[name.split(".")[0]] += us
    say("importtime", {"rc": rc, "install_cpu_s": round(cpu_it, 3), "modules": len(rows),
                       "self_total_s": round(sum(u for u, _ in rows) / 1e6, 3),
                       "top_packages_s": [(k, round(v / 1e6, 3)) for k, v in by_top.most_common(12)],
                       "top_modules_ms": [(n, round(u / 1e3, 1)) for u, n in sorted(rows, reverse=True)[:15]]})

    bare, imp = [], []
    for _ in range(PAIRS):
        bare.append(measured([str(PY), "-c", "pass"], root)[1])
        imp.append(measured([str(PY), str(HERE / "runner.py"), "imports", str(modfile)], root)[1])
    say("import_only", {"bare_python_cpu_p50": round(statistics.median(bare), 3),
                        "import_set_cpu_p50": round(statistics.median(imp), 3),
                        "import_cost_p50": round(statistics.median(imp) - statistics.median(bare), 3)})

    root = cold()
    out = work / "counts.json"
    rc, cpu_count, _ = measured([str(PY), str(HERE / "runner.py"), "count", str(out)], root)
    say("counts", {"rc": rc, **json.loads(out.read_text())})

    if os.name != "nt":
        env = dict(world.env, OBJC_DISABLE_INITIALIZE_FORK_SAFETY="YES")  # both modes get exactly this env
        srv = subprocess.Popen([str(PY), str(HERE / "server.py"), str(modfile)], cwd=root, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        say("forkserver", json.loads(srv.stdout.readline()))
        base, fork, digests = [], [], set()
        for i in range(PAIRS):
            for side in (("sub", "fork") if i % 2 == 0 else ("fork", "sub")):
                root = cold()
                if side == "sub":
                    rc, cpu, _ = measured([str(NEW), "install"], root, env=env)
                    base.append(cpu)
                else:
                    srv.stdin.write(json.dumps({"cwd": str(root), "env": env}) + "\n")
                    srv.stdin.flush()
                    r = json.loads(srv.stdout.readline())
                    rc, cpu = r["rc"], r["cpu"]
                    fork.append(cpu)
                if rc != 0:
                    say("error", {"side": side, "rc": rc})
                digests.add((side, lock_digest(root), deployed_digest(root)))
        srv.stdin.close()
        ratios = [f / b for f, b in zip(fork, base)]
        say("fork_oracle", {"pairs": len(base), "sub_cpu_p50": round(statistics.median(base), 3),
                            "fork_child_cpu_p50": round(statistics.median(fork), 3),
                            "paired_fork_over_sub_p50": round(statistics.median(ratios), 3),
                            "lockfile_digests": sorted(digests)})
