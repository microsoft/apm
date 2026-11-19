"""Git semver ranges never jump to a pre-release (#3601, #3649).

Chain: install ``^1.2.3`` while only ``v1.2.3`` and ``v1.3.0-beta.1`` exist,
then ``outdated`` / ``update`` must stay on the stable line; an exact
pre-release pin still installs the pre-release. Publishing ``v1.3.0`` moves
both ``outdated`` and ``update`` to it and the lockfile records the tag.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    file_digest,
    output,
    run_step,
    skill_package_tree,
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


def _lock_entry(project: Path, slug: str) -> dict:
    lock = load_yaml(project / "apm.lock.yaml") or {}
    for entry in lock.get("dependencies") or []:
        if entry.get("repo_url") == slug:
            return entry
    raise AssertionError(f"{slug} not in lockfile: {lock}")


def _deployed_revision(project: Path, name: str) -> str:
    path = project / ".agents" / "skills" / f"{name}-skill" / "SKILL.md"
    return path.read_text(encoding="utf-8").strip().splitlines()[-1]


def test_stable_range_never_selects_prerelease_across_outdated_and_update(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg = world.publish_package("pkg-s", tag="v1.2.3")
    repo = pkg.repository
    skill_package_tree(repo.worktree, "pkg-s", revision="v1.3.0-beta.1")
    beta = world.repositories.commit(repo, message="beta")
    world.repositories.tag(repo, "v1.3.0-beta.1", beta)

    project = world.consumer("ranged", dependencies=[f"{pkg.slug}#^1.2.3"])
    pinned = world.consumer("pinned", dependencies=[f"{pkg.slug}#v1.3.0-beta.1"])
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())

        run_step(runner, ("install", "--no-policy"), cwd=project, env=env, step="1-install")
        entry = _lock_entry(project, pkg.slug)
        assert entry.get("resolved_ref") == "v1.2.3", entry
        assert entry.get("resolved_commit") == pkg.commit.sha, entry
        assert _deployed_revision(project, "pkg-s") == "# pkg-s v1"

        r = run_step(
            runner, ("outdated",), cwd=project, env=env, step="2-outdated", expected=(0, 1)
        )
        assert "beta" not in output(r), output(r)

        lock_before = file_digest(project / "apm.lock.yaml")
        run_step(runner, ("update", "--yes"), cwd=project, env=env, step="3-update")
        assert _lock_entry(project, pkg.slug).get("resolved_ref") == "v1.2.3"
        assert file_digest(project / "apm.lock.yaml") == lock_before, "no-op update churned lock"

        run_step(runner, ("install", "--no-policy"), cwd=pinned, env=env, step="4-pin-beta")
        assert _lock_entry(pinned, pkg.slug).get("resolved_ref") == "v1.3.0-beta.1"
        assert _deployed_revision(pinned, "pkg-s") == "# pkg-s v1.3.0-beta.1"

        skill_package_tree(repo.worktree, "pkg-s", revision="v1.3.0")
        stable = world.repositories.commit(repo, message="stable")
        world.repositories.tag(repo, "v1.3.0", stable)

        r = run_step(
            runner, ("outdated",), cwd=project, env=env, step="5-outdated", expected=(0, 1)
        )
        assert "1.3.0" in output(r), output(r)
        assert "beta" not in output(r), output(r)

        run_step(runner, ("update", "--yes"), cwd=project, env=env, step="6-update")
        entry = _lock_entry(project, pkg.slug)
        assert entry.get("resolved_ref") == "v1.3.0", entry
        assert entry.get("resolved_commit") == stable.sha, entry
        assert _deployed_revision(project, "pkg-s") == "# pkg-s v1.3.0"
        manifest = load_yaml(project / "apm.yml")
        assert manifest["dependencies"]["apm"] == [f"{pkg.slug}#^1.2.3"]

        lock_before = file_digest(project / "apm.lock.yaml")
        run_step(
            runner, ("install", "--frozen", "--no-policy"), cwd=project, env=env, step="7-frozen"
        )
        assert file_digest(project / "apm.lock.yaml") == lock_before
