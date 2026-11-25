"""Legacy skill layout survives audit and uninstall, including a moved COPILOT_HOME (#3773).

``--legacy-skill-paths`` deploys Copilot skills to ``.github/skills`` (project)
or ``<copilot-home>/skills`` (user). Flagless rebuilds (``audit --ci`` drift
replay, uninstall survivor rebuild and cleanup) must use the layout the
install recorded. At user scope ``<copilot-home>`` is ``$COPILOT_HOME`` when
set (#3745), so the recorded layout and the moved home must agree.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration._harness_output_kit import HarnessScene, describe
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=(".github",))
    try:
        yield result
    finally:
        result.close()


def _files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "scope", ["project", "user", "user-copilot-home"], ids=["project", "user", "copilot-home"]
)
def test_legacy_skills_survive_audit_and_uninstall_in_their_recorded_root(
    scene: HarnessScene, scope: str
) -> None:
    user = scope != "project"
    env = dict(scene.env)
    if scope == "user-copilot-home":
        copilot_home = scene.isolated.root / "profiles" / "copilot-home"
        env["COPILOT_HOME"] = str(copilot_home)
    else:
        copilot_home = scene.home / ".copilot"
    skill_root = copilot_home / "skills" if user else scene.project / ".github" / "skills"
    shared_root = (
        scene.home / ".agents" / "skills" if user else scene.project / ".agents" / "skills"
    )
    factory = LocalPackageFactory(scene.isolated.package_root)
    skills = factory.create("skill-kit")
    for name in ("alpha", "beta"):
        factory.add_skill(skills, name, f"---\nname: {name}\ndescription: {name}\n---\nBody.\n")
    other = factory.create("rule-kit")
    factory.add_instruction(other, "house", "---\napplyTo: '**'\ndescription: h\n---\nRule.\n")
    cwd = scene.isolated.config_root if user else scene.project
    scope_flag = ("-g",) if user else ()

    def apm(*args: str):
        return scene.runner.run(args, cwd=cwd, env=env, scenario_id=" ".join(args))

    for package in (skills, other):
        installed = apm(
            "install",
            *scope_flag,
            str(package.root),
            "--no-policy",
            "--target",
            "copilot",
            "--legacy-skill-paths",
        )
        assert installed.returncode == 0, describe(installed)
    assert {"alpha/SKILL.md", "beta/SKILL.md"} <= _files(skill_root), _files(skill_root)
    assert _files(shared_root) == set(), _files(shared_root)
    if scope == "user-copilot-home":
        assert _files(scene.home / ".copilot") == set(), _files(scene.home / ".copilot")

    audit = apm("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, describe(audit)

    survivors = apm("uninstall", *scope_flag, str(other.root))
    assert survivors.returncode == 0, describe(survivors)
    assert {"alpha/SKILL.md", "beta/SKILL.md"} <= _files(skill_root), _files(skill_root)
    assert _files(shared_root) == set(), _files(shared_root)
    audit = apm("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, describe(audit)

    removed = apm("uninstall", *scope_flag, str(skills.root))
    assert removed.returncode == 0, describe(removed)
    assert not {"alpha/SKILL.md", "beta/SKILL.md"} & _files(skill_root), _files(skill_root)
    assert _files(shared_root) == set(), _files(shared_root)
