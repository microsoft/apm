"""``apm install <bundle>`` honours the consumer's ``apm.yml`` targets (#3649).

A local bundle install never edits ``apm.yml``, but the manifest's declared
targets still restrict where files land, exactly like a declarative install:
``--target`` > ``apm.yml`` targets > ``apm config target`` > auto-detect.
The chain then proves the bundle deployment is stable for ``audit --ci``,
re-install, ``install --frozen`` and a cold clone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._install_audit_replay_kit import (
    NO_POLICY,
    ReplayWorld,
    apm_package,
    lock_bytes,
    tree_digest,
    write,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_DECLARED = (".claude/rules/glob.md", ".cursor/rules/glob.mdc", ".claude/skills/helper/SKILL.md")
_UNDECLARED_ROOTS = (".github", ".codex", ".opencode", ".gemini")


def _packed_bundle(world: ReplayWorld, tmp_path: Path, *, archive: bool) -> Path:
    producer = world.sources / "bundler"
    apm_package(producer, "bundler")
    write(
        producer / "apm.yml",
        "name: bundler\nversion: 1.0.0\ndescription: b\nlicense: MIT\ndependencies:\n  apm: []\n",
    )
    world.apm("install", NO_POLICY, "--target", "claude", cwd=producer)
    out = tmp_path / "out"
    world.apm("pack", "-o", str(out), *(("--archive",) if archive else ()), cwd=producer)
    artifact = out / ("bundler-1.0.0.zip" if archive else "bundler-1.0.0")
    assert artifact.exists()
    return artifact


@pytest.mark.parametrize("archive", [False, True], ids=["directory", "archive"])
def test_bundle_install_lands_only_in_manifest_targets(
    tmp_path: Path, apm_binary_path: Path, archive: bool
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    artifact = _packed_bundle(world, tmp_path, archive=archive)
    project = world.project
    write(project / "apm.yml", "name: proj\nversion: 1.0.0\ntargets: [claude, cursor]\n")
    write(project / ".gitignore", "apm_modules/\n")
    manifest = (project / "apm.yml").read_bytes()

    world.apm("install", str(artifact), NO_POLICY)
    for path in _DECLARED:
        assert (project / path).is_file(), path
    for root in _UNDECLARED_ROOTS:
        assert not (project / root).exists(), root
    assert (project / "apm.yml").read_bytes() == manifest
    world.audit_clean()

    lock = lock_bytes(project)
    tree = tree_digest(project)
    world.apm("install", str(artifact), NO_POLICY)
    world.apm("install", "--frozen", NO_POLICY)
    assert lock_bytes(project) == lock
    assert tree_digest(project) == tree
    world.audit_clean()

    clone = world.cold_clone()
    world.audit_clean(cwd=clone)
    world.apm("install", "--frozen", NO_POLICY, cwd=clone)
    assert world.git(clone, "status", "--porcelain") == ""
    world.audit_clean(cwd=clone)


def test_bundle_install_explicit_target_still_wins(tmp_path: Path, apm_binary_path: Path) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    artifact = _packed_bundle(world, tmp_path, archive=False)
    project = world.project
    write(project / "apm.yml", "name: proj\nversion: 1.0.0\ntargets: [claude, cursor]\n")

    world.apm("install", str(artifact), "--target", "copilot", NO_POLICY)
    assert (project / ".github/agents/rev.agent.md").is_file()
    assert not (project / ".claude").exists()
    assert not (project / ".cursor").exists()
    world.audit_clean()
