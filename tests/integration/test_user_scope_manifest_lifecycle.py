"""User-scope (-g) config and policy inputs come only from the user manifest (#4087).

PROMISE: outcome.status_matches_state -- -g exit status reflects the user-scope
config, and -g is not governed by a git remote (source:
docs/src/content/docs/enterprise/apm-policy.mdx).
CELL: OP=install|update|install-frozen|policy-status SCOPE=user GATE=policy NET=local-fixture

Hermetic chains against the installed CLI, run from a directory outside
HOME with ``APM_HOME`` pointing elsewhere. At user scope the deploy root
(HOME) is not the manifest directory (``$APM_HOME``), so:

* a ``policy:`` block in ``$HOME/apm.yml`` is not the user-scope config; the
  ``policy.fetch_failure_default`` knob is read from ``$APM_HOME/apm.yml``;
* user scope has no git remote: an org policy reachable from a git checkout at
  HOME does not apply to ``install -g``, ``update -g`` or ``install -g --frozen``
  (one cell each), and ``apm policy status -g`` states the source "none".
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
    skill_package_tree,
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


def _home_checkout(world: PolicyScopeWorld, home: Path) -> None:
    """Make HOME a git checkout whose remote names the policy org."""
    env = world.isolated.subprocess_env()
    subprocess.run(["git", "init", "-q"], cwd=home, env=env, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://github.com/{ORG}/dotfiles.git"],
        cwd=home,
        env=env,
        check=True,
    )


_USER_SCOPE_COMMANDS = {
    "install": ("install", "-g", "{slug}#v1.0.0"),
    "update": ("update", "-g", "--yes"),
    "install-frozen": ("install", "-g", "--frozen"),
}


@pytest.mark.parametrize("command", sorted(_USER_SCOPE_COMMANDS))
def test_user_scope_command_is_not_governed_by_home_remote_policy(
    tmp_path: Path, apm_binary_path: Path, command: str
) -> None:
    """User scope has no git remote: an org policy reachable from HOME does not apply.

    HOME is a checkout whose remote names an org whose policy denies the
    package. The user-scope command succeeds, sends no policy request, writes
    no policy cache, leaves HOME's checkout untouched and prints nothing
    about policy.
    """
    world, pkg, home, apm_home, runner = _user_world(tmp_path, apm_binary_path)
    world.publish_policy(_DENY_PKG)
    _manifest(apm_home)
    cwd = world.isolated.work_root
    policy_cache = world.isolated.cache_root / POLICY_CACHE_DIR
    args = tuple(part.format(slug=pkg.slug) for part in _USER_SCOPE_COMMANDS[command])

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = {**fx.child_environment(world.git_env()), "APM_HOME": str(apm_home)}
        env.pop("CLAUDE_CONFIG_DIR", None)
        if command != "install":
            # Seed the user lockfile before HOME becomes a checkout. update
            # tracks the default branch, which then moves, so it re-resolves
            # and runs the full install pipeline instead of short-circuiting.
            seed = pkg.slug if command == "update" else f"{pkg.slug}#v1.0.0"
            _step(runner, ("install", "-g", seed), cwd=cwd, env=env, step="seed-install")
            if command == "update":
                skill_package_tree(pkg.repository.worktree, pkg.name, revision="v2")
                world.repositories.commit(pkg.repository, message="v2")
            world.clear_policy_cache()
            fx.clear()
        _home_checkout(world, home)
        git_before = tree_snapshot(home / ".git", exclude=())

        result = _step(runner, args, cwd=cwd, env=env, step=f"{command}-g")

        assert _skill_deployed(home), output(result)
        assert "policy" not in output(result).lower(), output(result)
        assert _policy_requests(fx) == [], _policy_requests(fx)
        assert not policy_cache.exists(), sorted(policy_cache.rglob("*"))
        assert tree_snapshot(home / ".git", exclude=()) == git_before


def test_user_scope_policy_status_reports_no_source(tmp_path: Path, apm_binary_path: Path) -> None:
    """`apm policy status -g` states the user-scope source as "none".

    Control: the same checkout at project scope resolves the org policy, so
    the fixture would serve it to any command that asked.
    """
    world, _pkg, home, apm_home, runner = _user_world(tmp_path, apm_binary_path)
    world.publish_policy(_DENY_PKG)
    _manifest(apm_home)
    _home_checkout(world, home)
    cwd = world.isolated.work_root

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = {**fx.child_environment(world.git_env()), "APM_HOME": str(apm_home)}
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

        result = _step(
            runner, ("policy", "status", "--json"), cwd=home, env=env, step="status-project"
        )
        assert json.loads(result.stdout)["outcome"] == "found", result.stdout
        assert _policy_requests(fx), "the fixture never served the org policy"
