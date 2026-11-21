"""`apm view --file PATH` reverse provenance (#3608) holds against a real install (#3649).

Promises (CHANGELOG / `apm view --help`): read-only exact-path lookup; exit 0
when provenance is recorded, 1 when unrecorded or an owner is unresolved, 2 on
usage or state errors; relative and absolute paths both name the same file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    output,
    remote_url,
    skill_md,
    tree_bytes,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_SKILL = ".claude/skills/vf/SKILL.md"
_AGENT = ".claude/agents/vfa.md"


def _installed(tmp_path: Path, binary: Path) -> tuple[MarketplacePackWorld, Path]:
    world = MarketplacePackWorld.create(tmp_path, binary)
    pkg = world.isolated.package_root / "viewpkg"
    write(pkg / "apm.yml", "name: viewpkg\nversion: 2.1.0\ndescription: v\n")
    write(pkg / "skills" / "vf" / "SKILL.md", skill_md("vf"))
    write(pkg / ".apm" / "agents" / "vfa.agent.md", "---\ndescription: a\n---\n# vfa\n")
    world.publish("viewpkg", pkg)
    project = world.consumer(
        "consumer-view",
        targets=("claude",),
        deps=({"git": remote_url("viewpkg"), "ref": "main"},),
    )
    world.apm(project, "install", "--no-policy")
    assert (project / _SKILL).is_file()
    assert (project / _AGENT).is_file()
    return world, project


def test_view_file_matrix_is_read_only_and_exit_codes_hold(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, project = _installed(tmp_path, apm_binary_path)
    write(project / "notes.md", "user owned\n")
    before = tree_bytes(project)

    skill = world.apm(project, "view", "--file", _SKILL)
    assert "viewpkg" in output(skill) and "matches the recorded deployed hash" in output(skill)
    world.apm(project, "view", "--file", _AGENT)
    world.apm(project, "view", "--file", "./.claude/skills/../skills/vf/SKILL.md")
    world.apm(project, "view", "--file", str(project / _SKILL))

    world.apm(project, "view", "--file", "notes.md", expect=1)
    world.apm(project, "view", "--file", "apm.yml", expect=1)
    world.apm(project, "view", "--file", "missing/never.md", expect=1)
    world.apm(project, "view", "--file", str(tmp_path / "elsewhere.md"), expect=2)
    world.apm(project, "view", "--file", "", expect=2)
    world.apm(project, "view", "--file", ".", expect=2)
    assert tree_bytes(project) == before

    edited = project / _SKILL
    edited.write_text(edited.read_text() + "edit\n")
    modified = world.apm(project, "view", "--file", _SKILL)
    assert "modified since deployment" in output(modified)


def test_view_file_accepts_logical_absolute_path_through_symlinked_cwd(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """`apm view --file "$PWD/<file>"` from a symlinked directory (macOS /tmp,
    /var, symlinked home or workspace dirs) must name the same deployed file."""
    world, project = _installed(tmp_path, apm_binary_path)
    link = tmp_path / "linked-workspace"
    os.symlink(project, link, target_is_directory=True)

    logical = f"{link}/{_SKILL}"
    result = world.apm(link, "view", "--file", logical, expect=None)
    assert result.returncode == 0, output(result)
    assert "viewpkg" in output(result)
    assert "outside the project deploy root" not in output(result)
