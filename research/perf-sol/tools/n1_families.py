import json
import re
import sys

FAM = [
    ("c discovery/re-walks", r"content_hash\.py:_package_files$|find_files_by_glob|find_primitive_files|_find_primitive_inventory_files|_inactive_skill_roots|bind_plugin_roots|resolve_plugin_root_placeholders|init_link_resolver|_is_root_local_package|inventory\.py:collect|carried_license\.py:_skill_dirs|format_detection"),
    ("f re-hash/verify reads", r"_hash_package_tree|compute_exact_file_hash|compute_file_hash|checkout_verify\.py:_read_regular|compute_deployed_hashes"),
    ("a containment+portable_relpath", r"path_security\.py|paths\.py:portable_relpath"),
    ("b YAML", r"yaml_io\.py"),
    ("d copies/writes", r"_reflink_copy_file|_do_copytree|deploy_store\.py:(put|_prepare)|inprocess_fetch\.py:(_write_file|_write_git_dir)|atomic_io\.py"),
    ("e journal/ledger/receipts", r"deploy_snapshot\.py:_append_line|deployment_ledger\.py:(record|_trusted_hash)|verdict_record\.py:write|deploy_state\.py"),
]


def fam(owner):
    for name, pat in FAM:
        if re.search(pat, owner):
            return name
    return "g rest"


for path in sys.argv[1:]:
    d = json.load(open(path))
    sampled = d["sampled_cpu_s"]
    total = d["process_cpu_s"] + (d["children_cpu_total_s"] or 0)
    agg = {}
    for k, v in d["apm_owner_self"]:
        agg[fam(k)] = agg.get(fam(k), 0) + v
    listed = sum(v for _, v in d["apm_owner_self"])
    agg["g rest"] = agg.get("g rest", 0) + (sampled - listed)
    print(f"== {path}: total {total:.2f} s (self {d['process_cpu_s']}, children {d['children_cpu_total_s']}), sampled {sampled:.2f}")
    for name in sorted(agg):
        print(f"| {name} | {agg[name]:.2f} s | {100 * agg[name] / sampled:.1f}% of sampled |")
    print("children:", {k: (v["count"], round(v["cpu_s"], 2)) for k, v in d["children_by_subcommand"].items()})
