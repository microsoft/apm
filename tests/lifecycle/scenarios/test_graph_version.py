"""Graph and version scenarios: depth, diamonds, constraints, tag objects, dry-run (#4105).

PROMISE: P-OWN-post-uninstall-dependency-reachability -- a transitive
dependency stays while any declared package still reaches it and goes with
the last one (source: .apm/architecture/owners, deps/reachability.py).
PROMISE: P-OWN-ls-remote-tag-commits -- an annotated tag resolves to its
peeled commit, exactly like a lightweight tag (source: deps/git_remote_ops.py).
PROMISE: P-OWN-git-semver-preflight-resolution -- a semver constraint resolves
to the highest matching tag; an unsatisfiable one fails and changes nothing.
PROMISE: P-OWN-prospective-dry-run-install-plan -- ``install --dry-run`` lists
indirect dependencies and writes nothing.

Every row runs through ``driver.run`` with org policy on (variant ``allow``),
applies I1-I9 after every step and pins the exact changed set of the steps
that mutate. The world serves ``lifecycle/p-skill`` at ``v1.0.0``; deeper
graphs and tag shapes are authored inside each test.
"""

from __future__ import annotations

import pytest

from tests.lifecycle.scenarios._kit import (
    Known,
    Scenario,
    assert_changes,
    known_defect,
    serve_repository,
    skill_package,
)
from tests.lifecycle.world import use_policy
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I3",
        "P-INV-I5",
        "P-INV-I7",
        "P-OWN-post-uninstall-dependency-reachability",
        "P-OWN-ls-remote-tag-commits",
        "P-OWN-git-semver-preflight-resolution",
        "P-OWN-prospective-dry-run-install-plan",
    ),
]

CHAIN = ("g-a", "g-b", "g-c", "g-d")
UNINSTALL_NOISE = Known("#4160", r"duplicate 'integrated' line")
DIAMOND = {"p-parent": ("p-child",), "p-parent2": ("p-child",)}
# What a first project install writes besides the deployed skills.
INSTALL_WRITES = (
    "project:apm.yml",
    "project:apm.lock.yaml",
    "project:.gitignore",
    "project:.apm-local*",
    "project:apm_modules*",
    # The copilot target root, created even when nothing deploys into it.
    "project:.github",
)


def _skills(*names: str) -> tuple[str, ...]:
    return tuple(f"project:.agents/skills/{name}*" for name in names)


def _chain(world) -> dict[str, str]:
    """g-a -> g-b -> g-c -> g-d, each a skill package beside the others."""
    factory = LocalPackageFactory(world.root / "graph")
    specs: dict[str, str] = {}
    for name, child in zip(CHAIN, (*CHAIN[1:], None), strict=True):
        deps = (f"../{child}",) if child else ()
        specs[name] = str(skill_package(factory, name, dependencies=deps))
    return specs


def _scenario(world, packages, **kwargs) -> Scenario:
    use_policy(world, "allow")
    return Scenario(world, packages, policy="allow", **kwargs)


def test_transitive_chain_deploys_every_depth(lifecycle_world) -> None:
    specs = _chain(lifecycle_world)
    requires = {name: (child,) for name, child in zip(CHAIN, CHAIN[1:], strict=False)}
    kinds = dict.fromkeys(CHAIN, ("skill",))
    scenario = _scenario(lifecycle_world, specs, kinds=kinds, requires=requires)

    step = scenario.run("install g-a")
    assert_changes(step.changed, (*INSTALL_WRITES, *_skills(*CHAIN)), label="install g-a")
    scenario.assert_deployed()
    assert len(scenario.lock_names()) == len(CHAIN)

    scenario.pair()
    rerun = scenario.run("install")
    assert_changes(rerun.changed, (), label="converged install")
    scenario.finish()


def test_diamond_keeps_shared_child_until_last_parent(lifecycle_world) -> None:
    scenario = _scenario(lifecycle_world, ("p-parent", "p-parent2", "p-child"), requires=DIAMOND)
    step = scenario.run("install p-parent p-parent2")
    assert_changes(
        step.changed,
        (*INSTALL_WRITES, *_skills("p-parent", "p-parent2", "p-child")),
        label="install diamond",
    )
    scenario.assert_deployed()
    assert len(scenario.lock_names()) == 3

    # Re-integrating the survivors prints their lines without package headers.
    first = scenario.run("uninstall p-parent", known={"I9": UNINSTALL_NOISE})
    assert_changes(
        first.changed,
        ("project:apm.yml", "project:apm.lock.yaml", "project:apm_modules*", *_skills("p-parent")),
        label="uninstall first parent",
    )
    scenario.assert_deployed()

    last = scenario.run("uninstall p-parent2")
    assert_changes(
        last.changed,
        (
            "project:apm.yml",
            "project:apm.lock.yaml",
            "project:apm_modules*",
            *_skills("p-parent2", "p-child"),
        ),
        label="uninstall last parent",
    )
    scenario.assert_deployed()
    scenario.finish()


def test_user_scope_diamond_deploys_the_shared_child(lifecycle_world) -> None:
    scenario = Scenario(
        lifecycle_world, ("p-parent", "p-parent2", "p-child"), scope="user", requires=DIAMOND
    )
    step = scenario.run("install p-parent p-parent2")
    assert "home:.agents/skills/p-parent/SKILL.md" in step.changed
    # install -g drops the transitive relative-path dependency silently.
    with known_defect("#4153", r"p-child/skill/copilot"):
        scenario.assert_deployed()
    scenario.finish()


@pytest.mark.parametrize(
    ("root_ref", "parent_ref", "exit_code"),
    [
        pytest.param("^1.0.0", "v1.0.0", 0, id="range-and-exact-agree"),
        pytest.param("v1.0.0", "^1.0.0", 0, id="exact-and-range-agree"),
        pytest.param("^9.0.0", None, 1, id="unsatisfiable-range"),
    ],
)
def test_constraint_compatibility(lifecycle_world, root_ref, parent_ref, exit_code) -> None:
    factory = LocalPackageFactory(lifecycle_world.root / "graph")
    specs = {"p-skill": f"lifecycle/p-skill#{root_ref}"}
    requires = {}
    if parent_ref is not None:
        parent = skill_package(factory, "g-pin", dependencies=(f"lifecycle/p-skill#{parent_ref}",))
        specs["g-pin"] = str(parent)
        requires["g-pin"] = ("p-skill",)
    kinds = {"p-skill": ("skill",), "g-pin": ("skill",)}
    scenario = _scenario(lifecycle_world, specs, kinds=kinds, requires=requires)

    step = scenario.run(f"install {' '.join(specs)}", exit=exit_code)
    if exit_code:
        # The failed install still creates an empty .github directory.
        with known_defect("#4159", r"unexpected: \['project:\.github'\]"):
            assert_changes(step.changed, (), label="refused constraint")
        assert "^9.0.0" in step.output, "I7: the refusal names the constraint"
    else:
        assert_changes(
            step.changed, (*INSTALL_WRITES, *_skills(*specs)), label="compatible constraints"
        )
        rows = scenario.lock().get_package_dependencies()
        skill_rows = [row for row in rows if row.repo_url == "lifecycle/p-skill"]
        assert len(skill_rows) == 1, rows
        assert skill_rows[0].resolved_ref == "v1.0.0"
        scenario.assert_deployed()
        scenario.pair()
    scenario.finish()


@pytest.fixture
def tagged_repository(lifecycle_world):
    """``lifecycle/g-tags``: v1.0.0 lightweight, v2.0.0 annotated, on distinct commits."""
    factory = LocalGitRepositoryFactory(lifecycle_world.root / "repos", env=lifecycle_world.env)
    packages = LocalPackageFactory(lifecycle_world.root / "tag-src")
    tree = skill_package(packages, "g-tags")
    repository = factory.create("g-tags", source_tree=tree)
    first = factory.commit(repository, message="g-tags v1")
    factory.tag(repository, "v1.0.0", first)
    (repository.worktree / "skills" / "g-tags" / "SKILL.md").write_text(
        "---\nname: g-tags\ndescription: Scenario skill g-tags v2.\n---\n\nUse g-tags v2.\n",
        encoding="ascii",
    )
    second = factory.commit(repository, message="g-tags v2")
    factory.tag(repository, "v2.0.0", second, annotated=True)
    serve_repository(lifecycle_world, "lifecycle/g-tags", repository.origin)
    return {"v1.0.0": first.sha, "v2.0.0": second.sha}


@pytest.mark.parametrize(
    ("ref", "tag"),
    [
        pytest.param("v1.0.0", "v1.0.0", id="lightweight"),
        pytest.param("v2.0.0", "v2.0.0", id="annotated"),
        pytest.param("^2.0.0", "v2.0.0", id="annotated-by-range"),
    ],
)
def test_tag_objects_resolve_to_their_commit(lifecycle_world, tagged_repository, ref, tag) -> None:
    specs = {"g-tags": f"lifecycle/g-tags#{ref}"}
    scenario = _scenario(lifecycle_world, specs, kinds={"g-tags": ("skill",)})
    step = scenario.run("install g-tags")
    assert_changes(step.changed, (*INSTALL_WRITES, *_skills("g-tags")), label=f"install {ref}")
    (row,) = scenario.lock().get_package_dependencies()
    assert row.resolved_commit == tagged_repository[tag], "the peeled commit is locked"
    assert row.resolved_ref == tag
    scenario.assert_deployed()
    scenario.pair()
    scenario.finish()


def test_dry_run_lists_indirect_dependencies(lifecycle_world) -> None:
    specs = _chain(lifecycle_world)
    scenario = _scenario(lifecycle_world, specs, kinds=dict.fromkeys(CHAIN, ("skill",)))
    step = scenario.run("install --dry-run g-a", read_only=True)
    assert_changes(step.changed, (), label="dry-run")
    for child in CHAIN[1:]:
        assert f"../{child} -> install" in step.output, f"I7: dry-run hides {child}"
    assert f"would install {len(CHAIN)} APM dependencies" in step.output
    scenario.assert_deployed()
    scenario.finish()
