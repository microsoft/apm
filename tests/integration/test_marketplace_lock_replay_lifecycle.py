"""Real-CLI lock-first replay of object-form marketplace dependencies (#3911).

A locked ``{name, marketplace}`` entry is replayed from ``apm.lock.yaml``:
plain ``apm install`` and cold ``apm install --frozen`` restore the locked
package with the catalog unreachable and no registration, and a changed
catalog selection only moves the package on ``apm update``. Servers defined
inline in the catalog entry are the one exception: the lock cannot rebuild
them, so a cold copy of such a package re-resolves through the catalog.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_NAME = "replay-skill"
_CATALOG = "acme"
_PACKAGE_REMOTE = "https://git.example.invalid/team/replay-package"
_CATALOG_REMOTE = "https://git.example.invalid/team/replay-catalog"
_MID_REMOTE = "https://git.example.invalid/team/mid-package"
_TARGET = "copilot"
_INSTALL = ("install", "--target", _TARGET, "--no-policy", "--parallel-downloads", "0")


_MCP_SERVERS = {"replay-server": {"command": "echo", "args": ["replay"]}}
_LSP_SERVERS = {"replay-server": {"command": "echo", "extensionToLanguage": {".rpl": "replay"}}}


def _skill(body: str) -> str:
    return f"---\nname: {_NAME}\ndescription: Replay fixture\n---\n# {body}\n"


@dataclass
class _World:
    repositories: LocalGitRepositoryFactory
    catalog: LocalGitRepository
    second: GitCommit
    root: Path
    env: dict[str, str]
    runner: ApmLifecycleRunner
    servers: tuple[str, dict[str, object]] | None

    @property
    def deployed(self) -> Path:
        return self.root / ".agents" / "skills" / _NAME / "SKILL.md"

    @property
    def lock_bytes(self) -> bytes:
        return (self.root / "apm.lock.yaml").read_bytes()

    def run(self, *args: str, expected: int = 0) -> CommandResult:
        result = self.runner.run(
            args, scenario_id="marketplace-lock-replay", cwd=self.root, env=self.env
        )
        assert result.returncode == expected, (
            f"{result.command!r}\n{result.stdout}\n{result.stderr}"
        )
        return result

    def select(self, ref: str) -> None:
        plugin: dict[str, object] = {
            "name": _NAME,
            "source": {"source": "url", "url": _PACKAGE_REMOTE, "ref": ref},
        }
        if self.servers:
            field, servers = self.servers
            plugin[field] = servers
        (self.catalog.worktree / "marketplace.json").write_text(
            json.dumps({"name": _CATALOG, "owner": {"name": "APM Test"}, "plugins": [plugin]}),
            encoding="utf-8",
        )
        self.repositories.commit(self.catalog, message=f"select {ref}")

    def block_catalog(self) -> Path:
        blocked = self.catalog.origin.with_name("blocked-catalog.git")
        self.catalog.origin.rename(blocked)
        return blocked

    def remove_materialized(self) -> None:
        for path in (self.root / "apm_modules", self.root / ".agents"):
            shutil.rmtree(path)

    def locked_row(self) -> dict:
        rows = load_yaml(self.root / "apm.lock.yaml")["dependencies"]
        (row,) = (row for row in rows if row.get("discovered_via") == _CATALOG)
        return row


def _world(
    tmp_path: Path,
    binary: Path,
    *,
    servers: tuple[str, dict[str, object]] | None = None,
    transitive: bool = False,
) -> _World:
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    package = repositories.create("replay-package")
    skill_path = package.worktree / "skills" / _NAME / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(_skill("one"), encoding="utf-8")
    repositories.tag(package, "v1.0.0", repositories.commit(package, message="v1"))
    skill_path.write_text(_skill("two"), encoding="utf-8")
    second = repositories.commit(package, message="v2")
    repositories.tag(package, "v2.0.0", second)
    catalog = repositories.create("replay-catalog")
    declaration: dict[str, object] = {"name": _NAME, "marketplace": _CATALOG}
    rewrites = [(package, _PACKAGE_REMOTE), (catalog, _CATALOG_REMOTE)]
    if transitive:
        # A locked package whose own apm.yml declares the marketplace entry.
        mid = repositories.create("mid-package")
        dump_yaml(
            {"name": "mid-package", "version": "1.0.0", "dependencies": {"apm": [declaration]}},
            mid.worktree / "apm.yml",
        )
        repositories.commit(mid, message="declare marketplace dependency")
        rewrites.append((mid, _MID_REMOTE))
        declaration = {"git": _MID_REMOTE}
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=(declaration,), targets=(_TARGET,)
    )
    world = _World(
        repositories=repositories,
        catalog=catalog,
        second=second,
        root=consumer.root,
        env=repositories.url_rewrite_subprocess_env_many(tuple(rewrites)),
        runner=ApmLifecycleRunner((str(binary),), timeout_seconds=120),
        servers=servers,
    )
    world.select("v1.0.0")
    world.run("marketplace", "add", _CATALOG_REMOTE, "--name", _CATALOG)
    world.run(*_INSTALL)
    locked = world.locked_row()
    assert locked["resolved_ref"] == "v1.0.0"
    assert locked["discovered_via"] == _CATALOG
    assert world.deployed.read_text(encoding="utf-8") == _skill("one")
    return world


def test_locked_marketplace_entry_replays_without_catalog_until_update(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Install and cold frozen install replay the lock; only update follows the catalog."""
    world = _world(tmp_path, apm_binary_path)
    lock_bytes = world.lock_bytes

    # The catalog now selects v2 and its fresh snapshot is cached; then the
    # catalog origin becomes unreachable.
    world.select("v2.0.0")
    world.run("marketplace", "update", _CATALOG)
    blocked = world.block_catalog()
    world.run(*_INSTALL)
    assert world.lock_bytes == lock_bytes
    assert world.deployed.read_text(encoding="utf-8") == _skill("one")

    # Cold runner: no registration, no catalog cache, no apm_modules, no deployed files.
    world.run("marketplace", "remove", _CATALOG, "--yes")
    world.remove_materialized()
    world.run(*_INSTALL, "--frozen")
    assert world.lock_bytes == lock_bytes
    assert world.deployed.read_text(encoding="utf-8") == _skill("one")
    world.run(*_INSTALL)
    assert world.lock_bytes == lock_bytes

    # Only an explicit update follows the catalog's new selection.
    blocked.rename(world.catalog.origin)
    world.run("marketplace", "add", _CATALOG_REMOTE, "--name", _CATALOG)
    world.run("update", "--yes", "--target", _TARGET, "--parallel-downloads", "0")
    updated = world.locked_row()
    assert updated["resolved_ref"] == "v2.0.0"
    assert updated["resolved_commit"] == world.second.sha
    assert world.deployed.read_text(encoding="utf-8") == _skill("two")


@pytest.mark.parametrize(
    ("field", "servers", "lock_key", "config"),
    [
        ("mcpServers", _MCP_SERVERS, "mcp_servers", ".mcp.json"),
        ("lspServers", _LSP_SERVERS, "lsp_servers", ".github/lsp.json"),
    ],
)
def test_catalog_defined_servers_replay_warm_and_reresolve_cold(
    tmp_path: Path,
    apm_binary_path: Path,
    field: str,
    servers: dict[str, object],
    lock_key: str,
    config: str,
) -> None:
    """Inline catalog servers replay from a warm copy; a cold copy needs the catalog."""
    world = _world(tmp_path, apm_binary_path, servers=(field, servers))
    lock = load_yaml(world.root / "apm.lock.yaml")
    assert set(lock[lock_key]) == set(servers)
    server_config = world.root / config
    assert "replay-server" in server_config.read_text(encoding="utf-8")
    lock_bytes = world.lock_bytes

    blocked = world.block_catalog()
    world.run(*_INSTALL)
    assert world.lock_bytes == lock_bytes
    assert "replay-server" in server_config.read_text(encoding="utf-8")

    # Only the catalog holds the server definitions a cold copy needs.
    world.remove_materialized()
    failed = world.run(*_INSTALL, "--frozen", expected=1)
    assert "Failed to fetch marketplace 'acme'" in " ".join(failed.stdout.split())
    blocked.rename(world.catalog.origin)
    world.run(*_INSTALL, "--frozen")
    assert world.lock_bytes == lock_bytes
    assert world.deployed.read_text(encoding="utf-8") == _skill("one")
    deployed_config = server_config.read_bytes()
    assert b"replay-server" in deployed_config

    # A catalog that changes the inline server under the same selection
    # cannot reach a cold frozen install: the locked content hash refuses it.
    world.servers = (field, {"replay-server": {**servers["replay-server"], "command": "false"}})
    world.select("v1.0.0")
    world.remove_materialized()
    world.run(*_INSTALL, "--frozen", expected=1)
    assert world.lock_bytes == lock_bytes
    assert server_config.read_bytes() == deployed_config


def test_transitive_marketplace_entry_replays_cold_and_frozen_refuses_unlocked(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A package's own marketplace declaration replays too; --frozen never asks the catalog."""
    world = _world(tmp_path, apm_binary_path, transitive=True)
    lock_bytes = world.lock_bytes
    world.block_catalog()
    world.run("marketplace", "remove", _CATALOG, "--yes")
    world.remove_materialized()
    world.run(*_INSTALL, "--frozen")
    assert world.lock_bytes == lock_bytes
    assert world.deployed.read_text(encoding="utf-8") == _skill("one")

    lock_path = world.root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["dependencies"] = [
        row for row in lock["dependencies"] if row.get("discovered_via") != _CATALOG
    ]
    dump_yaml(lock, lock_path)
    world.remove_materialized()
    failed = world.run(*_INSTALL, "--frozen", expected=1)
    output = " ".join((failed.stdout + failed.stderr).split())
    assert "--frozen replays marketplace dependencies from apm.lock.yaml only" in output
    assert "required by" in output
