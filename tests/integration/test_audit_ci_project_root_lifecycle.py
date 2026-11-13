"""``apm audit --ci`` never passes without a manifest to audit (#3688).

APM resolves the project from the working directory and does not search
parent directories. Run from a subdirectory of an installed project, or from
an empty checkout path, the CI gate used to report "All checks passed" and
exit 0 although nothing was audited. It must fail with ``manifest-missing``
and name the fix, while the same gate from the project root stays green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration._path_corner_world_kit import CornerWorld, output, tree_bytes, write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

CI = ("audit", "--ci", "--no-policy")


def test_audit_ci_fails_outside_the_project_root(tmp_path: Path, apm_binary_path: Path) -> None:
    world = CornerWorld.create(tmp_path, apm_binary_path)
    local = world.isolated.work_root / "local-pkg"
    write(local / "apm.yml", "name: local-pkg\nversion: 1.0.0\ndescription: l\n")
    write(local / ".apm/skills/loc/SKILL.md", "---\nname: loc\ndescription: l\n---\n# loc\n")
    project = world.project("consumer", targets=("claude",), deps=({"path": "../local-pkg"},))
    (project / ".claude").mkdir()
    world.apm(project, "install", "--no-policy")
    world.apm(project, *CI)

    subdir = project / ".claude" / "skills" / "loc"
    empty = world.isolated.work_root / "empty checkout"
    empty.mkdir()
    before = tree_bytes(project)
    for cwd in (subdir, empty):
        result = world.apm(cwd, *CI, expect=1)
        assert "manifest-missing" in output(result), output(result)

        report = world.apm(cwd, *CI, "--format", "json", expect=1)
        checks = {check["name"]: check for check in json.loads(report.stdout)["checks"]}
        missing = checks["manifest-missing"]
        assert missing["passed"] is False, report.stdout
        assert "nothing was audited" in missing["message"], missing
        assert "project root that contains apm.yml" in missing["message"], missing
    assert tree_bytes(project) == before
    assert sorted(empty.iterdir()) == []
