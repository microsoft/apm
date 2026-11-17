"""Real-CLI lifecycle: one dependency selected by several parents (0.36.1, T4).

Covers #3735: a package reached through several edges that select different
skills, targets or aliases kept only the first edge's selection. The approved
0.36.1 T4 design is the oracle: per-field union across declaring edges, "all" absorbs,
order-independent; uninstall shrinks to the surviving union and keeps the
locked union when reachability evidence is incomplete; audit recomputes the
expected selection for every locked dependency; alias divergence keeps the
deterministic shallowest/declaration-order winner and warns.

Every package uses a remote-shaped ``github.com`` identity served from owned
local repositories, so root and transitive declarations share one key.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.skill_selection_world import (
    AUDIT_CI,
    INSTALL,
    SkillSelectionWorld,
    audit_failures,
    deployment_values,
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

_SKILLS = ("alpha", "beta", "gamma", "delta")
_PROJECT_SKILLS = Path(".agents") / "skills"
_CLAUDE_SKILLS = Path(".claude") / "skills"


def _world(tmp_path: Path, apm_binary_path: Path, name: str = "w") -> SkillSelectionWorld:
    return SkillSelectionWorld.create(tmp_path / name, (str(apm_binary_path),))


def _edge(world_bundle_slug: str, **fields: object) -> dict[str, object]:
    return {"git": world_bundle_slug, **fields}


def _diamond(
    world: SkillSelectionWorld,
    a_fields: dict[str, object],
    b_fields: dict[str, object],
) -> tuple[str, str, str]:
    """Publish bundle + parents pkg-a and pkg-b; return (bundle, a, b) slugs."""
    bundle = world.publish("bundle", skills=_SKILLS).slug
    a = world.publish("pkg-a", dependencies=[_edge(bundle, **a_fields)]).slug
    b = world.publish("pkg-b", dependencies=[_edge(bundle, **b_fields)]).slug
    return bundle, a, b


def _assert_audit_green(world: SkillSelectionWorld, cwd: Path, env: dict[str, str], sid: str):
    result = world.run(AUDIT_CI, cwd=cwd, env=env, expect=None, scenario_id=sid)
    assert result.returncode == 0, (audit_failures(result), result.stderr[-2000:])


def test_diamond_parents_deploy_the_union_of_their_skill_selections_in_any_manifest_order(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L1: two parents select disjoint skills of one bundle; both orders converge."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"skills": ["alpha", "beta"]}, {"skills": ["gamma", "delta"]})
    forward = world.project("forward", dependencies=[a, b])
    reverse = world.project("reverse", dependencies=[b, a])
    with world.serve() as env:
        for project in (forward, reverse):
            world.run(INSTALL, cwd=project, env=env, scenario_id=f"l1-{project.name}")
            assert skill_dirs(project / _PROJECT_SKILLS) == sorted(_SKILLS)
            entry = locked(project, bundle)
            assert entry is not None
            assert entry.get("skill_subset") == sorted(_SKILLS)
            _assert_audit_green(world, project, env, f"l1-audit-{project.name}")
    forward_lock = lock_path(forward).read_bytes()
    reverse_lock = lock_path(reverse).read_bytes()
    assert forward_lock == reverse_lock, "lockfile depends on manifest declaration order"


def test_depth_three_chain_keeps_both_ancestor_skill_selections(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L2: consumer -> pkg-b -> pkg-a -> bundle, with pkg-b also selecting bundle skills."""
    world = _world(tmp_path, apm_binary_path)
    bundle = world.publish("bundle", skills=_SKILLS).slug
    a = world.publish("pkg-a", dependencies=[_edge(bundle, skills=["alpha", "beta"])]).slug
    b = world.publish("pkg-b", dependencies=[a, _edge(bundle, skills=["gamma", "delta"])]).slug
    project = world.project("consumer", dependencies=[b])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l2-install")
        assert skill_dirs(project / _PROJECT_SKILLS) == sorted(_SKILLS)
        assert locked(project, bundle)["skill_subset"] == sorted(_SKILLS)
        _assert_audit_green(world, project, env, "l2-audit")


@pytest.mark.parametrize("narrow_first", [True, False], ids=["narrow-first", "whole-first"])
def test_parent_asking_for_whole_bundle_is_not_starved_by_a_narrowing_sibling(
    tmp_path: Path, apm_binary_path: Path, narrow_first: bool
) -> None:
    """L3: pkg-a selects [alpha], pkg-b takes the whole bundle -> every skill, no subset."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"skills": ["alpha"]}, {})
    project = world.project("consumer", dependencies=[a, b] if narrow_first else [b, a])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l3-install")
        assert skill_dirs(project / _PROJECT_SKILLS) == sorted(_SKILLS)
        entry = locked(project, bundle)
        assert not entry.get("skill_subset"), entry.get("skill_subset")
        _assert_audit_green(world, project, env, "l3-audit")


def test_root_skill_selection_merges_with_a_transitive_selection_and_audit_passes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L4: root declares bundle [alpha]; pkg-b declares bundle [gamma]."""
    world = _world(tmp_path, apm_binary_path)
    bundle = world.publish("bundle", skills=_SKILLS).slug
    b = world.publish("pkg-b", dependencies=[_edge(bundle, skills=["gamma"])]).slug
    project = world.project("consumer", dependencies=[_edge(bundle, skills=["alpha"]), b])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l4-install")
        assert skill_dirs(project / _PROJECT_SKILLS) == ["alpha", "gamma"]
        assert locked(project, bundle)["skill_subset"] == ["alpha", "gamma"]
        _assert_audit_green(world, project, env, "l4-audit")


def test_dev_declaration_selection_merges_into_the_production_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A devDependencies edge still contributes its skills; production wins is_dev."""
    world = _world(tmp_path, apm_binary_path)
    bundle = world.publish("bundle", skills=_SKILLS).slug
    project = world.project(
        "consumer",
        dependencies=[_edge(bundle, skills=["alpha"])],
        dev_dependencies=[_edge(bundle, skills=["beta"])],
    )
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="dev-install")
        assert skill_dirs(project / _PROJECT_SKILLS) == ["alpha", "beta"]
        entry = locked(project, bundle)
        assert entry["skill_subset"] == ["alpha", "beta"]
        assert not entry.get("is_dev")
        _assert_audit_green(world, project, env, "dev-audit")


def test_parents_restricting_different_targets_populate_both_harness_roots(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L5: pkg-a restricts bundle to claude, pkg-b to copilot; consumer targets both."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"targets": ["claude"]}, {"targets": ["copilot"]})
    project = world.project("consumer", dependencies=[a, b], targets=("claude", "copilot"))
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l5-install")
        assert skill_dirs(project / _CLAUDE_SKILLS) == sorted(_SKILLS)
        assert skill_dirs(project / _PROJECT_SKILLS) == sorted(_SKILLS)
        assert locked(project, bundle).get("target_subset") == ["claude", "copilot"]
        _assert_audit_green(world, project, env, "l5-audit")


def test_divergent_aliases_keep_the_declaration_order_winner_and_warn(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """D4: two parents alias the same bundle differently -> deterministic winner + warning."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"alias": "bun-a"}, {"alias": "bun-b"})
    project = world.project("consumer", dependencies=[a, b])
    with world.serve() as env:
        result = world.run(INSTALL, cwd=project, env=env, scenario_id="alias-install")
    output = result.stdout + result.stderr
    assert locked(project, bundle).get("alias") == "bun-a"
    assert "bun-a" in output and "bun-b" in output, output[-3000:]
    assert "alias" in output.lower()


def test_uninstalling_one_diamond_parent_shrinks_to_the_survivor_selection_then_cleans(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L6: uninstall pkg-a leaves only pkg-b's skills; uninstall pkg-b removes the bundle."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"skills": ["alpha", "beta"]}, {"skills": ["gamma", "delta"]})
    project = world.project("consumer", dependencies=[a, b])
    skills_root = project / _PROJECT_SKILLS
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l6-install")
        assert skill_dirs(skills_root) == sorted(_SKILLS)

        world.run(("uninstall", a), cwd=project, env=env, scenario_id="l6-uninstall-a")
        assert skill_dirs(skills_root) == ["delta", "gamma"]
        entry = locked(project, bundle)
        assert entry["skill_subset"] == ["delta", "gamma"]
        recorded = deployment_values(project)
        for gone in ("alpha", "beta"):
            assert not any(f"/skills/{gone}" in value for value in recorded), recorded
            assert not any(f"/skills/{gone}" in f for f in entry.get("deployed_files") or [])
        _assert_audit_green(world, project, env, "l6-audit-after-a")

        world.run(("uninstall", b), cwd=project, env=env, scenario_id="l6-uninstall-b")
        assert skill_dirs(skills_root) == []
        assert locked(project, bundle) is None


def test_uninstall_keeps_the_locked_union_when_a_survivor_manifest_is_missing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L6 incomplete reachability: missing survivor evidence is not proof of a smaller set."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"skills": ["alpha", "beta"]}, {"skills": ["gamma", "delta"]})
    project = world.project("consumer", dependencies=[a, b])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l6i-install")
        assert skill_dirs(project / _PROJECT_SKILLS) == sorted(_SKILLS)
        survivor_manifest = project / "apm_modules" / "fixture-org" / "pkg-b" / "apm.yml"
        assert survivor_manifest.is_file()
        survivor_manifest.unlink()
        world.run(("uninstall", a), cwd=project, env=env, scenario_id="l6i-uninstall-a")
    assert locked(project, bundle)["skill_subset"] == sorted(_SKILLS)
    assert skill_dirs(project / _PROJECT_SKILLS) == sorted(_SKILLS)


def test_frozen_install_replays_a_union_lock_without_rewriting_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L8a: a lock written by the merging build replays byte-identically under --frozen."""
    world = _world(tmp_path, apm_binary_path)
    _bundle, a, b = _diamond(world, {"skills": ["alpha", "beta"]}, {"skills": ["gamma", "delta"]})
    project = world.project("consumer", dependencies=[a, b])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l8a-install")
        before = lock_path(project).read_bytes()
        shutil.rmtree(project / "apm_modules")
        shutil.rmtree(project / ".agents")
        world.run((*INSTALL, "--frozen"), cwd=project, env=env, scenario_id="l8a-frozen")
    assert lock_path(project).read_bytes() == before
    assert skill_dirs(project / _PROJECT_SKILLS) == sorted(_SKILLS)


def test_frozen_install_rejects_a_first_wins_lock_with_an_actionable_reason(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L8b (D6): a lock narrowed to one parent's selection fails --frozen, then plain install converges."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"skills": ["alpha", "beta"]}, {"skills": ["gamma", "delta"]})
    project = world.project("consumer", dependencies=[a, b])
    with world.serve() as env:
        # Produce a first-wins lock: install with only pkg-a, then add pkg-b to apm.yml.
        manifest_path = project / "apm.yml"
        manifest = load_yaml(manifest_path)
        manifest["dependencies"]["apm"] = [a]
        dump_yaml(manifest, manifest_path)
        world.run(INSTALL, cwd=project, env=env, scenario_id="l8b-seed")
        lock = load_yaml(lock_path(project))
        bundle_entry = next(e for e in lock["dependencies"] if e["repo_url"] == bundle)
        assert bundle_entry["skill_subset"] == ["alpha", "beta"]
        world.run(("install", "--no-policy", b), cwd=project, env=env, scenario_id="l8b-add-b")
        # Rewrite the lock back to the first-wins narrowing a 0.36.0 run records.
        frozen_lock = load_yaml(lock_path(project))
        for entry in frozen_lock["dependencies"]:
            if entry["repo_url"] == bundle:
                entry["skill_subset"] = ["alpha", "beta"]
        dump_yaml(frozen_lock, lock_path(project))
        narrowed = lock_path(project).read_bytes()
        result = world.run(
            (*INSTALL, "--frozen"), cwd=project, env=env, expect=None, scenario_id="l8b-frozen"
        )
        output = (result.stdout + result.stderr).lower()
        assert result.returncode != 0, output[-3000:]
        assert "apm install" in output and bundle in output, output[-3000:]
        assert lock_path(project).read_bytes() == narrowed
        world.run(INSTALL, cwd=project, env=env, scenario_id="l8b-converge")
        assert locked(project, bundle)["skill_subset"] == sorted(_SKILLS)


def test_unknown_skill_named_by_a_transitive_parent_fails_and_preserves_prior_state(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L9 (#3204): pkg-b's selection matches no skill; it fails, names pkg-b, keeps bytes.

    #3204 refuses a selection that matches nothing. The union with pkg-a's
    valid selection must not hide that pkg-b's own selection matches nothing.
    """
    world = _world(tmp_path, apm_binary_path)
    _bundle, a, b = _diamond(world, {"skills": ["alpha"]}, {"skills": ["gamma-typo"]})
    project = world.project("consumer", dependencies=[a])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l9-seed")
        before_lock = lock_path(project).read_bytes()
        before_skill = (project / _PROJECT_SKILLS / "alpha" / "SKILL.md").read_bytes()
        manifest_path = project / "apm.yml"
        manifest = load_yaml(manifest_path)
        manifest["dependencies"]["apm"] = [a, b]
        dump_yaml(manifest, manifest_path)
        result = world.run(INSTALL, cwd=project, env=env, expect=None, scenario_id="l9-typo")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output[-3000:]
    assert "gamma-typo" in output and b in output, output[-3000:]
    assert lock_path(project).read_bytes() == before_lock
    assert (project / _PROJECT_SKILLS / "alpha" / "SKILL.md").read_bytes() == before_skill
    assert skill_dirs(project / _PROJECT_SKILLS) == ["alpha"]


def test_update_recomputes_the_union_after_a_parent_changes_its_selection(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L11: pkg-b moves from [gamma] to [delta] upstream; apm update follows."""
    world = _world(tmp_path, apm_binary_path)
    bundle = world.publish("bundle", skills=_SKILLS).slug
    a = world.publish("pkg-a", dependencies=[_edge(bundle, skills=["alpha"])]).slug
    pub_b = world.publish("pkg-b", dependencies=[_edge(bundle, skills=["gamma"])])
    project = world.project("consumer", dependencies=[a, pub_b.slug])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l11-install")
        assert skill_dirs(project / _PROJECT_SKILLS) == ["alpha", "gamma"]
        world.republish(pub_b, dependencies=[_edge(bundle, skills=["delta"])])
        world.run(
            ("update", "--yes", "--parallel-downloads", "0"),
            cwd=project,
            env=env,
            scenario_id="l11-update",
        )
        assert skill_dirs(project / _PROJECT_SKILLS) == ["alpha", "delta"]
        assert locked(project, bundle)["skill_subset"] == ["alpha", "delta"]
        _assert_audit_green(world, project, env, "l11-audit")


@pytest.mark.parametrize(
    "tamper",
    ["undeclared-name-added", "declared-name-dropped"],
)
def test_audit_ci_rejects_a_transitive_skill_subset_that_disagrees_with_declarations(
    tmp_path: Path, apm_binary_path: Path, tamper: str
) -> None:
    """L12 / D5: audit recomputes the expected selection for transitive deps too."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"skills": ["alpha"]}, {"skills": ["gamma"]})
    project = world.project("consumer", dependencies=[a, b])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="l12-install")
        lock = load_yaml(lock_path(project))
        entry = next(e for e in lock["dependencies"] if e["repo_url"] == bundle)
        if tamper == "undeclared-name-added":
            entry["skill_subset"] = sorted({*(entry.get("skill_subset") or ["alpha"]), "beta"})
        else:
            entry["skill_subset"] = ["alpha"]
        dump_yaml(lock, lock_path(project))
        result = world.run(AUDIT_CI, cwd=project, env=env, expect=None, scenario_id="l12-audit")
    failures = audit_failures(result)
    assert result.returncode != 0, result.stdout[-3000:]
    assert "skill-subset-consistency" in failures, failures


# -- user scope (-g) -----------------------------------------------------------

_GLOBAL_INSTALL = ("install", "-g", "--no-policy", "--parallel-downloads", "0")


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


def test_user_scope_diamond_merges_then_shrinks_on_uninstall(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L10: L1 + L6 at user scope (-g)."""
    world = _world(tmp_path, apm_binary_path)
    bundle, a, b = _diamond(world, {"skills": ["alpha", "beta"]}, {"skills": ["gamma", "delta"]})
    _user_manifest(world, [a, b])
    apm_home = world.isolated.config_root
    skills_root = world.isolated.home / _PROJECT_SKILLS
    cwd = world.isolated.work_root
    with world.serve() as env:
        world.run(_GLOBAL_INSTALL, cwd=cwd, env=env, scenario_id="l10-install")
        assert skill_dirs(skills_root) == sorted(_SKILLS)
        assert locked(apm_home, bundle)["skill_subset"] == sorted(_SKILLS)
        _assert_audit_green(world, apm_home, env, "l10-audit")
        world.run(("uninstall", "-g", a), cwd=cwd, env=env, scenario_id="l10-uninstall-a")
        assert skill_dirs(skills_root) == ["delta", "gamma"]
        assert locked(apm_home, bundle)["skill_subset"] == ["delta", "gamma"]
        _assert_audit_green(world, apm_home, env, "l10-audit-after-a")
        world.run(("uninstall", "-g", b), cwd=cwd, env=env, scenario_id="l10-uninstall-b")
        assert skill_dirs(skills_root) == []
        assert locked(apm_home, bundle) is None
