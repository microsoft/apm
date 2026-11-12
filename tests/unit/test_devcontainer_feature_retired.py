"""Guards for #3520: the unpublished Dev Container Feature stays retired.

APM declined an official Dev Container Feature (#1273). No tracked file may
advertise the never-published GHCR reference, no workflow may build or test a
dev container feature, and the installation guide must keep its dev container
recipe.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_UNPUBLISHED_FEATURE_REF = re.compile(r"ghcr\.io/microsoft/apm/apm-cli")
_FEATURE_WORKFLOW_MARKERS = re.compile(
    r"devcontainers/action|devcontainer features test|devcontainer-feature|devcontainer/src"
)
_HISTORY_FILES = {"CHANGELOG.md"}


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def test_no_tracked_file_advertises_unpublished_feature():
    offenders = []
    for rel in _tracked_files():
        if rel in _HISTORY_FILES:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        if _UNPUBLISHED_FEATURE_REF.search(text):
            offenders.append(rel)
    assert offenders == []


def test_no_feature_sources_or_workflows_remain():
    tracked = _tracked_files()
    assert [p for p in tracked if p.startswith("devcontainer/")] == []
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
    assert workflows, "workflow directory not found"
    offenders = [
        wf.name
        for wf in workflows
        if _FEATURE_WORKFLOW_MARKERS.search(wf.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_installation_docs_describe_dev_container_recipe():
    docs = (
        REPO_ROOT / "docs" / "src" / "content" / "docs" / "getting-started" / "installation.md"
    ).read_text(encoding="utf-8")
    section = docs.split("\n## Dev containers\n", 1)
    assert len(section) == 2, "installation guide lost its Dev containers section"
    body = section[1].split("\n## ", 1)[0]
    assert "postCreateCommand" in body
    assert "pip install apm-cli" in body
    assert "maintained outside this project" in body
