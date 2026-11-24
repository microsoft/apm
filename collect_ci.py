"""Collect CI perf-bench runs into data/report.json (the schema build.py reads). CI only, no local benches.

Inputs:
  data/requests.log  one request per line: "<base8> <os> <fixtures> <journeys> <perf-bench branch>"
                     (lines starting with '#' or 'NEW=' are ignored), and/or
  --run ID[:os]      explicit run ids (os inferred from the artifacts when omitted).

Per run: download the shard artifacts and pair base/new CPU by (shard, pair index) per (fixture, journey):
speedup = 1 / median(new/base), 95% order-statistic band, pooled p50 seconds, seconds per CPU class
(reference EPYC 9V74), requests/spawns of the first pair, replay outcome. When no shard artifacts exist
(e.g. a cancelled run), fall back to the summary table in `gh run view --log` (no band, no class).
A cell measured by several runs keeps the one with the most pairs. Unfinished runs are listed as pending.
"""
import argparse, glob, json, math, os, re, statistics, subprocess, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RUNS = os.path.join(HERE, "runs")
REPO = "microsoft/apm"
REFERENCE_CLASS = "EPYC 9V74"
BASE_FULL = "e37400fa313bfe736ec63242299486dac7647961"


def sh(*a, timeout=600):
    return subprocess.run(a, capture_output=True, text=True, timeout=timeout).stdout.strip()


def band(ratios):
    n, s = len(ratios), sorted(ratios)
    lo = max(0, min(n - 1, math.floor(n / 2 - 1.96 * math.sqrt(n) / 2)))
    hi = max(0, min(n - 1, math.ceil(n / 2 + 1.96 * math.sqrt(n) / 2) - 1))
    return [s[lo], s[hi]]


def short_model(name):
    m = re.search(r"(?i)(EPYC \w+|Xeon[^@]*?\d{4}\w*|Apple M\d\w*(?: \w+)?|Core\(TM\) \S+)", name or "")
    return (m.group(1).strip() if m else (name or "?").strip()) or "?"


def os_of(platform):
    p = (platform or "").lower()
    return "windows" if p.startswith("windows") else "macos" if p.startswith(("macos", "darwin")) else "linux"


def run_for(branch):
    out = sh("gh", "run", "list", "--repo", REPO, "--workflow", "perf-bench.yml", "--branch", branch,
             "--limit", "1", "--json", "databaseId,status,conclusion")
    rows = json.loads(out or "[]")
    return rows[0] if rows else None


def run_info(rid):
    out = sh("gh", "run", "view", str(rid), "--repo", REPO, "--json", "status,conclusion,headBranch")
    return json.loads(out or "{}")


def download(rid):
    rd = os.path.join(RUNS, str(rid))
    if not glob.glob(f"{rd}/perf-bench-*"):
        os.makedirs(rd, exist_ok=True)
        sh("gh", "run", "download", str(rid), "--repo", REPO, "--dir", rd)
    return rd


def from_shards(rd):
    """{(fixture, journey): {(shard, idx): {"base": sample, "new": sample}}}, meta."""
    cells, meta_os = {}, None
    for f in glob.glob(f"{rd}/perf-bench-shard-*/runs/*/*.json"):
        shard = f.split("perf-bench-shard-")[1].split("/")[0]
        kind, idx = os.path.basename(os.path.dirname(f)).split("-")
        doc = json.load(open(f))
        meta = doc.get("meta") or {}
        meta_os = meta_os or os_of(meta.get("platform"))
        model = short_model(meta.get("cpu_model", ""))
        yard = meta.get("yardstick_cpu_s") or 0
        for r in doc.get("results", []):
            ok = [s for s in r.get("samples", []) if s.get("returncode") == 0]
            if not ok:
                continue
            s = ok[0]
            e = cells.setdefault((r["fixture"], r["journey"]), {}).setdefault((shard, idx), {})
            e[kind] = {"cpu": s["cpu_s"], "req": (s.get("traffic") or {}).get("requests"),
                       "spawns": sum((s.get("spawns") or {}).values()), "replay": s.get("replay", ""),
                       "units": s["cpu_s"] / yard if yard else None}
            e["model"] = model
    return cells, meta_os


def row_from_pairs(fx, j, pairs, os_, rid):
    full = [p for p in pairs.values() if "base" in p and "new" in p]
    if not full:
        return None
    ratios = [p["new"]["cpu"] / p["base"]["cpu"] for p in full]
    by = {}
    for p in full:
        by.setdefault(p["model"], []).append(p)
    by_class = {m: {"n": len(ps), "base_s": statistics.median(q["base"]["cpu"] for q in ps),
                    "new_s": statistics.median(q["new"]["cpu"] for q in ps)} for m, ps in sorted(by.items())}
    ref = REFERENCE_CLASS if REFERENCE_CLASS in by_class else max(by_class, key=lambda m: by_class[m]["n"])
    p50 = statistics.median(ratios)
    first = full[0]
    return {
        "command": j, "journey": j, "os": os_, "fixture": fx, "run": str(rid), "pairs": len(full),
        "paired_p50": p50, "band": band(ratios), "speedup": 1 / p50,
        "base_cpu_s": statistics.median(p["base"]["cpu"] for p in full),
        "new_cpu_s": statistics.median(p["new"]["cpu"] for p in full),
        "by_class": by_class,
        "headline": {"cpu_class": ref, "reference_class": ref == REFERENCE_CLASS, **by_class[ref]},
        "requests": f"{first['base']['req']}->{first['new']['req']}",
        "spawns": f"{first['base']['spawns']}->{first['new']['spawns']}",
        "replay_new": sorted({p["new"]["replay"] for p in full if p["new"].get("replay")}),
        "tls": {"mode": "openssl" if fx.endswith("-openssl") else "system"} if os_ == "windows" else {},
        "harness_rev": "v2", "source": "artifacts",
        "quality": "final" if len(full) >= 6 else f"provisional ({len(full)} pairs)",
    }


LOG_ROW = re.compile(r"\|\s*(tree-[\w-]+)\s*\|\s*([\w-]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|"
                     r"[^|]*\|[^|]*\|\s*([\d.]+)\s*\|[^|]*\|[^|]*\|\s*(\S+)\s*\|[^|]*\|[^|]*\|\s*(\S+)\s*\|"
                     r"\s*(\d+)/(\d+)\s*\|")


def from_log(rid, os_):
    rows = []
    for line in sh("gh", "run", "view", str(rid), "--repo", REPO, "--log").splitlines():
        m = LOG_ROW.search(line)
        if not m:
            continue
        fx, j, _rtt, b, n, paired, req, spawns, sb, sn = m.groups()
        p = float(paired)
        rows.append({"command": j, "journey": j, "os": os_, "fixture": fx, "run": str(rid),
                     "pairs": min(int(sb), int(sn)), "paired_p50": p, "band": None, "speedup": 1 / p,
                     "base_cpu_s": float(b), "new_cpu_s": float(n), "by_class": {}, "headline": {},
                     "requests": req, "spawns": spawns, "replay_new": [],
                     "tls": {"mode": "openssl" if fx.endswith("-openssl") else "system"} if os_ == "windows" else {},
                     "harness_rev": "v2", "source": "log",
                     "quality": "provisional (log table only: no band, no CPU class)"})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", default=[], help="run id, optionally ID:os")
    ap.add_argument("--requests", default=os.path.join(DATA, "requests.log"))
    ap.add_argument("--out", default=os.path.join(DATA, "report.json"))
    ap.add_argument("--new", default="", help="NEW sha (else from requests.log NEW= line)")
    ap.add_argument("--new-label", default=os.environ.get("PERF_NEW_LABEL", ""),
                    help='e.g. "the fat train head (#5406)" or "main"')
    a = ap.parse_args()
    todo, pending, new = [], [], a.new
    if os.path.exists(a.requests):
        for line in open(a.requests):
            if line.startswith("NEW="):
                new = new or line.split()[0][4:]
            if not line.strip() or line.startswith(("#", "NEW=")):
                continue
            parts = line.split()
            os_, branch = parts[1], parts[-1]
            run = run_for(branch)
            if not run or run["status"] != "completed":
                pending.append(f"{os_} {' '.join(parts[2:4])} {branch} {run and run['status']}")
                continue
            todo.append((run["databaseId"], os_, run["conclusion"]))
    for spec in a.run:
        rid, _, os_ = spec.partition(":")
        info = run_info(rid)
        if info.get("status") != "completed":
            pending.append(f"run {rid} {info.get('status')}")
            continue
        todo.append((int(rid), os_ or None, info.get("conclusion")))
    best = {}
    for rid, os_, concl in todo:
        rd = download(rid)
        cells, meta_os = from_shards(rd)
        os_ = os_ or meta_os or "linux"
        rows = [r for (fx, j), pairs in sorted(cells.items()) if (r := row_from_pairs(fx, j, pairs, os_, rid))]
        if not rows:
            rows = from_log(rid, os_)
        for r in rows:
            r["conclusion"] = concl
            k = (r["os"], r["fixture"], r["journey"])
            if k not in best or (r["pairs"], r["source"] == "artifacts") > (best[k]["pairs"], best[k]["source"] == "artifacts"):
                best[k] = r
        print(f"[i] run {rid} {os_} {concl}: {len(rows)} cells", file=sys.stderr)
    os.makedirs(DATA, exist_ok=True)
    out = {"base": BASE_FULL, "new": new or "?", "new_label": a.new_label, "harness": "danielmeppiel-perf-ceo-report",
           "reference_cpu_class": REFERENCE_CLASS, "collected": datetime.datetime.now().isoformat(timespec="minutes"),
           "rows": [best[k] for k in sorted(best)], "pending": pending}
    json.dump(out, open(a.out, "w"), indent=1)
    print(f"[+] {len(out['rows'])} cells -> {a.out}; pending {len(pending)}", file=sys.stderr)
    for p in pending:
        print("    pending:", p, file=sys.stderr)


if __name__ == "__main__":
    main()
