"""TEMPORARY diagnostic: find which earlier test node breaks the codex-pin victim on Windows."""

import subprocess
import sys

VICTIM = (
    "tests/unit/install/test_cached_codex_source_pin.py::"
    "test_frozen_global_install_from_warm_cache_uses_locked_source_pin[locked-sha]"
)
POLLUTER_FILES = [
    "tests/unit/install/phases/test_targets_phase_v2.py",
    "tests/unit/install/test_bootstrap_preflight.py",
]
BASE = [sys.executable, "-m", "pytest", "-p", "no:randomly", "-p", "no:xdist", "-p", "no:cacheprovider", "-q"]


def run(*nodes: str) -> tuple[bool, str]:
    r = subprocess.run([*BASE, *nodes], capture_output=True, text=True)
    return r.returncode == 0, r.stdout[-3000:]


def collect(path: str) -> list[str]:
    r = subprocess.run([*BASE, "--collect-only", "-q", path], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if "::" in l]


ok, _ = run(VICTIM)
print(f"VICTIM ALONE: {'PASS' if ok else 'FAIL'}", flush=True)
ok, out = run(*POLLUTER_FILES, VICTIM)
print(f"TRIO: {'PASS' if ok else 'FAIL'}", flush=True)
if not ok:
    print(out)
for path in POLLUTER_FILES:
    ok, _ = run(path, VICTIM)
    print(f"FILE {path} + victim: {'PASS' if ok else 'FAIL'}", flush=True)
    if ok:
        continue
    for node in collect(path):
        ok, out = run(node, VICTIM)
        print(f"  NODE {node}: {'PASS' if ok else 'FAIL'}", flush=True)
        if not ok:
            print(out[-1500:], flush=True)
