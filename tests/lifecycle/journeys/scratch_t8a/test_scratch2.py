import subprocess, sys, shutil
import pytest
from tests.lifecycle.journeys.twins import publish_twins
from tests.lifecycle.journeys.scratch_t8a.test_scratch import run

pytestmark = [pytest.mark.integration]

def test_explore2(lifecycle_world, tmp_path):
    w = lifecycle_world
    publish_twins(w.services, tmp_path / "twins", w.env)
    (w.project / "apm.yml").unlink()
    run(w, "git init -q --initial-branch=main")
    run(w, "apm init -y --target copilot,claude")
    run(w, "apm install microsoft/apm-sample-package#v1.0.0")
    run(w, "git add apm.yml apm.lock.yaml .gitignore")
    run(w, "git commit -q -m 'Add agent dependencies'")
    run(w, "git add .github .claude .agents")
    run(w, "git commit -q -m 'deployed'")
    run(w, "git status --porcelain")
    run(w, "git clone . ../teammate")
    w.project = w.project.parent / "teammate"
    shutil.rmtree(w.cache_root); w.cache_root.mkdir()
    run(w, "apm install --frozen")
    run(w, "git status --porcelain")
    run(w, "apm audit")
    run(w, "apm find --source .agents/skills/style-checker/SKILL.md")
    run(w, "apm install")
    run(w, "git status --porcelain")
    run(w, "apm install --help")
    run(w, "apm audit --ci -f sarif -o apm.sarif")
    p = w.project / ".agents/skills/style-checker/SKILL.md"
    p.write_text(p.read_text() + "\nlocal edit\n")
    run(w, "apm install --frozen")
    run(w, "apm audit --ci")
    run(w, "git status --porcelain")
