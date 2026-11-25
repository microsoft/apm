"""Identical-rewrite census (PERF research): which writes of a full install leave bytes unchanged.

For one fixture and one journey, run a full install under an audit hook that snapshots
every write target before it is opened for writing or renamed onto, then compares the
final bytes. Reports per-target counts (identical / changed / new / temp), syscall-ish
counts (write-opens, renames, fsync, chmod, utime) and, from a cProfile run, the CPU
share of the write path.

Journeys (CENSUS_JOURNEY):
  warm       lock kept, deployed files kept, apm_modules removed, warm cache
  noop-miss  converged project, fast paths off (APM_INSTALL_FAST_PATH=0)
"""
import json, os, pstats, shutil, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, os.getcwd())
from tests.benchmarks.journeys import Context, open_world, prepare_fixture  # noqa: E402

FIXTURE = os.environ.get("ORACLE_FIXTURE", "tree-10")
JOURNEY = os.environ.get("CENSUS_JOURNEY", "noop-miss")
NEW_APM = os.environ["NEW_APM"]
PY = str(Path(NEW_APM).parent / ("python.exe" if os.name == "nt" else "python"))
NO_HIT = ("CI", "GITHUB_ACTIONS", "TF_BUILD", "RUNNER_TEMP", "SSL_CERT_DIR")
TRACER = r'''
import hashlib, json, os, sys
snap, counts, targets = {}, {"open_w": 0, "rename": 0, "fsync": 0, "chmod": 0, "utime": 0}, {}
WFLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
def digest(p):
    try:
        with open(p, "rb") as h:
            return hashlib.sha256(h.read()).hexdigest()
    except OSError:
        return None
_open = open
def note(path):
    path = os.path.abspath(os.fsdecode(path))
    targets[path] = targets.get(path, 0) + 1
    if path not in snap:
        snap[path] = digest(path) if os.path.isfile(path) else None
def hook(event, args):
    try:
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            mode, flags = args[1], args[2] if len(args) > 2 else 0
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WFLAGS)
            if write:
                counts["open_w"] += 1
                note(args[0])
        elif event in ("os.rename", "os.replace") and len(args) >= 2:
            counts["rename"] += 1
            note(args[1])
        elif event == "os.chmod":
            counts["chmod"] += 1
        elif event == "os.utime":
            counts["utime"] += 1
    except Exception:
        pass
sys.addaudithook(hook)
_fsync = os.fsync
def fsync(fd):
    counts["fsync"] += 1
    return _fsync(fd)
os.fsync = fsync
sys.argv = ["apm", "install"]
import cProfile
from apm_cli.entrypoint import main
prof = cProfile.Profile()
try:
    prof.runcall(main)
except SystemExit:
    pass
finally:
    prof.dump_stats(os.environ["CENSUS_PROF"])
    out = {"counts": counts, "targets": {}}
    for path, n in targets.items():
        before, after = snap.get(path), digest(path) if os.path.isfile(path) else None
        kind = "temp" if after is None else "new" if before is None else (
            "identical" if before == after else "changed")
        out["targets"][path] = [kind, n]
    with _open(os.environ["CENSUS_OUT"], "w") as f:
        json.dump(out, f)
'''


def env(world, **extra):
    e = {k: v for k, v in world.env.items() if k not in NO_HIT}
    e.update(extra)
    return e


def main() -> int:
    name = FIXTURE
    work = Path(tempfile.mkdtemp())
    fixture = prepare_fixture(name, work)
    with open_world(fixture, work / "world" / name) as world:
        ctx = Context(fixture=fixture, world=world, apm=[NEW_APM], work=work / "w")
        ctx.fresh_project()
        root = ctx.roots()[0]
        for _ in range(2):
            subprocess.run([NEW_APM, "install"], cwd=root, env=env(world), capture_output=True)
        if JOURNEY == "warm":
            shutil.rmtree(Path(root) / "apm_modules", ignore_errors=True)
        out, prof = work / "census.json", work / "census.prof"
        started = time.perf_counter()
        r = subprocess.run([PY, "-c", TRACER], cwd=root, capture_output=True, text=True,
                           env=env(world, APM_INSTALL_FAST_PATH="0", CENSUS_OUT=str(out),
                                   CENSUS_PROF=str(prof)))
        wall = time.perf_counter() - started
        data = json.loads(out.read_text())
        rootp = os.path.realpath(root)
        home = os.path.realpath(world.apm_home)
        buckets = {}
        for path, (kind, n) in data["targets"].items():
            real = os.path.realpath(path) if os.path.exists(path) else path
            area = ("project" if real.startswith(rootp) and "apm_modules" not in real else
                    "apm_modules" if "apm_modules" in real else
                    "apm_home" if real.startswith(home) else "other")
            b = buckets.setdefault(area, {})
            b[kind] = b.get(kind, 0) + 1
        names = sorted(
            os.path.relpath(path, rootp if path.startswith(rootp) else home)
            for path, (kind, _n) in data["targets"].items()
            if kind == "identical" and (path.startswith(rootp) or path.startswith(home)))
        stats = pstats.Stats(str(prof))
        total = stats.total_tt
        write_fns = ("write_seam.py", "atomic_io.py", "deploy_snapshot", "transaction.py")
        cum = {}
        for (file, _line, fn), (_cc, _nc, _tt, ct, _callers) in stats.stats.items():
            if any(w in file for w in write_fns) and fn in (
                    "atomic_write_text", "write_text", "write_text_lf", "copy2", "copyfile",
                    "write_json_config", "open_for_write", "_record_deploy_write",
                    "_seal_deploy_write", "rename", "chmod", "mkdir"):
                cum[f"{Path(file).name}:{fn}"] = max(cum.get(f"{Path(file).name}:{fn}", 0), ct)
        label = f"{FIXTURE}/{JOURNEY}/{sys.platform}"
        print(f"CENSUS {label}: rc={r.returncode} wall={wall:.2f}s profiled_total={total:.3f}s", flush=True)
        print(f"CENSUS {label}: counts {json.dumps(data['counts'])}", flush=True)
        for area, b in sorted(buckets.items()):
            print(f"CENSUS {label}: targets[{area}] {json.dumps(b, sort_keys=True)}", flush=True)
        print(f"CENSUS {FIXTURE}/{JOURNEY}/{sys.platform}: identical {names[:12]}", flush=True)
        for k, v in sorted(cum.items(), key=lambda kv: -kv[1])[:12]:
            print(f"CENSUS {label}: cum {k} {v:.4f}s ({100*v/total:.1f}%)", flush=True)
        if r.returncode:
            print(f"CENSUS {label}: stderr {r.stderr[-800:]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
