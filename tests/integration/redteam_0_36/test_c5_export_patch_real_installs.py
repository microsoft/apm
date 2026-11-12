"""C5: `apm export-patch` against real installs of real package shapes (#2162).

The contract (docs/reference/cli/export-patch.md): one ``git apply``-ready
patch per package, applied "from the package repository root, checked out at
the base" in the header, and the run never modifies the project tree,
``apm.lock.yaml``, ``apm.yml``, or ``apm_modules/``.

The shipped e2e test fabricates ``apm_modules`` by hand; these scenarios drive
the real install so the package source tree is whatever APM really caches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.redteam_0_36._c5_support import (
    C5World,
    output,
    remote_url,
    skill_md,
    tree_bytes,
    write,
)
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _git_apply_check(world: C5World, tmp: Path, name: str, commit: str, patch: Path) -> None:
    """Apply *patch* from the package repository root at *commit*, as the header says."""
    clone = tmp / f"apply-{name}"
    world.git(tmp, "clone", "-q", remote_url(name), str(clone))
    world.git(clone, "checkout", "-q", commit)
    applied = world.git(clone, "apply", "--check", "-v", str(patch), check=False)
    assert applied.returncode == 0, (
        f"git apply from repo root failed for {name}:\n{applied.stdout}\n{applied.stderr}\n"
        f"patch:\n{patch.read_text()}"
    )


def _build_world(tmp_path: Path, binary: Path) -> tuple[C5World, dict[str, str]]:
    world = C5World.create(tmp_path, binary)
    sources = world.isolated.package_root

    mono = sources / "mono"
    write(mono / "packages" / "tool" / "apm.yml", "name: tool\nversion: 1.0.0\ndescription: t\n")
    write(mono / "packages" / "tool" / "skills" / "tool-skill" / "SKILL.md", skill_md("tool-skill"))
    write(mono / "README.md", "monorepo\n")
    _, mono_commit = world.publish("mono", mono)

    plugin = sources / "rawplug"
    write(plugin / ".claude-plugin" / "plugin.json", '{"name": "rawplug", "version": "0.4.0"}\n')
    write(plugin / "skills" / "rx" / "SKILL.md", skill_md("rx", "body\n"))
    write(plugin / "agents" / "ra.md", "---\nname: ra\ndescription: agent\n---\n# ra\n")
    _, plugin_commit = world.publish("rawplug", plugin)

    return world, {"mono": mono_commit.sha, "rawplug": plugin_commit.sha}


def test_export_patch_subdirectory_package_applies_from_repo_root(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, commits = _build_world(tmp_path, apm_binary_path)
    project = world.consumer(
        "consumer-subdir",
        targets=("claude",),
        deps=({"git": remote_url("mono"), "path": "packages/tool", "ref": "main"},),
    )
    world.apm(project, "install", "--no-policy")
    deployed = project / ".claude" / "skills" / "tool-skill" / "SKILL.md"
    assert deployed.is_file()
    deployed.write_text(deployed.read_text() + "local improvement\n")

    before_project = ArtifactSnapshot.capture(project)
    before_home = world.home_snapshot()
    result = world.apm(project, "export-patch", "-o", str(tmp_path / "out"))
    assert_unchanged(before_project, ArtifactSnapshot.capture(project))
    assert_unchanged(before_home, world.home_snapshot())

    patches = sorted((tmp_path / "out").glob("*.patch"))
    assert len(patches) == 1, output(result)
    text = patches[0].read_text()
    assert f"base: commit {commits['mono']}" in text
    assert "+local improvement" in text
    _git_apply_check(world, tmp_path, "mono", commits["mono"], patches[0])


def test_export_patch_claude_plugin_package_exports_upstream_files(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A marketplace-style Claude plugin (no apm.yml) must export, not report ambiguity."""
    world, commits = _build_world(tmp_path, apm_binary_path)
    project = world.consumer(
        "consumer-plugin",
        targets=("claude",),
        deps=({"git": remote_url("rawplug"), "ref": commits["rawplug"]},),
    )
    world.apm(project, "install", "--no-policy")
    skill = project / ".claude" / "skills" / "rx" / "SKILL.md"
    assert skill.is_file()
    skill.write_text(skill.read_text() + "plugin edit\n")

    before_project = tree_bytes(project)
    result = world.apm(project, "export-patch", "-o", str(tmp_path / "out"))
    assert tree_bytes(project) == before_project
    combined = output(result)
    assert "ambiguous source" not in combined, combined

    patches = sorted((tmp_path / "out").glob("*.patch"))
    assert len(patches) == 1, combined
    text = patches[0].read_text()
    assert "+++ b/skills/rx/SKILL.md" in text, text
    assert ".apm/" not in text.split("# base")[1], text
    _git_apply_check(world, tmp_path, "rawplug", commits["rawplug"], patches[0])

    # Closed loop: once upstream carries the edit, a re-run has nothing to export
    # for this file, and a second export run is byte-identical (deterministic).
    again = world.apm(project, "export-patch", "-o", str(tmp_path / "out2"))
    assert sorted(p.read_bytes() for p in (tmp_path / "out2").glob("*.patch")) == [
        patches[0].read_bytes()
    ], output(again)


def test_export_patch_two_packages_and_install_hint_then_overwrite(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Edits in 2 packages -> 2 patches; audit and install point at export-patch."""
    world, commits = _build_world(tmp_path, apm_binary_path)
    project = world.consumer(
        "consumer-two",
        targets=("claude",),
        deps=(
            {"git": remote_url("mono"), "path": "packages/tool", "ref": "main"},
            {"git": remote_url("rawplug"), "ref": commits["rawplug"]},
        ),
    )
    world.apm(project, "install", "--no-policy")
    lock_before = (project / "apm.lock.yaml").read_bytes()
    for rel in (".claude/skills/tool-skill/SKILL.md", ".claude/skills/rx/SKILL.md"):
        target = project / rel
        target.write_text(target.read_text() + "two-package edit\n")

    audit = world.apm(project, "audit", "--ci", "--no-policy", expect=1)
    assert "apm export-patch" in output(audit)

    world.apm(project, "export-patch", "-o", str(tmp_path / "out"))
    patches = sorted((tmp_path / "out").glob("*.patch"))
    assert len(patches) == 2, [p.name for p in patches]
    assert (project / "apm.lock.yaml").read_bytes() == lock_before

    install = world.apm(project, "install", "--no-policy")
    assert "apm export-patch" in output(install)
    assert (project / "apm.lock.yaml").read_bytes() == lock_before
