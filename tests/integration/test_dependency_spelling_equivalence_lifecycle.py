"""One package under every spelling, and conflicting diamonds fail closed (#3649).

The same Git package declared as shorthand, HTTPS URL, SCP-style SSH URL or a
``git:`` object is one lock entry: switching spellings never churns
apm.lock.yaml, a cold re-materialization through the SSH spelling locks the
same bytes, and uninstall accepts a different spelling than the one
declared.  A diamond whose parents pin one child to two different tags is
rejected before anything is written; aligning the refs then installs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import (
    NO_POLICY,
    World,
    remote,
    set_deps,
    write,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_SPELLINGS: tuple[object, ...] = (
    "https://github.com/test/c.git",
    "git@github.com:test/c.git",
    {"git": "https://github.com/test/c.git"},
    "test/c",
)


def _route_ssh(world: World, name: str) -> None:
    """Route the SCP-style SSH spelling of *name* to its local fixture."""
    env = dict(world.env)
    index = int(env["GIT_CONFIG_COUNT"])
    env[f"GIT_CONFIG_KEY_{index}"] = f"url.{world.repos[name].file_url}/.insteadOf"
    env[f"GIT_CONFIG_VALUE_{index}"] = f"git@github.com:test/{name}.git"
    env["GIT_CONFIG_COUNT"] = str(index + 1)
    world.env = env


def test_spellings_share_one_lock_entry(tmp_path: Path, apm_binary_path: Path) -> None:
    world = World.create(tmp_path, apm_binary_path)
    world.publish("e")
    world.publish("c", [remote("e")])
    _route_ssh(world, "c")
    world.manifest(["test/c"])
    world.apm("install", NO_POLICY)
    lock_before = world.lock_bytes()
    world.cross_check(present={"test/c", "test/e"})

    for spelling in _SPELLINGS:
        world.manifest([spelling])
        world.apm("install", NO_POLICY)
        assert world.lock_bytes() == lock_before, spelling
        world.cross_check(present={"test/c", "test/e"})

    # Cold re-materialization through the SSH spelling locks the same bytes.
    world.manifest(["git@github.com:test/c.git"])
    shutil.rmtree(world.project / "apm_modules")
    world.clean_cache()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before
    assert world.modules() == {"test/c", "test/e"}

    world.apm("uninstall", "https://github.com/test/c")
    assert world.deployed() == set()
    assert not world.modules()


def test_conflicting_diamond_fails_closed_then_converges(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = World.create(tmp_path, apm_binary_path)
    source = world.publish("c")
    rules = source / ".apm" / "instructions" / "c.instructions.md"
    releases = {}
    for tag in ("v1.0.0", "v2.0.0"):
        write(rules, f"---\napplyTo: '**/*.py'\n---\nc {tag}\n")
        releases[tag] = world.factory.commit(world.repos["c"], message=tag)
        world.factory.tag(world.repos["c"], tag, releases[tag])
    world.publish("a", ["test/c#v1.0.0"])
    world.publish("b", ["test/c#v2.0.0"])
    world.manifest(["test/a", "test/b"])

    result = world.apm("install", NO_POLICY, expect=1)
    assert "incompatible immutable requirements" in result.stdout + result.stderr
    assert not (world.project / "apm.lock.yaml").exists()
    assert world.deployed() == set()

    # Aligning the parent's ref converges in one run and is then a no-op.
    set_deps(world.repos["b"].worktree, ["test/c#v1.0.0"])
    world.republish("b")
    world.apm("install", NO_POLICY)
    assert world.locked()["test/c"]["resolved_commit"] == releases["v1.0.0"].sha
    deployed = world.project / ".github/instructions/c.instructions.md"
    assert deployed.read_text(encoding="ascii").endswith("c v1.0.0\n")
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before
    world.cross_check(present={"test/a", "test/b", "test/c"})
