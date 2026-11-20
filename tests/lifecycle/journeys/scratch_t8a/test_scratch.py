import subprocess, sys, shutil
import pytest
from tests.lifecycle.journeys.twins import publish_twins

pytestmark = [pytest.mark.integration]

def run(world, line, cwd=None):
    import shlex
    args = shlex.split(line)
    if args[0] == "apm":
        cmd = (sys.executable, "-m", "apm_cli.cli", *args[1:])
    else:
        cmd = args
    r = subprocess.run(cmd, cwd=cwd or world.project, env=world.env, capture_output=True, text=True)
    print(f"\n$ {line}\n-- rc={r.returncode}\n-- stdout:\n{r.stdout}\n-- stderr:\n{r.stderr}")
    return r

def test_explore(lifecycle_world, tmp_path):
    w = lifecycle_world
    publish_twins(w.services, tmp_path / "twins", w.env)
    (w.project / "apm.yml").unlink()
    run(w, "git init -q --initial-branch=main")
    run(w, "apm --version")
    run(w, "apm --help")
    run(w, "apm init -y --target copilot,claude")
    print(open(w.project / "apm.yml").read())
    run(w, "apm install microsoft/apm-sample-package#v1.0.0")
    run(w, "git status --porcelain")
    run(w, "apm install")
    run(w, "apm install microsoft/apm/packages/apm-guide#v0.37.0")
    run(w, "git status --porcelain")
    print([e for e in w.services.egress_log()][:50])
