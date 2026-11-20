"""TEMPORARY diagnostic: replay gw1 order on Windows and locate the codex-pin polluter."""

import os
import re
import subprocess
import sys

ORDER = "scripts/zz_diag/gw1_prefix.txt"
nodes = [l for l in open(ORDER, encoding="utf-8").read().splitlines() if l]
files = list(dict.fromkeys(n.split("::")[0] for n in nodes))
base = [sys.executable, "-m", "pytest", "-p", "no:randomly", "-p", "no:xdist", "-p", "no:cacheprovider",
        "-q", "-p", "order_probe_plugin", "-o", "addopts=", "--maxfail=100000"]
env = {**os.environ, "PYTHONPATH": os.pathsep.join(["scripts/zz_diag", os.environ.get("PYTHONPATH", "")]),
       "DIAG_ORDER": ORDER}


def run(extra_env):
    r = subprocess.run([*base, *files], env={**env, **extra_env}, capture_output=True, text=True)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.startswith("DIAG"):
            print(line, flush=True)
    m = re.search(r"DIAG FIRST-FAIL-AFTER (\S+)", out)
    i = out.find("DIAG FIRST-FAIL-AFTER")
    if i >= 0:
        print(out[i:i + 800], flush=True)
    print(out.strip().splitlines()[-1] if out.strip() else "", flush=True)
    return m.group(1) if m else None


print("STAGE 1: probe after each file", flush=True)
hit = run({})
if hit:
    f = hit.split("::")[0]
    print(f"STAGE 2: probe after each test in {f}", flush=True)
    run({"DIAG_PROBE_EACH_IN": f})
else:
    print("NO FAILURE REPRODUCED IN SERIAL gw1 ORDER", flush=True)
