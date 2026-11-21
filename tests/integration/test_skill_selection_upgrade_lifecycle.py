"""Real-CLI upgrade paths for multi-parent selections (#3735, #3760; 0.36.1 T4).

Checkouts captured from 0.35.1 and 0.36.0 (tests/fixtures/upgrade_0361/t4/)
carry first-wins selections and, for the uninstall leak, unrecorded skill
directories. The approved T4 matrix is the oracle: a plain install rewrites
the lock to the union (visible diff confined to the merged dependency);
``install --frozen`` on a first-wins lock fails with remediation text (D6);
``audit --ci`` reports the stale selection (D5); skills leaked by earlier
releases stay unmanaged on disk and are never claimed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.yaml_io import load_yaml
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.skill_selection_world import (
    AUDIT_CI,
    INSTALL,
    UPGRADE_SKILLS,
    SkillSelectionWorld,
    audit_failures,
    build_upgrade_world,
    copy_capture,
    lock_path,
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

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upgrade_0361" / "t4"
_VERSIONS = ("0.35.1", "0.36.0")
_BUNDLE = "fixture-org/bundle"
_AGENTS = Path(".agents") / "skills"
_CLAUDE = Path(".claude") / "skills"


def _restore(tmp_path: Path, apm_binary_path: Path, version: str, scenario: str):
    world = SkillSelectionWorld.create(tmp_path / "w", (str(apm_binary_path),))
    build_upgrade_world(world, scenario)
    project = world.isolated.work_root / "consumer"
    copy_capture(_FIXTURES / version / scenario, project)
    old = locked(project, _BUNDLE)
    assert old is not None
    return world, project


def _entries_except_bundle(path: Path) -> list[dict[str, object]]:
    data = load_yaml(path)
    return [e for e in data["dependencies"] if e["repo_url"] != _BUNDLE]


def _without_content_hash(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{k: v for k, v in e.items() if k != "content_hash"} for e in entries]


@pytest.mark.parametrize("version", _VERSIONS)
@pytest.mark.parametrize("scenario", ["divergent-diamond", "target-subset"])
def test_frozen_install_on_an_old_first_wins_lock_fails_with_remediation(
    tmp_path: Path, apm_binary_path: Path, version: str, scenario: str
) -> None:
    """D6: CI replay of a first-wins lock stops and tells the user to re-lock."""
    world, project = _restore(tmp_path, apm_binary_path, version, scenario)
    before = lock_path(project).read_bytes()
    with world.serve() as env:
        result = world.run(
            (*INSTALL, "--frozen"), cwd=project, env=env, expect=None, scenario_id="up-frozen"
        )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output[-3000:]
    assert _BUNDLE in output and "apm install" in output, output[-3000:]
    assert lock_path(project).read_bytes() == before


@pytest.mark.parametrize("version", _VERSIONS)
def test_audit_ci_flags_an_old_first_wins_skill_selection(
    tmp_path: Path, apm_binary_path: Path, version: str
) -> None:
    """D5: the first-wins transitive skill_subset is reported, not silently accepted."""
    world, project = _restore(tmp_path, apm_binary_path, version, "divergent-diamond")
    with world.serve() as env:
        result = world.run(AUDIT_CI, cwd=project, env=env, expect=None, scenario_id="up-audit")
    assert result.returncode != 0, result.stdout[-3000:]
    assert "skill-subset-consistency" in audit_failures(result)


@pytest.mark.parametrize("version", _VERSIONS)
def test_install_rewrites_an_old_diamond_lock_to_the_union_then_uninstall_shrinks(
    tmp_path: Path, apm_binary_path: Path, version: str
) -> None:
    """Med row: visible lock diff to the union, limited to the merged dependency."""
    world, project = _restore(tmp_path, apm_binary_path, version, "divergent-diamond")
    others_before = _entries_except_bundle(lock_path(project))
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="up-install")
        assert skill_dirs(project / _AGENTS) == sorted(UPGRADE_SKILLS)
        assert locked(project, _BUNDLE)["skill_subset"] == sorted(UPGRADE_SKILLS)
        # Besides the union, the only diff is the one-time move to lockfile
        # version 4 (hash framing hardening, #4000): every other row is
        # re-hashed in the framed stream from its verified installed bytes.
        others_after = _entries_except_bundle(lock_path(project))
        assert _without_content_hash(others_after) == _without_content_hash(others_before)
        assert load_yaml(lock_path(project))["lockfile_version"] == "4"
        for entry in others_after:
            installed = (
                LockedDependency.from_dict(entry)
                .to_dependency_ref()
                .get_install_path(project / "apm_modules")
            )
            assert entry["content_hash"] == compute_package_hash(installed, framed=True)
        audit = world.run(AUDIT_CI, cwd=project, env=env, expect=None, scenario_id="up-audit")
        assert audit.returncode == 0, audit_failures(audit)
        world.run(
            ("update", "--yes", "--parallel-downloads", "0"),
            cwd=project,
            env=env,
            scenario_id="up-update",
        )
        assert locked(project, _BUNDLE)["skill_subset"] == sorted(UPGRADE_SKILLS)
        world.run(("uninstall", "fixture-org/pkg-a"), cwd=project, env=env, scenario_id="up-rm")
        assert skill_dirs(project / _AGENTS) == ["delta", "gamma"]
        assert locked(project, _BUNDLE)["skill_subset"] == ["delta", "gamma"]


@pytest.mark.parametrize("version", _VERSIONS)
def test_install_widens_an_old_target_subset_lock_to_both_harnesses(
    tmp_path: Path, apm_binary_path: Path, version: str
) -> None:
    world, project = _restore(tmp_path, apm_binary_path, version, "target-subset")
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="up-targets")
        audit = world.run(AUDIT_CI, cwd=project, env=env, expect=None, scenario_id="up-t-audit")
    assert skill_dirs(project / _CLAUDE) == sorted(UPGRADE_SKILLS)
    assert skill_dirs(project / _AGENTS) == sorted(UPGRADE_SKILLS)
    assert locked(project, _BUNDLE)["target_subset"] == ["claude", "copilot"]
    assert audit.returncode == 0, audit_failures(audit)


@pytest.mark.parametrize("version", _VERSIONS)
def test_old_alias_lock_keeps_its_winner_replays_frozen_and_warns_on_install(
    tmp_path: Path, apm_binary_path: Path, version: str
) -> None:
    """D4: the declaration-order winner equals the old first-wins alias, so no churn."""
    world, project = _restore(tmp_path, apm_binary_path, version, "alias")
    with world.serve() as env:
        world.run((*INSTALL, "--frozen"), cwd=project, env=env, scenario_id="up-alias-frozen")
        result = world.run(INSTALL, cwd=project, env=env, scenario_id="up-alias-install")
    assert locked(project, _BUNDLE)["alias"] == "bun-a"
    output = result.stdout + result.stderr
    assert "bun-a" in output and "bun-b" in output, output[-3000:]


@pytest.mark.parametrize("version", _VERSIONS)
def test_skills_leaked_by_an_old_uninstall_stay_unmanaged(
    tmp_path: Path, apm_binary_path: Path, version: str
) -> None:
    """U1 residue: APM never claims or deletes skill dirs it did not record."""
    world, project = _restore(tmp_path, apm_binary_path, version, "u1-leaked")
    leaked = ["beta", "delta", "gamma"]
    leaked_bytes = {n: (project / _AGENTS / n / "SKILL.md").read_bytes() for n in leaked}
    with world.serve() as env:
        world.run((*INSTALL, "--frozen"), cwd=project, env=env, scenario_id="up-u1-frozen")
        world.run(INSTALL, cwd=project, env=env, scenario_id="up-u1-install")
        audit = world.run(AUDIT_CI, cwd=project, env=env, expect=None, scenario_id="up-u1-a")
        assert audit.returncode == 0, audit_failures(audit)
        entry = locked(project, _BUNDLE)
        assert entry["skill_subset"] == ["alpha"]
        assert not any(n in f for f in entry["deployed_files"] for n in leaked)
        world.run(("uninstall", _BUNDLE), cwd=project, env=env, scenario_id="up-u1-rm")
    assert skill_dirs(project / _AGENTS) == leaked
    for name, content in leaked_bytes.items():
        assert (project / _AGENTS / name / "SKILL.md").read_bytes() == content
