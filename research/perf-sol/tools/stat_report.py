import collections
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
STAT = (":stat", ":lstat", ":exists", ":is_file", ":is_dir", ":is_symlink", ":isdir", ":isfile", ":islink", ":lexists")
for run in sys.argv[2:]:
    f = next((root / run / "profile" / "profiles").glob("*tree-50*"))
    d = json.loads(f.read_text())
    tot = d["process_cpu_s"] + (d["children_cpu_total_s"] or 0)
    sc = d["process_cpu_s"] / d["sampled_cpu_s"]
    k = dict(d["leaf_kind"])
    print(f"== {run} {f.name}: total {tot:.2f} s; stat {k.get('stat', 0) * sc:.2f}, realpath {k.get('realpath', 0) * sc:.2f}, file_io {k.get('file_io', 0) * sc:.2f}, children {d['children_cpu_total_s']:.2f}")
    w = d.get("stat_calls_by_kind_where_owner")
    if w:
        bykind, byloc, byown = collections.Counter(), collections.Counter(), collections.Counter()
        for key, v in w:
            kind, rest = key.split(" | ", 1)
            loc, own = rest.split(" <- ", 1)
            bykind[kind] += v
            byloc[loc] += v
            byown[f"{kind:7s} {own.split('/')[-1]}"] += v
        print(f"   stat-family calls {d['stat_calls_total']} (listed {sum(v for _, v in w)})")
        print("   by kind:", dict(bykind.most_common()))
        print("   by location:", dict(byloc.most_common()))
        for o, v in byown.most_common(18):
            print(f"     {v:6d} {o}")
    else:
        own = collections.Counter()
        for key, v in d["leaf_by_apm_owner"]:
            leaf, o = key.split(" <- ")
            if any(x in leaf for x in STAT):
                own[o.split("/")[-1]] += v * sc
        for o, v in own.most_common(12):
            print(f"   {v:5.2f} s {o}")
