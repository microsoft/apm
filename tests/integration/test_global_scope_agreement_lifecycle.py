"""User-scope (-g) commands agree with the user lockfile across a chain (#3649).

With an isolated HOME / APM_HOME, a git-sourced parent with one transitive
child is installed globally. ``deps list -g``, ``deps tree -g``,
``deps why -g``, ``outdated -g``, ``lock -g`` and ``update -g`` must agree
with the user lockfile; ``uninstall -g`` of the parent removes the
transitive and every deployed file it owned, and a repeat run is a no-op.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    file_digest,
    lock_repo_urls,
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


def _skills(root: Path) -> set[str]:
    return {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}


def test_global_scope_commands_agree_across_chain(tmp_path: Path, apm_binary_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    child = world.publish_package("pkg-child")
    parent = world.publish_package("pkg-parent")
    repo = parent.repository
    skill_package_tree(repo.worktree, "pkg-parent")
    manifest = repo.worktree / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"dependencies:\n  apm:\n    - {child.slug}#v1.0.0\n",
        encoding="utf-8",
    )
    world.repositories.advance_tag(
        repo, "v1.0.0", world.repositories.commit(repo, message="add child")
    )
    home = world.isolated.home
    config_root = world.isolated.config_root
    dump_yaml({"name": "user", "version": "0.1.0"}, config_root / "apm.yml")
    lock_path = config_root / "apm.lock.yaml"
    claude = home / ".claude"
    cwd = world.isolated.work_root
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        env.pop("CLAUDE_CONFIG_DIR", None)

        def g(*args: str, step: str, expected: int | tuple[int, ...] = 0):
            return run_step(runner, args, cwd=cwd, env=env, step=step, expected=expected)

        g(
            "install",
            "-g",
            f"{parent.slug}#v1.0.0",
            "--target",
            "claude",
            "--no-policy",
            step="1-install",
        )
        assert lock_repo_urls(config_root) == {parent.slug, child.slug}
        assert _skills(claude) == {"pkg-parent-skill", "pkg-child-skill"}
        assert load_yaml(config_root / "apm.yml")["dependencies"]["apm"] == [
            f"{parent.slug}#v1.0.0"
        ]
        lock = file_digest(lock_path)

        r = g("deps", "list", "-g", step="2-list")
        assert "pkg-parent" in output(r) and "pkg-child" in output(r), output(r)
        assert "orphan" not in output(r).lower(), output(r)
        r = g("deps", "tree", "-g", step="3-tree")
        assert "pkg-parent" in output(r) and "pkg-child" in output(r), output(r)
        r = g("deps", "why", child.slug, "--global", step="4-why")
        assert "pkg-parent" in output(r), output(r)
        g("outdated", "-g", step="5-outdated", expected=(0, 1))
        g("lock", "-g", step="6-lock")
        assert file_digest(lock_path) == lock, "lock -g churned an up-to-date lock"
        g("cache", "clean", "--yes", step="7-cache-clean")
        g("update", "-g", "--yes", step="8-update")
        assert file_digest(lock_path) == lock, "no-op update -g churned the lock"
        g("install", "-g", "--target", "claude", "--no-policy", step="9-restore")
        assert file_digest(lock_path) == lock, "restore -g churned the lock"
        assert _skills(claude) == {"pkg-parent-skill", "pkg-child-skill"}

        g("uninstall", "-g", parent.slug, step="10-uninstall")
        assert lock_repo_urls(config_root) == set()
        assert _skills(claude) == set()
        assert load_yaml(config_root / "apm.yml")["dependencies"]["apm"] == []
        modules = config_root / "apm_modules"
        leftovers = [p for p in modules.rglob("apm.yml")] if modules.exists() else []
        assert leftovers == [], leftovers
        r = g("deps", "list", "-g", step="11-list-empty")
        assert "pkg-child" not in output(r), output(r)
        g("install", "-g", "--target", "claude", "--no-policy", step="12-restore-empty")
        assert _skills(claude) == set()
