"""Audit distinguishes unmanaged conflicts without modifying target state."""

import json
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.drift import diff_scratch_against_project
from apm_cli.install.drift_render import render_drift_json, render_drift_sarif, render_drift_text
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = pytest.mark.component


@pytest.mark.parametrize("managed", [False, True])
def test_unowned_skill_is_not_managed_drift(tmp_path: Path, managed: bool) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    relative = ".claude/skills/review/SKILL.md"
    for root, content in ((scratch, b"Package content"), (project, b"User content")):
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url="example/review",
            deployed_files=[relative] if managed else [],
        )
    )
    findings = diff_scratch_against_project(scratch, project, lock, [KNOWN_TARGETS["claude"]])
    kind = "modified" if managed else "unmanaged-conflict"
    assert [(finding.path, finding.kind) for finding in findings] == [(relative, kind)]
    assert (project / relative).read_bytes() == b"User content"
    assert (scratch / relative).read_bytes() == b"Package content"
    assert render_drift_json(findings)["drift"][0]["kind"] == kind
    sarif = render_drift_sarif(findings)
    assert sarif[0]["ruleId"] == f"apm/drift/{kind}"
    assert sarif[0]["level"] == "error"
    assert relative in json.dumps(sarif)
    text = render_drift_text(findings)
    if managed:
        assert "Run 'apm install' to re-sync" in text
    else:
        assert "Rename" in text
        assert "cannot repair" in text
        assert "Run 'apm install' to re-sync" not in text
