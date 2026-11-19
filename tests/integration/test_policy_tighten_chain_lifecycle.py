"""Tightening an org dependency deny list stays atomic and removable (#3649).

Promise under test: once an org tightens ``dependencies.deny`` (enforcement
``block``), every gate agrees on the verdict, every denial is atomic (no
manifest, lockfile, or deployed-file writes), and the user can still remove
the newly denied package with ``uninstall`` or ``prune``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    deployed_skills,
    file_digest,
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
_DENY_B = {
    **_ALLOW_ALL,
    "dependencies": {"deny": ["contoso/pkg-b"]},
}


def _snapshot(project: Path) -> dict[str, str]:
    return tree_snapshot(project, exclude=(".git",))


def test_tightened_deny_policy_is_atomic_and_removable(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    pkg_b = world.publish_package("pkg-b")
    world.publish_policy(_ALLOW_ALL)
    project = world.consumer(dependencies=[f"{pkg_a.slug}#v1.0.0", f"{pkg_b.slug}#v1.0.0"])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())

        # 1. allowed install
        run_step(runner, ("install",), cwd=project, env=env, step="1-install-allowed")
        assert lock_repo_urls(project) == {pkg_a.slug, pkg_b.slug}
        assert deployed_skills(project) == {"pkg-a-skill", "pkg-b-skill"}
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="1b-audit-allowed")

        # 2. org tightens policy; cache expires
        world.publish_policy(_DENY_B)
        world.clear_policy_cache()
        before = _snapshot(project)

        # 3. plain install is blocked atomically
        r = run_step(runner, ("install",), cwd=project, env=env, step="3-install", expected=1)
        assert "pkg-b" in output(r)
        assert _snapshot(project) == before, "blocked install wrote files"

        # 4. frozen install is blocked atomically
        r = run_step(
            runner, ("install", "--frozen"), cwd=project, env=env, step="4-frozen", expected=1
        )
        assert "pkg-b" in output(r)
        assert _snapshot(project) == before, "blocked frozen install wrote files"

        # 5. audit --ci agrees with install
        r = run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="5-audit", expected=1)
        assert "pkg-b" in output(r)
        assert _snapshot(project) == before

        # 6. update is blocked atomically
        r = run_step(
            runner, ("update", "--yes"), cwd=project, env=env, step="6-update", expected=(0, 1)
        )
        assert _snapshot(project) == before, "update under deny wrote files"
        update_rc = r.returncode

        # 7. compile does not resurrect or delete anything
        run_step(runner, ("compile",), cwd=project, env=env, step="7-compile", expected=(0, 1))

        # 8. uninstall of the newly denied package must still work
        run_step(
            runner,
            ("uninstall", pkg_b.slug),
            cwd=project,
            env=env,
            step="8-uninstall-denied",
        )
        assert lock_repo_urls(project) == {pkg_a.slug}
        assert deployed_skills(project) == {"pkg-a-skill"}
        manifest = load_yaml(project / "apm.yml")
        assert manifest["dependencies"]["apm"] == [f"{pkg_a.slug}#v1.0.0"]

        # 9. gates agree again; install idempotent
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="9-audit-clean")
        lock_before = file_digest(project / "apm.lock.yaml")
        run_step(runner, ("install",), cwd=project, env=env, step="9b-install-clean")
        assert file_digest(project / "apm.lock.yaml") == lock_before

        # 10. hand-edit apm.yml to re-add the denied dep: install writes nothing
        manifest["dependencies"]["apm"].append(f"{pkg_b.slug}#v1.0.0")
        dump_yaml(manifest, project / "apm.yml")
        before = _snapshot(project)
        r = run_step(runner, ("install",), cwd=project, env=env, step="10-handedit", expected=1)
        assert "pkg-b" in output(r)
        assert _snapshot(project) == before, "hand-edited denied install wrote files"
        assert not (project / "apm_modules" / "contoso" / "pkg-b").exists()

    print(f"update_rc_under_deny={update_rc}")


def test_prune_removes_newly_denied_orphan(tmp_path: Path, apm_binary_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    pkg_b = world.publish_package("pkg-b")
    world.publish_policy(_ALLOW_ALL)
    project = world.consumer(dependencies=[f"{pkg_a.slug}#v1.0.0", f"{pkg_b.slug}#v1.0.0"])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")
        world.publish_policy(_DENY_B)
        world.clear_policy_cache()

        manifest = load_yaml(project / "apm.yml")
        manifest["dependencies"]["apm"] = [f"{pkg_a.slug}#v1.0.0"]
        dump_yaml(manifest, project / "apm.yml")

        run_step(runner, ("prune",), cwd=project, env=env, step="2-prune")
        assert not (project / "apm_modules" / "contoso" / "pkg-b").exists()
        assert lock_repo_urls(project) == {pkg_a.slug}
        assert deployed_skills(project) == {"pkg-a-skill"}

        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="3-audit")
        lock_before = file_digest(project / "apm.lock.yaml")
        run_step(runner, ("install",), cwd=project, env=env, step="4-install")
        assert file_digest(project / "apm.lock.yaml") == lock_before


def test_update_with_available_upgrade_is_blocked_atomically(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A real upgrade of a newly denied package must not be written."""
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    pkg_b = world.publish_package("pkg-b")
    world.publish_policy(_ALLOW_ALL)
    project = world.consumer(dependencies=[f"{pkg_a.slug}#^1.0.0", f"{pkg_b.slug}#^1.0.0"])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)
    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")

        for pkg in (pkg_a, pkg_b):
            skill_package_tree(pkg.repository.worktree, pkg.name, revision="v1.1.0")
            commit = world.repositories.commit(pkg.repository, message="1.1.0")
            world.repositories.tag(pkg.repository, "v1.1.0", commit)
        world.publish_policy(_DENY_B)
        world.clear_policy_cache()
        before = _snapshot(project)

        r = run_step(
            runner, ("update", "--yes"), cwd=project, env=env, step="2-update", expected=(0, 1)
        )
        assert _snapshot(project) == before, f"update under deny wrote files:\n{output(r)}"
        assert r.returncode == 1, output(r)
        assert "pkg-b" in output(r)

        r = run_step(
            runner,
            ("install", "--update"),
            cwd=project,
            env=env,
            step="3-install-update",
            expected=1,
        )
        assert _snapshot(project) == before, "install --update under deny wrote files"
