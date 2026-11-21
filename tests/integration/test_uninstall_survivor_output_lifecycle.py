"""Uninstall prints no unattributed survivor integration lines (#4160, #4219).

PROMISE: P-INV-I9 -- no duplicate or noisy lines; uninstall reports what it
removed. After ``apm uninstall`` removes one package, the surviving packages
are redeployed from their recorded selection. That rebuild must not print
the survivors' integration tree lines ("|-- Skill integrated -> ...") with
no package header naming them, once per surviving package.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I9", "P-LAW-outcome.status_matches_state"),
]


def _skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: Survivor skill {name}.\n---\n\nUse {name}.\n"


def test_uninstall_prints_no_unattributed_survivor_lines(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
    project = isolated.work_root / "proj"
    project.mkdir()
    (project / "apm.yml").write_text("name: proj\nversion: 1.0.0\n", encoding="ascii")
    factory = LocalPackageFactory(project)
    child = factory.create("p-child", version="1.0.0")
    factory.add_skill(child, "p-child", _skill("p-child"))
    for name in ("p-parent", "p-parent2"):
        parent = factory.create(name, version="1.0.0")
        factory.add_skill(parent, name, _skill(name))
        factory.add_relative_dependency(parent, child)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    env = isolated.subprocess_env()

    install = runner.run(
        ("install", "./p-parent", "./p-parent2", "--target", "copilot", "--no-policy"),
        cwd=project,
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    survivor_skill = project / ".agents" / "skills" / "p-parent2" / "SKILL.md"
    assert survivor_skill.is_file(), install.stdout

    result = runner.run(("uninstall", "./p-parent"), cwd=project, env=env)

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    tree_lines = [line.strip() for line in out.splitlines() if line.strip().startswith("|--")]
    assert tree_lines == [], out
    assert survivor_skill.is_file(), out
    assert not (project / ".agents" / "skills" / "p-parent").exists(), out
