"""Collect the v0.39.0 release-note bench runs into one table (pinned bar method).

Reads requests.log (base8 os fixture journeys branch), downloads each finished
run into ../runs/<id>, pairs base/new CPU by (run, shard, pair index), and
prints per base: journey x fixture x OS x population with CPU p50 base/new,
paired p50 ratio, its 95% order-statistic band, requests and spawns.
"""
import glob, json, math, os, statistics, subprocess, sys

D = '/Users/danielmeppiel/.copilot/session-state/3e13e13b-ca92-4e58-8ec4-98ccb45457df/files/report-data'
OUT = '/Users/danielmeppiel/.copilot/session-state/dc81e4a3-7afb-4790-8434-c9231ae5484a/files/perf-report'
RUNS = os.path.join(OUT, 'runs')
AUTH = {"install-cold-auth", "install-warm-auth"}


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()


def run_for(branch):
    out = sh("gh", "run", "list", "--repo", "microsoft/apm", "--workflow", "perf-bench.yml",
             "--branch", branch, "--limit", "1", "--json", "databaseId,status,conclusion")
    rows = json.loads(out or "[]")
    return rows[0] if rows else None


def band(ratios):
    n = len(ratios)
    s = sorted(ratios)
    lo = max(0, min(n - 1, math.floor(n / 2 - 1.96 * math.sqrt(n) / 2)))
    hi = max(0, min(n - 1, math.ceil(n / 2 + 1.96 * math.sqrt(n) / 2) - 1))
    return s[lo], s[hi]


REFERENCE_CLASS = "EPYC 9V74"


def _short_model(name):
    """'AMD EPYC 9V74 80-Core Processor' -> 'EPYC 9V74'; else the name; '?' when unknown."""
    import re
    m = re.search(r"(?i)(EPYC \w+|Xeon[^@]*?\d{4}\w*|Apple M\d\w*(?: \w+)?|Core\(TM\) \S+)", name or "")
    return (m.group(1).strip() if m else (name or "?").strip()) or "?"


def by_class(full):
    out = {}
    for p in full:
        out.setdefault(p.get("model", "?"), []).append(p)
    return {
        m: {"n": len(ps), "base": statistics.median(p["base"][0] for p in ps),
            "new": statistics.median(p["new"][0] for p in ps)}
        for m, ps in sorted(out.items())
    }


def _units(full):
    """Median yardstick-normalized CPU (cpu_s / yardstick_cpu_s), base and new; None if absent."""
    base = [p.get("base_units") for p in full if p.get("base_units")]
    new = [p.get("new_units") for p in full if p.get("new_units")]
    if not base or not new:
        return None
    return {"base": statistics.median(base), "new": statistics.median(new)}


def cells(run_dir):
    side = {}
    for f in glob.glob(f"{run_dir}/perf-bench-shard-*/runs/*/*.json"):
        shard = f.split("perf-bench-shard-")[1].split("/")[0]
        tag = os.path.basename(os.path.dirname(f))
        kind, idx = tag.split("-")
        doc = json.load(open(f))
        meta = doc.get("meta") or {}
        model = _short_model(meta.get("cpu_model", ""))
        yard = meta.get("yardstick_cpu_s") or 0
        for r in doc["results"]:
            ok = [s for s in r["samples"] if s.get("returncode") == 0]
            if not ok:
                continue
            key = (r["fixture"], r["journey"])
            entry = side.setdefault(key, {}).setdefault((shard, idx), {})
            entry[kind] = (
                ok[0]["cpu_s"], ok[0]["traffic"].get("requests"), sum(ok[0]["spawns"].values()))
            entry["model"] = model
            entry[kind + "_units"] = ok[0]["cpu_s"] / yard if yard else None
    return side


def main():
    lines = [l.split() for l in open(os.path.join(D, "requests.log")) if not l.startswith(("NEW=", "#")) and l.strip()]
    table, pending = [], []
    for base8, osname, fixture, _journeys, branch in lines:
        run = run_for(branch)
        if not run or run["status"] != "completed":
            pending.append(f"{base8} {osname} {fixture} {branch} {run and run['status']}")
            continue
        rid = str(run["databaseId"])
        rd = os.path.join(RUNS, rid)
        if not glob.glob(f"{rd}/perf-bench-shard-*"):
            os.makedirs(rd, exist_ok=True)
            sh("gh", "run", "download", rid, "--repo", "microsoft/apm", "--dir", rd)
        for (fx, j), pairs in sorted(cells(rd).items()):
            full = [p for p in pairs.values() if "base" in p and "new" in p]
            if not full:
                continue
            ratios = [p["new"][0] / p["base"][0] for p in full]
            lo, hi = band(ratios)
            table.append({
                "base": base8, "os": osname, "fixture": fx, "journey": j,
                "population": "authenticated token-first" if j in AUTH else "public github.com anonymous",
                "base_cpu": statistics.median(p["base"][0] for p in full),
                "new_cpu": statistics.median(p["new"][0] for p in full),
                "ratio": statistics.median(ratios), "lo": lo, "hi": hi, "pairs": len(full),
                "req": f"{full[0]['base'][1]}->{full[0]['new'][1]}",
                "spawns": f"{full[0]['base'][2]}->{full[0]['new'][2]}",
                "run": rid, "conclusion": run["conclusion"],
                "by_class": by_class(full),
                "units": _units(full),
            })
    json.dump(table, open(os.path.join(OUT, "table.json"), "w"), indent=1)
    for base8 in sorted({r["base"] for r in table}):
        print(f"\n### base {base8} -> new (CPU p50 s, paired new/base p50 [95% band], pairs)\n")
        print("| journey | population | fixture | OS | base s | new s | paired | 95% band | speedup | requests | spawns | pairs | run | units base->new | abs by CPU class (base->new s, n) |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in sorted((r for r in table if r["base"] == base8),
                        key=lambda r: (r["journey"], r["fixture"], r["os"])):
            print(f"| {r['journey']} | {r['population']} | {r['fixture']} | {r['os']} | {r['base_cpu']:.2f} | "
                  f"{r['new_cpu']:.2f} | {r['ratio']:.3f} | {r['lo']:.3f}-{r['hi']:.3f} | "
                  f"{1 / r['ratio']:.2f}x | {r['req']} | {r['spawns']} | {r['pairs']} | {r['run']} | "
                  + (f"{r['units']['base']:.1f}->{r['units']['new']:.1f}" if r.get("units") else "-") + " | "
                  + "; ".join(f"{'*' if m == REFERENCE_CLASS else ''}{m}: {c['base']:.2f}->{c['new']:.2f} ({c['n']})"
                              for m, c in r["by_class"].items()) + " |")
    if pending:
        print("\nPENDING:\n" + "\n".join(pending), file=sys.stderr)


main()
