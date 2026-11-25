"""Per-package in-process engagement on Windows, in the bench's real world and a plain user env."""
import logging, os, subprocess, sys, tempfile, time, traceback
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="LOG %(message)s")
for noisy in ("urllib3", "charset_normalizer", "apm_cli.utils", "apm_cli.deps"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
sys.path.insert(0, os.environ["HARNESS_DIR"])
from apm_cli.cache import inprocess_fetch as f  # noqa: E402
from apm_cli.core.invocation_memo import invocation_scope  # noqa: E402
from tests.fixtures.perf.offline import isolated_environment  # noqa: E402
from tests.fixtures.perf.real_network import REAL_PACKAGES, counting_tunnel  # noqa: E402


def specs():
    for spec in REAL_PACKAGES:
        ref = spec.split("#", 1)[1] if "#" in spec else None
        parts = spec.split("#", 1)[0].split("/")
        yield spec, f"https://github.com/{parts[0]}/{parts[1]}", "/".join(parts[2:]), ref


def run(label, env):
    print(f"\n===== WORLD {label}", flush=True)
    for spec, url, sub, ref in specs():
        with invocation_scope():
            out = subprocess.run(["git", "ls-remote", url, ref or "HEAD", f"{ref}^{{}}" if ref else "HEAD"],
                                 capture_output=True, text=True, env=env)
            lines = out.stdout.split("\n")
            sha = (lines[-2] if len(lines) > 2 else lines[0]).split("\t")[0] if out.stdout else ""
            if len(sha) != 40 and ref and len(ref) == 40:
                sha = ref
            staged = Path(tempfile.mkdtemp())
            t = time.monotonic()
            try:
                if sub and not sub.endswith(".md"):
                    got = f.fetch_sparse_worktree(url, sha, staged, env, (), (sub,))
                    kind = "sparse"
                else:
                    got = f.fetch_worktree(url, sha, staged, env, ())
                    kind = "full"
                print(f"RESULT {spec} {kind} sha={sha[:8]} {'HIT' if got else 'NONE'} {time.monotonic()-t:.2f}s git_rc={out.returncode}", flush=True)
            except Exception:
                traceback.print_exc()


run("plain user env", dict(os.environ))
_, base = isolated_environment(Path(tempfile.mkdtemp()) / "env")
with counting_tunnel() as tunnel:
    run("bench real world (isolated + tunnel)", tunnel.child_environment(base))
