"""Malformed frontmatter refuses the package and names the file and the fault (#5216).

An instruction's ``applyTo`` scope must fail closed, so a malformed instruction
refuses its whole package; a root ``SKILL.md`` package with malformed frontmatter
is reported invalid. Either way the diagnostic names the file and the offending
key (or, for broken YAML, where in the frontmatter it breaks), and nothing from
the package is deployed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-frontmatter-bom-bounded-yaml"),
]

_FAULTS = {
    "broken-yaml": ("applyTo: [unclosed\n", "frontmatter line 2"),
    "reserved-key": ("applyTo: '**'\ncontent: x\n", "reserved frontmatter key(s): 'content'"),
}


def _install(tmp_path: Path, apm_binary_path: Path, package: Path) -> tuple[int, str, Path]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir(parents=True)
    (project / "apm.yml").write_text(
        f"name: consumer\nversion: 1.0.0\ntargets: [copilot]\n"
        f"dependencies:\n  apm:\n    - {package.as_posix()}\n",
        encoding="utf-8",
    )
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"), cwd=project, env=isolated.subprocess_env()
    )
    return result.returncode, " ".join((result.stdout + result.stderr).split()), project


@pytest.mark.parametrize("fault", sorted(_FAULTS))
def test_a_malformed_instruction_refuses_its_package_by_name(
    tmp_path: Path, apm_binary_path: Path, fault: str
) -> None:
    block, named = _FAULTS[fault]
    package = tmp_path / "pkg"
    rules = package / ".apm" / "instructions"
    rules.mkdir(parents=True)
    (package / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
    (rules / "bad.instructions.md").write_text(f"---\n{block}---\n# Bad\n", encoding="utf-8")
    (rules / "good.instructions.md").write_text("---\napplyTo: '**'\n---\n# Good\n")

    code, text, project = _install(tmp_path, apm_binary_path, package)

    assert code == 1, text
    assert "bad.instructions.md" in text and named in text, text
    assert not (project / ".github" / "instructions").exists()


@pytest.mark.parametrize("fault", sorted(_FAULTS))
def test_a_malformed_root_skill_is_reported_invalid_by_name(
    tmp_path: Path, apm_binary_path: Path, fault: str
) -> None:
    block, named = _FAULTS[fault]
    package = tmp_path / "skill-pkg"
    package.mkdir()
    (package / "SKILL.md").write_text(f"---\nname: s\n{block}---\n# Skill\n", encoding="utf-8")

    code, text, project = _install(tmp_path, apm_binary_path, package)

    named = named.replace("line 2", "line 3")
    assert code == 1, text
    assert "SKILL.md" in text and named in text, text
    assert not (project / ".agents" / "skills").exists()
    assert not (project / ".github" / "skills").exists()
