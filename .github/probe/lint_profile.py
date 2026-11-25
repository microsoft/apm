"""Architecture-linter profile (PERF research, #5196): one full run on a CI runner.

Runs scripts/lint_architecture_boundaries.py --root <NEW checkout> in-process under
cProfile, with ast.parse wrapped (count, time and caller per file) and an audit hook
counting file opens per path and per opening call site. Prints wall, CPU, the parse
and read census, and the top cumulative checks.
"""
import ast, collections, cProfile, io, os, pstats, resource, sys, time, traceback
from pathlib import Path

ROOT = Path(os.environ["LINT_ROOT"]).resolve()
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

parses = collections.Counter()
parse_time = collections.Counter()
parse_sites = collections.Counter()
reads = collections.Counter()
read_sites = collections.Counter()
_real_parse = ast.parse


def _site(skip: int) -> str:
    for frame in traceback.extract_stack()[: -skip][::-1]:
        if "architecture_linter" in frame.filename or "lint_architecture" in frame.filename:
            return f"{Path(frame.filename).name}:{frame.name}"
    return "?"


def traced_parse(source, filename="<unknown>", *args, **kwargs):
    started = time.perf_counter()
    try:
        return _real_parse(source, filename, *args, **kwargs)
    finally:
        name = str(filename)
        parses[name] += 1
        parse_time[name] += time.perf_counter() - started
        parse_sites[_site(2)] += 1


ast.parse = traced_parse


def hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        mode = args[1]
        if mode is None or (isinstance(mode, str) and "r" in mode and "+" not in mode):
            path = os.fsdecode(args[0])
            if path.endswith(".py"):
                reads[path] += 1
                read_sites[_site(3)] += 1


sys.addaudithook(hook)
sys.argv = ["lint_architecture_boundaries.py", "--root", str(ROOT)]
import runpy  # noqa: E402

before = resource.getrusage(resource.RUSAGE_SELF)
started = time.perf_counter()
prof = cProfile.Profile()
out = io.StringIO()
code = 0
try:
    prof.runcall(runpy.run_path, str(ROOT / "scripts" / "lint_architecture_boundaries.py"),
                 run_name="__main__")
except SystemExit as exc:
    code = exc.code
wall = time.perf_counter() - started
after = resource.getrusage(resource.RUSAGE_SELF)
cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
files = len(parses)
print(f"LINT exit={code} wall={wall:.1f}s cpu={cpu:.1f}s", flush=True)
print(f"LINT parse: calls={sum(parses.values())} files={files} total={sum(parse_time.values()):.2f}s "
      f"max_per_file={max(parses.values()) if parses else 0} "
      f"files_parsed_more_than_once={sum(1 for v in parses.values() if v > 1)}", flush=True)
for name, n in parses.most_common(8):
    print(f"LINT parse-top {n}x {parse_time[name]:.3f}s {name}", flush=True)
for site, n in parse_sites.most_common(10):
    print(f"LINT parse-site {n} {site}", flush=True)
print(f"LINT reads(.py): calls={sum(reads.values())} files={len(reads)} "
      f"max_per_file={max(reads.values()) if reads else 0} "
      f"files_read_more_than_once={sum(1 for v in reads.values() if v > 1)}", flush=True)
for name, n in reads.most_common(6):
    print(f"LINT read-top {n}x {name}", flush=True)
for site, n in read_sites.most_common(12):
    print(f"LINT read-site {n} {site}", flush=True)
stats = pstats.Stats(prof)
rows = []
for (file, _line, fn), (_cc, _nc, tt, ct, _callers) in stats.stats.items():
    if "/checks/" in file or "groups" in file:
        rows.append((ct, tt, f"{Path(file).parent.name}/{Path(file).name}:{fn}"))
for ct, tt, name in sorted(rows, reverse=True)[:25]:
    print(f"LINT cum {ct:.2f}s self {tt:.2f}s {name}", flush=True)
stats.sort_stats("tottime")
buf = io.StringIO()
pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(15)
print("LINT tottime-top\n" + "\n".join("LINT   " + l for l in buf.getvalue().splitlines()[6:30]), flush=True)
