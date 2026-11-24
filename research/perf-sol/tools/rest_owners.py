import json, re, sys, collections
from pathlib import Path
exec(open(Path(__file__).with_name('v039_buckets.py')).read().split('root = Path')[0])
GROUPS = [
    ("imports (module bodies)", r":<module>$"),
    ("package validation (models/validation, primitive_classification, deployable_source_plan)", r"models/validation|primitive_classification|deployable_source_plan"),
    ("config/manifest reads (config.py, manifest_io, scope, registry config, git_config_memo, commit_reachability cfg)", r"apm_cli/config\.py|manifest_io|core/scope|registry/config_loader|git_config_memo|commit_reachability\.py:_config|apm_package\.py"),
    ("security content scan", r"security/content_scanner|security/"),
    ("cache bookkeeping (git_cache, cache_pin, locking, atomic_land)", r"cache/git_cache|cache_pin|cache/locking|cache/"),
    ("resolver/graph (apm_resolver, transport_selection, github_downloader, dependency models)", r"apm_resolver|transport_selection|github_downloader|models/dependency|deps/"),
    ("lockfile/deploy records + deployment_state", r"lockfile|deployment_state|deploy_records"),
    ("cli/console/version/pipeline glue", r"console|version\.py|commands/|install/pipeline|install/services|install/context|cli\.py|entrypoint"),
]
def grp(owner):
    for n, p in GROUPS:
        if re.search(p, owner):
            return n
    return "other apm"
for run in sys.argv[1:]:
    for f in sorted(Path(f'runs/v039/{run}/profile/profiles').glob('tree-50-install-cold*')):
        d = json.loads(f.read_text())
        sc = d['process_cpu_s'] / d['sampled_cpu_s']
        listed = sum(v for _, v in d['apm_owner_self']) * sc
        agg = collections.Counter(); top = collections.defaultdict(list)
        for k, v in d['apm_owner_self']:
            if fam(k).startswith('rest'):
                g = "no apm frame / unsampled" if k == "<no apm frame>" else grp(k)
                agg[g] += v * sc; top[g].append((v * sc, k))
        agg["no apm frame / unsampled"] += d['process_cpu_s'] - listed
        tot = sum(agg.values())
        print(f"== {run} {f.stem} rest {tot:.2f} s (process {d['process_cpu_s']:.2f}; startup {d.get('startup_cpu_s')}, import {d.get('import_cpu_s')})")
        for g, v in agg.most_common():
            ex = ", ".join(f"{k.split('apm_cli/')[-1]} {s:.2f}" for s, k in sorted(top[g], reverse=True)[:3])
            print(f"   {v:5.2f} s {g}  [{ex}]")
