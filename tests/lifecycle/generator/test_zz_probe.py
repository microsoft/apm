import os, time, json, pytest
from tests.lifecycle.rows import Row
from tests.lifecycle.generator.replay import CoveringReplay

pytestmark = [pytest.mark.e2e]

STEPS = os.environ.get("PROBE_STEPS", "").split(";;")
PKGS = tuple(os.environ.get("PROBE_PKGS", "p-skill p-exec").split())
TGTS = tuple(os.environ.get("PROBE_TGTS", "copilot").split())
SRCS = json.loads(os.environ.get("PROBE_SRCS", "{}"))

def test_probe(lifecycle_world):
    row = Row("probe", ("P-INV-I1",), PKGS, TGTS, "project", (), ())
    r = CoveringReplay(lifecycle_world, row, {"world": {"sources": SRCS}})
    r._set_targets()
    for s in STEPS:
        s = s.strip()
        if not s:
            continue
        step = s
        if s == "CHECK":
            from dataclasses import replace
            r.row = replace(r.row, then={"deployed": "expected"})
            try:
                r.check_then(); print("CHECK ok", sorted(r.observe().deployed))
            except AssertionError as e:
                print("CHECK FAIL", str(e)[:600])
            continue
        if s.startswith("!"):
            code, line = s[1:].split(" ", 1)
            step = {"run": line, "exit": int(code)}
        t = time.time()
        try:
            r.step(step)
            print(f"OK   {time.time()-t:5.2f}s {s} -> rc={r.last.returncode}")
        except AssertionError as e:
            print(f"FAIL {time.time()-t:5.2f}s {s}: {str(e)[:2500]}")
