"""``apm install --policy`` enforces an explicit policy source (#1925).

Promises under test:

1. A plain-git project (no host auto-discovery can reach) that passes
   ``--policy ./org-policy.yml`` blocks a denied dependency at install time;
   the local file needs no network.
2. The explicit source only adds constraints: it cannot re-allow what the
   auto-discovered org policy denies.
3. A named ``--policy`` source that cannot be loaded (an unreachable URL, a
   missing file) fails closed for ``install``, ``install --mcp`` and
   ``update`` with nothing to change, under ``policy.fetch_failure_default``
   ``warn`` and ``block`` alike: non-zero exit, no file written.
4. With a loadable explicit source, a discovered-policy failure still follows
   ``policy.fetch_failure_default``.
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
    run_step,
    tree_snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.promise("req-pl-001"),
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
        text = r.stdout + r.stderr
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
        text = r.stdout + r.stderr
        assert "pkg-b" in text
        assert "enforcement=block" in text, "explicit enforcement=off relaxed the org policy"
        assert _snapshot(project) == before, "blocked install wrote files"


_MISSING_FILE = "./missing-policy.yml"
_COMMANDS = {
    "install": lambda source: ("install", "--policy", source),
    "install-mcp": lambda source: (
        "install",
        "--mcp",
        "fetch",
        "--policy",
        source,
        "--",
        "npx",
        "-y",
        "@mcp/fetch",
    ),
    "update": lambda source: ("update", "--yes", "--policy", source),
}


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_unloadable_explicit_policy_fails_closed_whatever_the_default(
    tmp_path: Path, apm_binary_path: Path, command: str
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    project = world.consumer(dependencies=[f"{pkg_a.slug}#v1.0.0"])
    _plain_git(world, project)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="0-install")
        assert lock_repo_urls(project) == {pkg_a.slug}

        for source in (_UNREACHABLE_URL, _MISSING_FILE):
            for default in ("warn", "block"):
                manifest = load_yaml(project / "apm.yml")
                manifest["policy"] = {"fetch_failure_default": default}
                dump_yaml(manifest, project / "apm.yml")
                before = _snapshot(project)
                r = run_step(
                    runner,
                    _COMMANDS[command](source),
                    cwd=project,
                    env=env,
                    step=f"{command}-{default}-{'url' if '://' in source else 'file'}",
                    expected=1,
                )
                text = r.stdout + r.stderr
                assert "explicit policy source" in text.lower(), text
                assert "could not be loaded" in text, text
                assert _snapshot(project) == before, "a failed --policy wrote files"


@pytest.mark.parametrize("default", ["warn", "block"])
def test_discovered_failure_with_loadable_explicit_follows_the_default(
    tmp_path: Path, apm_binary_path: Path, default: str
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    world.publish_policy({"name": "broken", "version": "1.0.0", "enforcement": "sometimes"})
    project = world.consumer(
        dependencies=[f"{pkg_a.slug}#v1.0.0"],
        extra={"policy": {"fetch_failure_default": default}},
    )
    dump_yaml(_DENY_B, project / "org-policy.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        before = _snapshot(project)
        r = run_step(
            runner,
            ("install", "--policy", "./org-policy.yml"),
            cwd=project,
            env=env,
            step=f"1-install-{default}",
            expected=1 if default == "block" else 0,
        )
        text = r.stdout + r.stderr
        assert "discovered policy source" in text.lower(), text
        if default == "block":
            assert _snapshot(project) == before, "blocked install wrote files"
        else:
            assert lock_repo_urls(project) == {pkg_a.slug}
