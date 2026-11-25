"""The real `apm install` of the bench's real consumer on Windows, with in-process debug lines."""
import os, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, os.environ["HARNESS_DIR"])
from tests.fixtures.perf.generator import consumer_manifest  # noqa: E402
from tests.fixtures.perf.offline import isolated_environment  # noqa: E402
from tests.fixtures.perf.real_network import REAL_PACKAGES, counting_tunnel  # noqa: E402

APM = os.environ["NEW_APM"]


def install(label, env):
    project = Path(tempfile.mkdtemp()) / "proj"
    project.mkdir()
    (project / "apm.yml").write_text(consumer_manifest("real-consumer", list(REAL_PACKAGES), ("copilot",)))
    run_env = dict(env, APM_LOG_LEVEL="DEBUG", APM_CACHE_DIR=str(Path(tempfile.mkdtemp()) / "cache"))
    print(f"\n===== CLI {label}", flush=True)
    t = time.monotonic()
    r = subprocess.run([APM, "install"], cwd=project, env=run_env, capture_output=True, text=True, errors="replace")
    print(f"rc={r.returncode} {time.monotonic() - t:.1f}s")
    out = r.stdout + r.stderr
    keep = [l for l in out.splitlines() if any(k in l for k in ("in-process", "inprocess", "fallback", "sparse", "Cache HIT", "Traceback", "Error", "[x]"))]
    print("\n".join(keep[:120]) or "(no in-process lines)")
    print("--- tail ---")
    print(out[-2500:], flush=True)


install("plain user env", dict(os.environ))
_, base = isolated_environment(Path(tempfile.mkdtemp()) / "env")
with counting_tunnel() as tunnel:
    install("bench real world", tunnel.child_environment(base))
