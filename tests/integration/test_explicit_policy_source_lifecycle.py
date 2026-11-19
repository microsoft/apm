"""``apm install --policy`` enforces an explicit policy source (#1925).

Promises under test:

1. A plain-git project (no host auto-discovery can reach) that passes
   ``--policy ./org-policy.yml`` blocks a denied dependency at install time;
   the local file needs no network.
2. The explicit source only adds constraints: it cannot re-allow what the
   auto-discovered org policy denies.
3. An unreachable ``--policy`` URL follows ``policy.fetch_failure_default``:
   ``warn`` reports it and installs, ``block`` fails closed without writes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    lock_repo_urls,
    output,
    run_step,
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

_DENY_B = {
    "name": "vendored-policy",
    "version": "1.0.0",
    "enforcement": "block",
    "dependencies": {"deny": ["contoso/pkg-b"]},
}
_LOOSE = {
    "name": "loose-policy",
    "version": "1.0.0",
    "enforcement": "off",
    "dependencies": {"allow": ["contoso/*"]},
}
# Port 9 (discard) on loopback refuses the connection without any real network.
_UNREACHABLE_URL = "https://127.0.0.1:9/apm-policy.yml"


def _plain_git(world: PolicyScopeWorld, project: Path) -> None:
    """Point origin at a plain path remote that org-policy discovery cannot parse."""
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(project.parent / "plain-remote.git")],
        cwd=project,
        env=world.isolated.subprocess_env(),
        check=True,
    )


def _snapshot(project: Path) -> dict[str, str]:
    return tree_snapshot(project, exclude=(".git",))


def test_plain_git_explicit_policy_blocks_denied_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    pkg_b = world.publish_package("pkg-b")
    project = world.consumer(dependencies=[f"{pkg_a.slug}#v1.0.0", f"{pkg_b.slug}#v1.0.0"])
    _plain_git(world, project)
    dump_yaml(_DENY_B, project / "org-policy.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())

        # Without --policy nothing reaches this remote: the install succeeds.
        before = _snapshot(project)
        r = run_step(
            runner,
            ("install", "--policy", "./org-policy.yml"),
            cwd=project,
            env=env,
            step="1-install-explicit-deny",
            expected=1,
        )
        text = output(r)
        assert "pkg-b" in text
        assert "org-policy.yml" in text, "effective policy source not reported"
        assert _snapshot(project) == before, "blocked install wrote files"

        # The same project installs once the denied dependency is gone.
        manifest = load_yaml(project / "apm.yml")
        manifest["dependencies"]["apm"] = [f"{pkg_a.slug}#v1.0.0"]
        dump_yaml(manifest, project / "apm.yml")
        run_step(
            runner,
            ("install", "--policy", "./org-policy.yml"),
            cwd=project,
            env=env,
            step="2-install-explicit-allowed",
        )
        assert lock_repo_urls(project) == {pkg_a.slug}


def test_explicit_policy_cannot_reallow_discovered_deny(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    pkg_b = world.publish_package("pkg-b")
    world.publish_policy(_DENY_B)
    project = world.consumer(dependencies=[f"{pkg_a.slug}#v1.0.0", f"{pkg_b.slug}#v1.0.0"])
    dump_yaml(_LOOSE, project / "loose-policy.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        before = _snapshot(project)
        r = run_step(
            runner,
            ("install", "--policy", "./loose-policy.yml"),
            cwd=project,
            env=env,
            step="1-install-loosen-attempt",
            expected=1,
        )
        text = output(r)
        assert "pkg-b" in text
        assert "enforcement=block" in text, "explicit enforcement=off relaxed the org policy"
        assert _snapshot(project) == before, "blocked install wrote files"


def test_unreachable_explicit_policy_follows_fetch_failure_default(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    project = world.consumer(dependencies=[f"{pkg_a.slug}#v1.0.0"])
    _plain_git(world, project)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())

        # fetch_failure_default: block -> fail closed, nothing written.
        manifest = load_yaml(project / "apm.yml")
        manifest["policy"] = {"fetch_failure_default": "block"}
        dump_yaml(manifest, project / "apm.yml")
        before = _snapshot(project)
        r = run_step(
            runner,
            ("install", "--policy", _UNREACHABLE_URL),
            cwd=project,
            env=env,
            step="1-unreachable-block",
            expected=1,
        )
        assert "could not be loaded" in output(r)
        assert _snapshot(project) == before, "fail-closed install wrote files"

        # Default (warn) -> the unreachable source is reported, install proceeds.
        del manifest["policy"]
        dump_yaml(manifest, project / "apm.yml")
        r = run_step(
            runner,
            ("install", "--policy", _UNREACHABLE_URL),
            cwd=project,
            env=env,
            step="2-unreachable-warn",
        )
        assert "could not be loaded" in output(r)
        assert lock_repo_urls(project) == {pkg_a.slug}


def test_update_with_nothing_to_change_still_routes_unreachable_explicit_policy(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The no-change shortcut must not skip fetch_failure_default for --policy (#1925)."""
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    project = world.consumer(dependencies=[f"{pkg_a.slug}#v1.0.0"])
    _plain_git(world, project)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="1-install")
        lock = (project / "apm.lock.yaml").read_bytes()
        update = ("update", "--yes", "--policy", _UNREACHABLE_URL)

        # Default (warn): reported, nothing to change, exit 0.
        r = run_step(runner, update, cwd=project, env=env, step="2-update-warn")
        assert "could not be loaded" in output(r)
        assert "already at their latest" in output(r)

        # fetch_failure_default: block -> nonzero even with nothing to change.
        manifest = load_yaml(project / "apm.yml")
        manifest["policy"] = {"fetch_failure_default": "block"}
        dump_yaml(manifest, project / "apm.yml")
        r = run_step(runner, update, cwd=project, env=env, step="3-update-block", expected=1)
        assert "could not be loaded" in output(r)
        assert (project / "apm.lock.yaml").read_bytes() == lock
