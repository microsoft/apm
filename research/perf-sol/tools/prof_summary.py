import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
for run in sys.argv[2:]:
    for f in sorted((root / run / "profile" / "profiles").glob("*tree-50*")):
        d = json.loads(f.read_text())
        self_ = d["process_cpu_s"]
        ch = d["children_cpu_total_s"] or 0
        tot = self_ + ch
        sc = self_ / d["sampled_cpu_s"]
        kinds = d.get("leaf_kind", [])
        print(f"== {run} {f.name}: total {tot:.2f} s (self {self_:.2f}, children {ch:.2f}, imports {d['import_cpu_s']:.2f})")
        if kinds:
            print("   kinds: " + ", ".join(f"{k} {v * sc:.2f}" for k, v in kinds[:8]))
        print("   top apm owners (exclusive): " + "; ".join(f"{k.split('/')[-1]} {v * sc:.2f}" for k, v in d["apm_owner_self"][:8]))
        if d.get("children_by_subcommand"):
            print("   children: " + ", ".join(f"{k} x{v['count']} {v['cpu_s']:.2f}" for k, v in list(d["children_by_subcommand"].items())[:5]))
