import collections
import json
import re
import sys
from pathlib import Path

FAM = [
    ("path resolution + containment (realpath*, path_security, paths)", r"utils/realpath|path_security\.py|utils/paths\.py|skill_adoption\.py:_inactive_skill_roots|plugin_root_render|init_link_resolver|_is_root_local_package"),
    ("YAML", r"yaml_io\.py"),
    ("journal/ledger/receipts", r"journal_modes|deploy_snapshot|deployment_ledger|write_receipts|deploy_state|verdict_record"),
    ("discovery/re-walks", r"discovery|inventory|find_files_by_glob|format_detection|carried_license|helpers\.py:find_plugin_json|agent_plugins/loader|_package_files"),
    ("file writes/copies (incl. in-process fetch writes)", r"file_ops|deploy_store|atomic_io|inprocess_fetch\.py:(_write_file|_write_git_dir|materialize)|_copy_skill_bundle|hook_bundle"),
    ("hash/verify reads", r"content_hash|checkout_verify|compute_deployed_hashes"),
    ("fetch/network (in-process)", r"inprocess_fetch|http_credentials|anonymous_ref_listing|auth\.py"),
    ("primitive parse/integrate", r"primitives/parser|_integrator\.py|integration/"),
]


def fam(owner):
    for name, pat in FAM:
        if re.search(pat, owner):
            return name
    return "rest (imports, cli, other)" if owner.startswith("apm_cli/") else "rest (no apm frame)"


root = Path(sys.argv[1])
for run in sys.argv[2:]:
    for f in sorted((root / run / "profile" / "profiles").glob("*tree-50*")):
        d = json.loads(f.read_text())
        tot = d["process_cpu_s"] + (d["children_cpu_total_s"] or 0)
        sc = d["process_cpu_s"] / d["sampled_cpu_s"]
        agg = collections.Counter()
        for k, v in d["apm_owner_self"]:
            agg[fam(k)] += v * sc
        listed = sum(v for _, v in d["apm_owner_self"]) * sc
        agg["rest (imports, cli, other)"] += d["process_cpu_s"] - listed
        agg["git children"] = d["children_cpu_total_s"] or 0
        print(f"== {run} {f.name.replace('-rtt30.txt', '')} total {tot:.2f} s")
        for name, v in agg.most_common(4):
            print(f"   {v:5.2f} s ({100 * v / tot:4.1f}%) {name}")
