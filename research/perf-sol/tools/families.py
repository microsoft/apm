import json, sys, re
FAM = [
 ("1 containment (path_security)", r"utils/path_security\.py|base_integrator\.py:validate_deploy_path|write_seam\.py"),
 ("1b relpath/realpath for display+keys (paths.portable_relpath, _plan_key, _package_key, _is_root_local_package)", r"utils/paths\.py:portable_relpath|_plan_key|_package_key|_is_root_local_package|core/scope\.py"),
 ("1c link resolver + plugin root binding (init_link_resolver, bind_plugin_roots, resolve_plugin_root_placeholders)", r"init_link_resolver|plugin_root_render|plugin_parser"),
 ("1d no-clobber / unmanaged-conflict preflight (skill_preflight, skill_adoption, check_collision, deploy_state)", r"skill_preflight|skill_adoption|check_collision|deploy_state\.py|commit_gate"),
 ("2 receipts / ledger / snapshot / cache pin", r"deploy_snapshot|deployment_ledger|write_receipts|cache_pin|lockfile_write_authority"),
 ("3 checkout verify + commit reachability", r"checkout_verify|commit_reachability"),
 ("4 lockfile + YAML (build, emit, parse, validate)", r"yaml_io|deps/lockfile\.py|phases/lockfile\.py|mcp_integrator\.py:update_lockfile"),
 ("5 manifest/package validation + resolver admission", r"models/validation|models/apm_package|manifest_io|format_detection|anonymous_ref_listing|policy|apm_resolver|ref_resol"),
 ("6 content hash", r"content_hash"),
 ("7 git config/env probes (parent side)", r"git_env\.py|git_sparse|cached_repo_env"),
 ("8 discovery / inventory scans", r"primitives/discovery|carried_license|compilation/inventory|find_files_by_glob|agent_plugins/loader|helpers\.py:find_plugin_json|mcp_config_view"),
 ("9 copy / materialize (file_ops, deploy_store, git_cache)", r"file_ops|deploy_store|git_cache|atomic_io|locking\.py:atomic_land|skill_integrator\.py:_copy_skill_bundle|hook_bundle"),
 ("10 primitive render/integrate (parser, integrators)", r"primitives/parser|_integrator\.py|integration/"),
]
def fam(owner):
    for name, pat in FAM:
        if re.search(pat, owner): return name
    return "z other apm (imports, cli, console, misc)" if owner.startswith("apm_cli/") else "z no apm frame (interp/imports)"
d = json.load(open(sys.argv[1])); total = d["process_cpu_s"] + (d["children_cpu_total_s"] or 0)
scale = float(sys.argv[2]) / total if len(sys.argv) > 2 else 1.0
own = {}; rp = {}; calls = {}
for k, v in d["apm_owner_self"]: own[fam(k)] = own.get(fam(k), 0) + v
for k, v in d["leaf_by_apm_owner"]:
    leaf, owner = k.split(" <- ")
    if "realpath" in leaf: rp[fam(owner)] = rp.get(fam(owner), 0) + v
for k, v in d.get("realpath_calls_by_apm_owner", []): calls[fam(k)] = calls.get(fam(k), 0) + v
listed = sum(v for _, v in d["apm_owner_self"])
print(f"process self {d['process_cpu_s']} children {d['children_cpu_total_s']} total {total:.2f} sampled {d['sampled_cpu_s']} listed-owner {listed:.2f} import {d['import_cpu_s']} realpath calls {d.get('realpath_calls_total')} scale->{sys.argv[2] if len(sys.argv)>2 else '-'}")
for name in sorted(own, key=lambda n: -own[n]):
    print(f"| {name} | {own[name]*scale:.2f} | {100*own[name]/total:.1f}% | realpath {rp.get(name,0)*scale:.2f} s | realpath calls {calls.get(name,0)} |")
print("children:", {k: (v['count'], round(v['cpu_s']*scale, 2)) for k, v in d['children_by_subcommand'].items()}, f"total {d['children_cpu_total_s']*scale:.2f} ({100*d['children_cpu_total_s']/total:.1f}%)")
print(f"unlisted sampled (tail beyond top-150 owners) {(d['sampled_cpu_s']-listed)*scale:.2f}; unsampled self {(d['process_cpu_s']-d['sampled_cpu_s'])*scale:.2f}")
