"""Local-path packages with unresolvable symbolic links fail closed (#3649).

Install copies a local package into apm_modules and dereferences links whose
target stays inside the package.  A link that escapes the package root or
points at a missing file aborts the install with nothing written: no
lockfile, no deployed files, no apm_modules entry and an unchanged apm.yml.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, write, write_package

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="creates OS symlinks"),
]

_LINKED = ".apm/instructions/linked.instructions.md"
_SHARED = "---\napplyTo: '**'\n---\nshared text\n"


def _local_package(world: World, link_target: str) -> Path:
    package = write_package(world.isolated.work_root / "lp", "lp")
    write(package / "shared" / "s.md", _SHARED)
    write(world.isolated.work_root / "outside.md", "secret\n")
    (package / _LINKED).symlink_to(link_target)
    world.manifest(["../lp"])
    return package


@pytest.mark.parametrize(
    ("target", "message"),
    [("../../../outside.md", "escapes the package root"), ("../../nope.md", "broken")],
    ids=["escaping", "broken"],
)
def test_unresolvable_local_link_aborts_with_nothing_written(
    tmp_path: Path, apm_binary_path: Path, target: str, message: str
) -> None:
    world = World.create(tmp_path, apm_binary_path)
    _local_package(world, target)
    manifest_before = (world.project / "apm.yml").read_bytes()

    result = world.apm("install", NO_POLICY, expect=1)
    assert message in " ".join((result.stdout + result.stderr).split())
    assert not (world.project / "apm.lock.yaml").exists()
    leftovers = [p for p in (world.project / ".github").rglob("*") if p.is_file()]
    assert leftovers == []
    assert not world.modules()
    assert (world.project / "apm.yml").read_bytes() == manifest_before
