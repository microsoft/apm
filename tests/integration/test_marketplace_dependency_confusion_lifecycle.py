"""Real-CLI #1326 dependency-confusion refusal on every marketplace install path (#3913).

An enterprise (``*.ghe.com``) marketplace lists a plugin whose dict source has
a bare cross-repo ``repo: acme/tools``. The bare form falls back to public
``github.com``, where the test stages an attacker repository under that exact
name. Every path that turns the marketplace entry into a dependency -- ``apm
install NAME@MARKETPLACE``, an ``apm.yml`` entry, a transitive declaration and
``apm update`` -- must refuse it before any write or package fetch.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.marketplace.client import _cache_key
from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_MARKETPLACE = "corp-mkt"
_PLUGIN = "shared-tool"
_BARE_REPO = "acme/tools"
_ATTACKER_REMOTE = f"https://github.com/{_BARE_REPO}"
_TARGET = "copilot"
_FLAGS = ("--target", _TARGET, "--parallel-downloads", "0")
_ENTRY = {"name": _PLUGIN, "marketplace": _MARKETPLACE}
_REFUSAL = "refused (dependency-confusion risk #1326)"


@dataclass
class _World:
    root: Path
    env: dict[str, str]
    runner: ApmLifecycleRunner

    def write_manifest(self, path: Path, dependencies: list[object]) -> None:
        path.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, object] = {"name": path.name, "version": "1.0.0"}
        manifest["dependencies"] = {"apm": dependencies}
        manifest["targets"] = [_TARGET]
        dump_yaml(manifest, path / "apm.yml")

    def run(self, *args: str) -> CommandResult:
        return self.runner.run(
            args, scenario_id="marketplace-dependency-confusion", cwd=self.root, env=self.env
        )


def _seed_enterprise_marketplace(config_root: Path) -> None:
    """Register a ``*.ghe.com`` marketplace and a fresh catalog snapshot offline."""
    source = MarketplaceSource(
        name=_MARKETPLACE, owner="corp", repo="marketplace", host="corp.ghe.com"
    )
    (config_root / "marketplaces.json").write_text(
        json.dumps({"marketplaces": [source.to_dict()]}), encoding="utf-8"
    )
    cache = config_root / "cache" / "marketplace"
    cache.mkdir(parents=True)
    catalog = {
        "name": _MARKETPLACE,
        "plugins": [{"name": _PLUGIN, "source": {"type": "github", "repo": _BARE_REPO}}],
    }
    key = _cache_key(source)
    (cache / f"{key}.json").write_text(json.dumps(catalog), encoding="utf-8")
    (cache / f"{key}.meta.json").write_text(
        json.dumps({"fetched_at": time.time(), "ttl_seconds": 3600}), encoding="utf-8"
    )


def _world(tmp_path: Path, binary: Path) -> _World:
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    _seed_enterprise_marketplace(isolated.config_root)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    attacker = repositories.create("attacker-tools")
    skill = attacker.worktree / "skills" / "pwned" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: pwned\ndescription: attacker\n---\n# pwned\n", encoding="utf-8")
    repositories.tag(attacker, "v1.0.0", repositories.commit(attacker, message="stage"))
    return _World(
        root=isolated.work_root / "consumer",
        env=repositories.url_rewrite_subprocess_env_many(((attacker, _ATTACKER_REMOTE),)),
        runner=ApmLifecycleRunner((str(binary),), timeout_seconds=120),
    )


def _arrange(world: _World, path: str) -> tuple[str, ...]:
    """Lay out the consumer for *path* and return the command that resolves the entry."""
    if path == "cli":
        world.write_manifest(world.root, [])
        return ("install", f"{_PLUGIN}@{_MARKETPLACE}", *_FLAGS)
    if path == "transitive":
        world.write_manifest(world.root.parent / "middle", [_ENTRY])
        world.write_manifest(world.root, ["../middle"])
        return ("install", *_FLAGS)
    world.write_manifest(world.root, [_ENTRY])
    if path == "update":
        return ("update", "--yes", *_FLAGS)
    return ("install", *_FLAGS)


@pytest.mark.parametrize("path", ["cli", "apm-yml", "transitive", "update"])
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
    assert f"'corp.ghe.com/{_BARE_REPO}'" in output
    assert f"'github.com/{_BARE_REPO}'" in output
    assert (world.root / "apm.yml").read_bytes() == manifest_before
    assert not (world.root / "apm.lock.yaml").exists()
    assert not (world.root / "apm_modules" / "acme").exists()
    assert not list(world.root.rglob("pwned"))
