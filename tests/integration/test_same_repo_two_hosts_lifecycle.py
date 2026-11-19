"""The same owner/repo from two hosts cannot share one apm_modules directory (#3696).

The apm_modules layout is host-blind, so ``test/tool`` from github.com and from
another host would both install to ``apm_modules/test/tool``. Install and update
reject the combination before anything is fetched, naming both declarations
(and the declaring parent for a transitive one), instead of failing on a
staging race. Giving one declaration its own ``alias:`` installs both; the same alias on
two different packages is rejected the same way. Another port on the same host is
another server, so it is a different package too (#3927).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import (
    GENERIC_HOST,
    NO_POLICY,
    World,
    remote,
    set_deps,
    write_package,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_OTHER = f"{GENERIC_HOST}/test/tool"
_PORTED_HOST = "github.com:8443"
_SHARED = "would both install to apm_modules/test/tool"


def _world(tmp_path: Path, apm_binary_path: Path, other_host: str = GENERIC_HOST) -> World:
    world = World.create(tmp_path, apm_binary_path)
    world.publish("tool")
    world.publish("mid")
    source = world.isolated.package_root / "tool-other"
    write_package(source, "tool-other")
    other = world.factory.create("tool-other", source_tree=source)
    world.factory.commit(other, message="publish tool on another host")
    world.env = world.factory.url_rewrite_subprocess_env_many(
        [(repo, remote(name)) for name, repo in world.repos.items()]
        + [(other, remote("tool", other_host))]
    )
    return world


def _output(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


def test_same_repo_from_two_hosts_is_rejected_before_fetch(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)
    world.manifest(["test/tool"])
    world.apm("install", NO_POLICY)
    lock_before = world.lock_bytes()
    modules_before = world.modules()

    world.manifest(["test/tool", _OTHER])
    for command, code in (
        (("install", NO_POLICY), 1),
        (("update", "--yes"), 1),
        # Dry-run reports resolution errors as an incomplete preview.
        (("install", "--dry-run", "--verbose", NO_POLICY), 0),
    ):
        output = _output(world.apm(*command, expect=code))
        assert f"github.com/test/tool and {_OTHER} are different packages" in output, command
        assert _SHARED in output, command
        assert "replacement in progress" not in output, command
        assert world.lock_bytes() == lock_before
        assert world.modules() == modules_before
    assert world.deployed() == {"tool.instructions.md"}


def test_transitive_declaration_names_its_parent(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _world(tmp_path, apm_binary_path)
    world.manifest(["test/tool", "test/mid"])
    world.apm("install", NO_POLICY)
    lock_before = world.lock_bytes()
    modules_before = world.modules()
    expected = f"github.com/test/tool and {_OTHER} (required by test/mid) are different packages"

    # A new release of the parent pulls the same owner/repo from another host.
    set_deps(world.repos["mid"].worktree, [_OTHER])
    world.republish("mid", message="depend on tool from another host")
    output = _output(world.apm("update", "--yes", expect=1))
    assert expected in output
    assert _SHARED in output
    assert world.lock_bytes() == lock_before
    assert world.modules() == modules_before

    # A fresh install of the same graph is rejected before anything is written.
    (world.project / "apm.lock.yaml").unlink()
    shutil.rmtree(world.project / "apm_modules")
    output = _output(world.apm("install", NO_POLICY, expect=1))
    assert expected in output
    assert world.modules() == set()
    assert not (world.project / "apm.lock.yaml").exists()


def test_alias_installs_both_packages(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _world(tmp_path, apm_binary_path)
    # The same alias on two different packages is one shared directory too.
    world.manifest(
        [
            {"git": "test/tool", "alias": "shared"},
            {"git": f"https://{_OTHER}.git", "alias": "shared"},
        ]
    )
    output = _output(world.apm("install", NO_POLICY, expect=1))
    assert "would both install to apm_modules/shared" in output
    assert world.modules() == set()
    assert not (world.project / "apm.lock.yaml").exists()

    world.manifest(["test/tool", {"git": f"https://{_OTHER}.git", "alias": "tool-other"}])

    world.apm("install", NO_POLICY)
    assert world.modules() == {"test/tool", "tool-other"}
    assert world.deployed() == {"tool.instructions.md", "tool-other.instructions.md"}
    lock_after = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_after


def test_same_repo_on_another_port_is_a_different_package(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path, other_host=_PORTED_HOST)
    ported = remote("tool", _PORTED_HOST)

    world.manifest(["test/tool", ported])
    output = _output(world.apm("install", NO_POLICY, expect=1))
    assert f"github.com/test/tool and {_PORTED_HOST}/test/tool are different packages" in output
    assert _SHARED in output
    assert world.modules() == set()
    assert not (world.project / "apm.lock.yaml").exists()

    world.manifest(["test/tool", {"git": ported, "alias": "tool-other"}])
    world.apm("install", NO_POLICY)
    assert world.modules() == {"test/tool", "tool-other"}
    assert world.deployed() == {"tool.instructions.md", "tool-other.instructions.md"}
    entries = {(dep.get("host"), dep.get("port")) for dep in world.lock()["dependencies"]}
    assert {("github.com", None), ("github.com", 8443)} <= entries
    lock_after = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_after
