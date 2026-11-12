"""A cached org policy and a denied transitive keep install atomic (#3649).

* A fresh policy cache keeps install and ``audit --ci`` in agreement until
  it expires.
* Once the cache expires, a denied *transitive* dependency blocks install
  atomically, including positional installs of its parent or of itself
  (``apm.yml`` restored byte-for-byte).
* Uninstalling the parent of a newly denied transitive still works.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    deployed_skills,
    lock_repo_urls,
    output,
    run_step,
    skill_package_tree,
    tree_snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_ALLOW_ALL = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}


def _deny(*slugs: str) -> dict[str, object]:
    return {**_ALLOW_ALL, "dependencies": {"deny": list(slugs)}}


def _publish_parent(world: PolicyScopeWorld, child_slug: str):
    """Publish pkg-parent@v1.0.0 that depends on the child package."""
    parent = world.publish_package("pkg-parent")
    repo = parent.repository
    skill_package_tree(repo.worktree, "pkg-parent")
    manifest = repo.worktree / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"dependencies:\n  apm:\n    - {child_slug}#v1.0.0\n",
        encoding="utf-8",
    )
    commit = world.repositories.commit(repo, message="add child")
    world.repositories.advance_tag(repo, "v1.0.0", commit)
    parent.commit = commit
    return parent


def _world(tmp_path: Path):
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    child = world.publish_package("pkg-child")
    parent = _publish_parent(world, child.slug)
    other = world.publish_package("pkg-other")
    world.publish_policy(_ALLOW_ALL)
    project = world.consumer(dependencies=[f"{parent.slug}#v1.0.0"])
    return world, child, parent, other, project


def test_transitive_denial_install_chain_is_atomic(tmp_path: Path, apm_binary_path: Path) -> None:
    world, child, parent, other, project = _world(tmp_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())

        run_step(runner, ("install",), cwd=project, env=env, step="1-install")
        assert lock_repo_urls(project) == {parent.slug, child.slug}
        assert deployed_skills(project) == {"pkg-parent-skill", "pkg-child-skill"}

        # Tighten upstream but keep the (fresh) cache: install and audit agree.
        world.publish_policy(_deny(child.slug))
        run_step(runner, ("install",), cwd=project, env=env, step="2-install-cached")
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="2b-audit-cached")

        # Cache expires: transitive denial blocks install atomically.
        world.clear_policy_cache()
        before = tree_snapshot(project)
        r = run_step(runner, ("install",), cwd=project, env=env, step="3-install", expected=1)
        assert "pkg-child" in output(r), output(r)
        assert tree_snapshot(project) == before, "blocked transitive install wrote files"

        # Positional re-install of the parent pulls the denied transitive:
        # blocked, and apm.yml is restored byte-for-byte.
        run_step(
            runner,
            ("install", f"{parent.slug}#v1.0.0"),
            cwd=project,
            env=env,
            step="4-install-positional-parent",
            expected=1,
        )
        assert tree_snapshot(project) == before, "blocked positional install wrote files"

        # Positional install of the denied package itself.
        run_step(
            runner,
            ("install", f"{child.slug}#v1.0.0"),
            cwd=project,
            env=env,
            step="5-install-denied-positional",
            expected=1,
        )
        assert tree_snapshot(project) == before, "denied positional install wrote files"

        # Uninstalling the parent removes the denied transitive too.
        run_step(runner, ("uninstall", parent.slug), cwd=project, env=env, step="6-uninstall")
        assert lock_repo_urls(project) == set()
        assert deployed_skills(project) == set()
        assert not (project / "apm_modules" / "contoso" / "pkg-child").exists()
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="7-audit-clean")
        run_step(runner, ("install",), cwd=project, env=env, step="8-install-clean")

        # An unrelated allowed package installs once the denied graph is gone.
        run_step(runner, ("install", f"{other.slug}#v1.0.0"), cwd=project, env=env, step="9-other")
        assert lock_repo_urls(project) == {other.slug}


@pytest.mark.lifecycle_smoke
def test_audit_ci_agrees_with_install_on_denied_transitive(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """audit --ci judges the locked transitive set install refuses (#3658)."""
    world, child, parent, _other, project = _world(tmp_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)
    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="2-audit-allowed")
        world.publish_policy(_deny(child.slug))
        world.clear_policy_cache()
        before = tree_snapshot(project)
        r = run_step(runner, ("install",), cwd=project, env=env, step="3-install", expected=1)
        assert "pkg-child" in output(r), output(r)
        r = run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="4-audit", expected=1)
        assert "pkg-child" in output(r), output(r)
        assert "dependency-denylist" in output(r), output(r)
        assert tree_snapshot(project) == before

        # Removing the parent clears the transitive verdict for both gates.
        run_step(runner, ("uninstall", parent.slug), cwd=project, env=env, step="5-uninstall")
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="6-audit-clean")


@pytest.mark.lifecycle_smoke
def test_audit_no_cache_fetches_fresh_auto_discovered_policy(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """--no-cache bypasses a fresh cache for auto-discovered policy (#3659)."""
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg = world.publish_package("pkg-b")
    world.publish_policy(_ALLOW_ALL)
    project = world.consumer(dependencies=[f"{pkg.slug}#v1.0.0"])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)
    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")
        world.publish_policy(_deny(pkg.slug))
        # The cache is still fresh: a cached read keeps the old verdict.
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="2-audit-cached")
        r = run_step(
            runner,
            ("audit", "--ci", "--no-cache"),
            cwd=project,
            env=env,
            step="3-audit-no-cache",
            expected=1,
        )
        assert "pkg-b" in output(r), output(r)
