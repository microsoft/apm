"""Real-CLI lifecycle: skill re-integration keeps the layout the install used (#3773).

``apm install --legacy-skill-paths`` deploys skills to per-client roots
(``.github/skills`` at project scope, ``~/.copilot/skills`` at user scope)
instead of the shared ``.agents/skills``. Every later path that re-integrates
skills without the flag -- the ``apm audit --ci`` drift replay and the
uninstall survivor rebuild -- must deploy to the same layout, derived from the
recorded deployments, so a clean legacy install is not reported as drift and
survivors are not copied into ``.agents/skills`` unrecorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.targets import USER_ROOT_ENV_VARS
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.skill_selection_world import (
    AUDIT_CI,
    INSTALL,
    SkillSelectionWorld,
    audit_failures,
    locked,
    skill_dirs,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_SKILLS = ("alpha", "beta", "gamma", "delta")
_SHARED_SKILLS = Path(".agents") / "skills"
_GLOBAL_INSTALL = ("install", "-g", "--no-policy", "--parallel-downloads", "0")
_LAYOUTS = {
    # layout -> (install flags, project skills root, user skills root)
    "legacy": (("--legacy-skill-paths",), Path(".github") / "skills", Path(".copilot") / "skills"),
    "default": ((), _SHARED_SKILLS, _SHARED_SKILLS),
}


def _world(tmp_path: Path, apm_binary_path: Path) -> SkillSelectionWorld:
    return SkillSelectionWorld.create(tmp_path / "w", (str(apm_binary_path),))


def _assert_audit_green(world: SkillSelectionWorld, cwd: Path, env: dict[str, str], sid: str):
    result = world.run(AUDIT_CI, cwd=cwd, env=env, expect=None, scenario_id=sid)
    assert result.returncode == 0, (audit_failures(result), result.stderr[-2000:])


def _user_manifest(world: SkillSelectionWorld, dependencies: list[object]) -> None:
    dump_yaml(
        {
            "name": "user",
            "version": "0.1.0",
            "description": "user scope manifest",
            "author": "APM Test",
            "dependencies": {"apm": dependencies},
            "targets": ["copilot"],
        },
        world.isolated.config_root / "apm.yml",
    )


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_project_skill_layout_survives_audit_and_uninstall(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    """Install, audit, uninstall an unrelated package, audit, uninstall the bundle."""
    flags, skills_rel, _user_rel = _LAYOUTS[layout]
    world = _world(tmp_path, apm_binary_path)
    bundle = world.publish("bundle", skills=("alpha", "beta")).slug
    c = world.publish("pkg-c", instructions=["c-rules"]).slug
    project = world.project("consumer", dependencies=[bundle, c])
    skills_root = project / skills_rel
    with world.serve() as env:
        world.run((*INSTALL, *flags), cwd=project, env=env, scenario_id="pl-install")
        assert skill_dirs(skills_root) == ["alpha", "beta"]
        recorded = set(locked(project, bundle)["deployed_files"])
        _assert_audit_green(world, project, env, "pl-audit")

        world.run(("uninstall", c), cwd=project, env=env, scenario_id="pl-uninstall-c")
        assert skill_dirs(skills_root) == ["alpha", "beta"]
        if layout == "legacy":
            assert skill_dirs(project / _SHARED_SKILLS) == []
        assert set(locked(project, bundle)["deployed_files"]) == recorded
        _assert_audit_green(world, project, env, "pl-audit-after-c")

        world.run(("uninstall", bundle), cwd=project, env=env, scenario_id="pl-uninstall-b")
        assert skill_dirs(skills_root) == []


def test_legacy_skill_layout_still_reports_real_drift(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A hand-edited legacy-layout skill is still drift."""
    world = _world(tmp_path, apm_binary_path)
    bundle = world.publish("bundle", skills=("alpha",)).slug
    project = world.project("consumer", dependencies=[bundle])
    with world.serve() as env:
        world.run((*INSTALL, "--legacy-skill-paths"), cwd=project, env=env, scenario_id="ld-in")
        skill = project / ".github" / "skills" / "alpha" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
        result = world.run(AUDIT_CI, cwd=project, env=env, expect=None, scenario_id="ld-audit")
    assert result.returncode != 0
    details = "\n".join(audit_failures(result).get("drift", []))
    assert ".github/skills/alpha/SKILL.md" in details, audit_failures(result)
    assert ".agents/skills" not in details, details


@pytest.mark.xfail(
    strict=True,
    reason="#3760: uninstall re-deploys every skill of a surviving bundle (fixed by #3784)",
)
def test_uninstall_of_an_unrelated_package_keeps_the_legacy_skill_layout(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """U1 x legacy layout: survivors are re-integrated where install put them, nothing unrecorded."""
    world = _world(tmp_path, apm_binary_path)
    bundle = world.publish("bundle", skills=_SKILLS).slug
    c = world.publish("pkg-c", instructions=["c-rules"]).slug
    project = world.project("consumer", dependencies=[{"git": bundle, "skills": ["alpha"]}, c])
    legacy_root = project / ".github" / "skills"
    with world.serve() as env:
        world.run((*INSTALL, "--legacy-skill-paths"), cwd=project, env=env, scenario_id="ul-in")
        assert skill_dirs(legacy_root) == ["alpha"]
        recorded = set(locked(project, bundle)["deployed_files"])
        world.run(("uninstall", c), cwd=project, env=env, scenario_id="ul-rm")
    assert skill_dirs(legacy_root) == ["alpha"]
    assert skill_dirs(project / _SHARED_SKILLS) == []
    assert set(locked(project, bundle)["deployed_files"]) == recorded


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_user_skill_layout_survives_audit_and_uninstall(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    """-g: legacy skills stay under ~/.copilot/skills through audit and uninstall."""
    flags, _project_rel, skills_rel = _LAYOUTS[layout]
    world = _world(tmp_path, apm_binary_path)
    bundle = world.publish("bundle", skills=("alpha", "beta")).slug
    c = world.publish("pkg-c", instructions=["c-rules"]).slug
    _user_manifest(world, [bundle, c])
    apm_home = world.isolated.config_root
    home = world.isolated.home
    skills_root = home / skills_rel
    cwd = world.isolated.work_root
    with world.serve() as env:
        world.run((*_GLOBAL_INSTALL, *flags), cwd=cwd, env=env, scenario_id="ul-g-install")
        assert skill_dirs(skills_root) == ["alpha", "beta"]
        _assert_audit_green(world, apm_home, env, "ul-g-audit")

        world.run(("uninstall", "-g", c), cwd=cwd, env=env, scenario_id="ul-g-uninstall-c")
        assert skill_dirs(skills_root) == ["alpha", "beta"]
        if layout == "legacy":
            assert skill_dirs(home / _SHARED_SKILLS) == []
        _assert_audit_green(world, apm_home, env, "ul-g-audit-after-c")

        world.run(("uninstall", "-g", bundle), cwd=cwd, env=env, scenario_id="ul-g-uninstall-b")
        assert skill_dirs(skills_root) == []


@pytest.mark.skipif(
    "copilot" not in USER_ROOT_ENV_VARS,
    reason="needs the COPILOT_HOME user-root owner from #3782",
)
def test_copilot_home_legacy_skill_layout_survives_audit_and_uninstall(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """-g with COPILOT_HOME: legacy skills stay in $COPILOT_HOME/skills, never ~/.agents/skills."""
    world = _world(tmp_path, apm_binary_path)
    copilot_home = world.isolated.root / "elsewhere" / "copilot-profile"
    copilot_home.mkdir(parents=True)
    world.extra_env["COPILOT_HOME"] = str(copilot_home)
    bundle = world.publish("bundle", skills=("alpha", "beta")).slug
    c = world.publish("pkg-c", instructions=["c-rules"]).slug
    _user_manifest(world, [bundle, c])
    apm_home = world.isolated.config_root
    home = world.isolated.home
    skills_root = copilot_home / "skills"
    cwd = world.isolated.work_root
    with world.serve() as env:
        world.run(
            (*_GLOBAL_INSTALL, "--legacy-skill-paths"), cwd=cwd, env=env, scenario_id="ch-install"
        )
        assert skill_dirs(skills_root) == ["alpha", "beta"]
        _assert_audit_green(world, apm_home, env, "ch-audit")
        world.run(("uninstall", "-g", c), cwd=cwd, env=env, scenario_id="ch-uninstall-c")
        assert skill_dirs(skills_root) == ["alpha", "beta"]
        _assert_audit_green(world, apm_home, env, "ch-audit-after-c")
        world.run(("uninstall", "-g", bundle), cwd=cwd, env=env, scenario_id="ch-uninstall-b")
        assert skill_dirs(skills_root) == []
    assert skill_dirs(home / _SHARED_SKILLS) == []
    assert not (home / ".copilot").exists()
