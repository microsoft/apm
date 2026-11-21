"""#4601 evidence: loop the concurrent-writer scenario and record replace errors."""

import json
import os
import sys
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apm_cli.policy import discovery
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

ITERATIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 300
_real_replace = os.replace
errors = Counter()
lock = threading.Lock()


def recording_replace(src, dst, *a, **k):
    try:
        return _real_replace(src, dst, *a, **k)
    except OSError as exc:
        kind = "meta" if str(dst).endswith(".meta.json") else "policy"
        with lock:
            errors[(kind, type(exc).__name__, getattr(exc, "winerror", None))] += 1
        raise


os.replace = recording_replace


def policy(i):
    return ApmPolicy(name=f"writer-{i}", version=f"{i}.0", enforcement="warn",
                     dependencies=DependencyPolicy(deny=(f"bad-{i}",)))


final_none = 0
torn = 0
for it in range(ITERATIONS):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ref = "contoso/.github"

        def w(i):
            discovery._write_cache(ref, policy(i), root, chain_refs=[ref])
            discovery._read_cache_entry(ref, root)

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(w, range(16)))
        if discovery._read_cache(ref, root) is None:
            final_none += 1
            d = discovery._get_cache_dir(root)
            k = discovery._cache_key(ref)
            meta = json.loads((d / f"{k}.meta.json").read_text(encoding="utf-8"))
            doc = (d / f"{k}.yml").read_text(encoding="utf-8")
            if meta.get("fingerprint") != discovery._policy_fingerprint(doc):
                torn += 1

print(f"platform={sys.platform} iterations={ITERATIONS}")
print(f"final_read_none={final_none} torn_pair(fingerprint mismatch)={torn}")
for key, n in sorted(errors.items(), key=str):
    print(f"replace_error target={key[0]} type={key[1]} winerror={key[2]} count={n}")
sys.exit(0)
