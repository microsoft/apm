"""Audit replays a local package with in-package links as install copied it (#3667).

Install copies a local package into apm_modules and dereferences links whose
target stays inside the package.  ``apm audit --ci`` replays through the
same local package image, so a clean install audits clean, editing a link
target is reported as drift and fails ``--frozen``, and the source link
itself is never touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, write, write_package

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="creates OS symlinks"),
]

_LINKED = ".apm/instructions/linked.instructions.md"
_SHARED = "---\napplyTo: '**'\n---\nshared text\n"


def test_contained_link_installs_audits_and_tracks_target_edits(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = World.create(tmp_path, apm_binary_path)
    package = write_package(world.isolated.work_root / "lp", "lp")
    write(package / "shared" / "s.md", _SHARED)
    (package / _LINKED).symlink_to("../../shared/s.md")
    world.manifest(["../lp"])

    world.apm("install", NO_POLICY)
    deployed = world.project / ".github/instructions/linked.instructions.md"
    assert deployed.read_text(encoding="ascii") == _SHARED
    assert not deployed.is_symlink()

    world.apm("audit", "--ci", NO_POLICY)
    assert (package / _LINKED).is_symlink()
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY, "--frozen")
    assert world.lock_bytes() == lock_before

    write(package / "shared" / "s.md", _SHARED + "changed\n")
    world.apm("install", NO_POLICY, "--frozen", expect=1)
    assert world.lock_bytes() == lock_before
    audit = world.apm("audit", "--ci", NO_POLICY, expect=1)
    assert "linked.instructions.md" in audit.stdout

    world.apm("install", NO_POLICY)
    world.apm("audit", "--ci", NO_POLICY)
    world.apm("uninstall", "../lp")
    assert world.deployed() == set()
    assert (package / _LINKED).is_symlink()
