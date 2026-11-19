"""Real-CLI #1326 dependency-confusion refusal on every marketplace install path (#3913).

An enterprise (``*.ghe.com``) marketplace, registered over SSH and served by a
local Git fixture, lists a plugin whose dict source has a bare cross-repo
``repo: acme/tools``. The bare form falls back to public ``github.com``, where
the test stages a repository under that exact name. Every path that turns the
entry into a dependency -- ``apm install NAME@MARKETPLACE``, an ``apm.yml``
entry (with or without a version range), a transitive declaration and ``apm
update`` -- must refuse it before any write or package fetch. A host-qualified
``repo: github.com/acme/tools`` entry still installs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_MARKETPLACE = "corp-mkt"
_CATALOG_REMOTE = "ssh://git@corp.ghe.com/corp/marketplace"
_BARE = "shared-tool"
_QUALIFIED = "public-tool"
_ATTACKER_REMOTE = "https://github.com/acme/tools"
_TARGET = "copilot"
_FLAGS = ("--target", _TARGET, "--parallel-downloads", "0")
_REFUSAL = "refused (dependency-confusion risk #1326)"


@dataclass
class _World:
    root: Path
    env: dict[str, str]
    runner: ApmLifecycleRunner
    repositories: LocalGitRepositoryFactory
    catalog: LocalGitRepository

    def publish(self, bare_repo: str) -> None:
        plugins = [
            {"name": _BARE, "source": {"type": "github", "repo": bare_repo}},
            {"name": _QUALIFIED, "source": {"type": "github", "repo": "github.com/acme/tools"}},
        ]
        (self.catalog.worktree / "marketplace.json").write_text(
            json.dumps({"name": _MARKETPLACE, "owner": {"name": "Corp"}, "plugins": plugins}),
            encoding="utf-8",
        )
        self.repositories.commit(self.catalog, message=f"publish {bare_repo}")

    def write_manifest(self, path: Path, dependencies: list[object]) -> None:
        path.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": path.name,
            "version": "1.0.0",
            "targets": [_TARGET],
            "dependencies": {"apm": dependencies},
        }
        dump_yaml(manifest, path / "apm.yml")

    def run(self, *args: str, cwd: Path | None = None) -> CommandResult:
        return self.runner.run(
            args,
            scenario_id="marketplace-dependency-confusion",
            cwd=cwd or self.root,
            env=self.env,
        )


def _with_ssh_rewrite(env: dict[str, str], repository: LocalGitRepository) -> dict[str, str]:
    """Route the SSH catalog remote to the local fixture (the harness rewrites HTTPS only)."""
    count = int(env["GIT_CONFIG_COUNT"])
    for offset, remote in enumerate((_CATALOG_REMOTE, f"{_CATALOG_REMOTE}.git")):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = f"url.{repository.file_url}/.insteadOf"
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = remote
    env["GIT_CONFIG_COUNT"] = str(count + 2)
    return env


def _world(tmp_path: Path, binary: Path, *, bare_repo: str = "acme/tools") -> _World:
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    package = repositories.create("acme-tools")
    skill = package.worktree / "skills" / "pwned" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: pwned\ndescription: staged\n---\n# pwned\n", encoding="utf-8")
    repositories.tag(package, "v1.0.0", repositories.commit(package, message="stage"))
    catalog = repositories.create("corp-marketplace")
    env = repositories.url_rewrite_subprocess_env_many(((package, _ATTACKER_REMOTE),))
    world = _World(
        root=isolated.work_root / "consumer",
        env=_with_ssh_rewrite(env, catalog),
        runner=ApmLifecycleRunner((str(binary),), timeout_seconds=120),
        repositories=repositories,
        catalog=catalog,
    )
    world.publish(bare_repo)
    world.root.mkdir(parents=True)
    added = world.run("marketplace", "add", f"{_CATALOG_REMOTE}.git", "--name", _MARKETPLACE)
    assert added.returncode == 0, added.stdout + added.stderr
    return world


def _arrange(world: _World, path: str) -> tuple[str, ...]:
    """Lay out the consumer for *path* and return the command that resolves the entry."""
    entry: dict[str, object] = {"name": _BARE, "marketplace": _MARKETPLACE}
    if path == "cli":
        world.write_manifest(world.root, [])
        return ("install", f"{_BARE}@{_MARKETPLACE}", *_FLAGS)
    if path == "transitive":
        world.write_manifest(world.root.parent / "middle", [entry])
        world.write_manifest(world.root, ["../middle"])
        return ("install", *_FLAGS)
    if path == "apm-yml-range":
        # The staged repo has no matching tag: a tag lookup before admission
        # would fail with a version error instead of the #1326 refusal.
        entry["version"] = "^1.0.0"
    world.write_manifest(world.root, [entry])
    if path == "update":
        return ("update", "--yes", *_FLAGS)
    return ("install", *_FLAGS)


@pytest.mark.parametrize("path", ["cli", "apm-yml", "apm-yml-range", "transitive", "update"])
def test_bare_cross_repo_marketplace_entry_is_refused_before_any_write(
    tmp_path: Path, apm_binary_path: Path, path: str
) -> None:
    """The #1326 refusal fires on every path; nothing is fetched, written or deployed."""
    world = _world(tmp_path, apm_binary_path)
    command = _arrange(world, path)
    manifest_before = (world.root / "apm.yml").read_bytes()

    result = world.run(*command)

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert result.returncode != 0, output
    assert _REFUSAL in output, output
    assert "'corp.ghe.com/acme/tools'" in output
    assert "'github.com/acme/tools'" in output
    assert (world.root / "apm.yml").read_bytes() == manifest_before
    assert not (world.root / "apm.lock.yaml").exists()
    assert not (world.root / "apm_modules" / "acme").exists()
    assert not list(world.root.rglob("pwned"))


def test_host_qualified_marketplace_entry_still_installs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The documented escape hatch is not refused on the resolver path."""
    world = _world(tmp_path, apm_binary_path)
    world.write_manifest(world.root, [{"name": _QUALIFIED, "marketplace": _MARKETPLACE}])

    result = world.run("install", *_FLAGS)

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert result.returncode == 0, output
    assert _REFUSAL not in output
    assert (world.root / "apm.lock.yaml").exists()
    assert list(world.root.rglob("pwned/SKILL.md"))


def test_apm_update_re_admits_an_entry_locked_before_its_catalog_turned_ambiguous(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A locked entry whose catalog row is now bare is refused on ``apm update``.

    Plain installs replay the lock without the catalog (#3911); ``apm update``
    re-resolves through admission, so it is the remediation for lock rows an
    older APM wrote.
    """
    world = _world(tmp_path, apm_binary_path, bare_repo="github.com/acme/tools")
    world.write_manifest(world.root, [{"name": _BARE, "marketplace": _MARKETPLACE}])
    installed = world.run("install", *_FLAGS)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    lock_before = (world.root / "apm.lock.yaml").read_bytes()
    world.publish("acme/tools")

    result = world.run("update", "--yes", *_FLAGS)

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert result.returncode != 0, output
    assert _REFUSAL in output, output
    assert (world.root / "apm.lock.yaml").read_bytes() == lock_before
