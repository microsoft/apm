"""Per-shard paired ratios + base absolute per shard for a perf-bench run (downloads shards)."""
import glob, json, os, re, statistics, subprocess, sys

def load(run):
    d = f"runs/del-{run}"
    if not os.path.isdir(d):
        os.makedirs(d)
        for s in (1, 2, 3):
            subprocess.run(["gh", "run", "download", run, "--repo", "microsoft/apm", "--name", f"perf-bench-shard-{s}", "--dir", f"{d}/s{s}"], capture_output=True)
    out = {}
    for s in (1, 2, 3):
        for f in glob.glob(f"{d}/s{s}/**/*.json", recursive=True):
            m = re.search(r"/(base|new)-(\d+)/", f)
            if not m:
                continue
            for x in json.load(open(f)).get("results", []):
                if x.get("error"):
                    out.setdefault(("ERR", x["fixture"], x["journey"]), []).append(x["error"][:120])
                    continue
                out.setdefault((s, x["fixture"], x["journey"], m.group(1)), {})[int(m.group(2))] = x["cpu_p50_s"]
    return out

for run in sys.argv[1:]:
    data = load(run)
    print("==", run)
    keys = sorted({k[1:3] for k in data if k[0] != "ERR"})
    for fx, j in keys:
        ratios, rows = [], []
        for s in (1, 2, 3):
            b = data.get((s, fx, j, "base"), {}); n = data.get((s, fx, j, "new"), {})
            pr = [n[p] / b[p] for p in b if p in n and b[p]]
            if pr:
                ratios += pr
                rows.append(f"s{s} base {statistics.median(b.values()):.2f} new {statistics.median(n.values()):.2f} r {statistics.median(pr):.3f}")
        print(f"  {fx} {j}: paired {statistics.median(ratios):.3f} | " + " | ".join(rows))
    for k, v in data.items():
        if k[0] == "ERR":
            print("  ERRORS", k, len(v), v[:1])
