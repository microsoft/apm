"""User-scope (-g) config and policy inputs come only from the user manifest (#4087).

PROMISE: outcome.status_matches_state -- -g exit status reflects the user-scope
config, and -g is not governed by a git remote (source:
docs/src/content/docs/enterprise/apm-policy.mdx).
CELL: OP=install|update SCOPE=user GATE=policy NET=local-fixture

Hermetic chains against the installed CLI, run from a directory outside
HOME with ``APM_HOME`` pointing elsewhere. At user scope the deploy root
(HOME) is not the manifest directory (``$APM_HOME``), so:

* a ``policy:`` block in ``$HOME/apm.yml`` is not the user-scope config; the
  ``policy.fetch_failure_default`` knob is read from ``$APM_HOME/apm.yml``;
* user scope has no git remote: org policy is not discovered (no policy
  request leaves the CLI) from a git checkout at HOME, for ``install -g`` and
  ``update -g`` alike, and ``apm policy status -g`` states the source "none".
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._policy_scope_kit import (
    ORG,
    POLICY_CACHE_DIR,
    POLICY_REPO,
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
    pytest.mark.promise("P-LAW-outcome.status_matches_state", "P-OWN-apm-home-resolution"),
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_DENY_PKG = {
    "name": "contoso-policy",
    "version": "1.0.0",
    "enforcement": "block",
    "dependencies": {"deny": [f"{ORG}/pkg-user"]},
}


def _user_world(tmp_path: Path, apm_binary_path: Path):
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg = world.publish_package("pkg-user")
    home = world.isolated.home
    apm_home = tmp_path / "elsewhere" / "apm-home"
    apm_home.mkdir(parents=True)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)
    return world, pkg, home, apm_home, runner


def _manifest(root: Path, **extra: object) -> None:
    dump_yaml(
        {"name": "user", "version": "0.1.0", "targets": ["claude"], **extra}, root / "apm.yml"
    )


def _step(runner, args, *, cwd, env, step, expected=0):
    """Run one step and apply the I6 (ASCII) and I9 (no duplicate status lines) oracles."""
    result = run_step(runner, args, cwd=cwd, env=env, step=step, expected=expected)
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), output(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), output(result)
    return result


def _skill_deployed(home: Path) -> bool:
    return (home / ".claude" / "skills" / "pkg-user-skill" / "SKILL.md").is_file()


def test_global_fetch_failure_default_comes_from_user_manifest(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, pkg, home, apm_home, runner = _user_world(tmp_path, apm_binary_path)
    # $HOME/apm.yml is not the user-scope manifest when APM_HOME is set.
    _manifest(home, policy={"fetch_failure_default": "block"})
    _manifest(apm_home)
    cwd = world.isolated.work_root

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = {**fx.child_environment(world.git_env()), "APM_HOME": str(apm_home)}
        env.pop("CLAUDE_CONFIG_DIR", None)
        result = _step(
            runner,
            ("install", "-g", f"{pkg.slug}#v1.0.0"),
            cwd=cwd,
            env=env,
            step="home-decoy-ignored",
        )
        assert _skill_deployed(home), output(result)
        assert lock_repo_urls(apm_home) == {pkg.slug}

        # Control: the same knob in the user manifest is honoured.
        _manifest(
            apm_home,
            policy={"fetch_failure_default": "block"},
            dependencies={"apm": [f"{pkg.slug}#v1.0.0"]},
        )
        before = (tree_snapshot(apm_home), tree_snapshot(home / ".claude"))
        result = _step(
            runner,
            ("install", "-g"),
            cwd=cwd,
            env=env,
            step="user-manifest-block",
            expected=1,
        )
        assert "fetch_failure_default=block" in output(result), output(result)
        # I7: the blocked command preserves user-scope state.
        assert (tree_snapshot(apm_home), tree_snapshot(home / ".claude")) == before


def _policy_requests(fx) -> list[str]:
    return [
        f"{o.host} {o.method} {o.path}"
        for o in fx.observations
        if o.method != "CONNECT" and (f"/{POLICY_REPO}" in o.path or "apm-policy" in o.path)
    ]


def test_global_commands_do_not_discover_policy_from_home_git_remote(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, pkg, home, apm_home, runner = _user_world(tmp_path, apm_binary_path)
    world.publish_policy(_DENY_PKG)
    env0 = world.isolated.subprocess_env()
    # A dotfiles checkout at HOME whose remote names the policy org.
    subprocess.run(["git", "init", "-q"], cwd=home, env=env0, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://github.com/{ORG}/dotfiles.git"],
        cwd=home,
        env=env0,
        check=True,
    )
    _manifest(apm_home)
    cwd = world.isolated.work_root
    policy_cache = world.isolated.cache_root / POLICY_CACHE_DIR
    git_before = tree_snapshot(home / ".git", exclude=())

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = {**fx.child_environment(world.git_env()), "APM_HOME": str(apm_home)}
        env.pop("CLAUDE_CONFIG_DIR", None)
        result = _step(
            runner,
            ("install", "-g", f"{pkg.slug}#v1.0.0"),
            cwd=cwd,
            env=env,
            step="install-g",
        )
        assert _skill_deployed(home), output(result)
        assert "Policy violation" not in output(result), output(result)
        result = _step(runner, ("update", "-g", "--yes"), cwd=cwd, env=env, step="update-g")
        assert "Policy violation" not in output(result), output(result)
        assert _skill_deployed(home), output(result)

        # The user-scope policy source is stated, including "none".
        result = _step(
            runner, ("policy", "status", "-g", "--json"), cwd=cwd, env=env, step="status-g"
        )
        report = json.loads(result.stdout)
        assert (report["scope"], report["outcome"], report["source"]) == (
            "user",
            "no_git_remote",
            "none",
        ), report

        assert _policy_requests(fx) == [], _policy_requests(fx)
        assert not policy_cache.exists(), sorted(policy_cache.rglob("*"))
        assert tree_snapshot(home / ".git", exclude=()) == git_before

        # Control: the same checkout at project scope does discover that policy.
        result = _step(
            runner, ("policy", "status", "--json"), cwd=home, env=env, step="status-project"
        )
        assert json.loads(result.stdout)["outcome"] == "found", result.stdout
        assert _policy_requests(fx), "the fixture never served the org policy"
