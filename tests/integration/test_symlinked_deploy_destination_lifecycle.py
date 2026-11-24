"""A deploy destination reached through a symlinked directory is refused before any write.

``apm install`` refuses to deploy through a symlinked skills directory (#5156).
The existing cells cover a link that points outside the root (project scope in
tests/lifecycle/scenarios/test_platform.py, user scope in
test_claude_config_dir_home_skill_lifecycle.py). These cells add the link kinds
that resolve nowhere: a dangling link and a link loop, at project and user
scope. Each install exits 1, writes nothing through the link and leaves the
project, the user root and the lockfile unchanged. Every scope and link kind
prints the same refusal, naming the link and the next step (#5287).
Owners: integration/targets.py (_validate_project_target_root) and
install/deployed_paths.py (deployed_path_entry).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._install_audit_replay_kit import ReplayWorld, tree_digest
from tests.integration.test_claude_config_dir_home_skill_lifecycle import _World
from tests.integration.test_uninstall_local_reanchor_lifecycle import _manifest, _skill_package

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
    pytest.mark.skipif(os.name == "nt", reason="needs an unprivileged symlink"),
]

_REMEDY = "Replace the symlink with a regular directory or remove it, then retry."


def _project(tmp_path: Path, apm_binary_path: Path, kind: str) -> tuple[ReplayWorld, Path]:
    """A project whose .claude/skills is a *kind* link; return it and the link's target."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _skill_package(world.project / "vendor" / "e", "e")
    _manifest(world, ["./vendor/e"], [])
    (world.project / ".claude").mkdir()
    outside = world.project.parent / "outside-skills"
    if kind == "outside":
        outside.mkdir()
    link = world.project / ".claude" / "skills"
    link.symlink_to("skills" if kind == "loop" else outside, target_is_directory=True)
    return world, outside


def _user(tmp_path: Path, apm_binary_path: Path, kind: str) -> tuple[_World, Path]:
    """The #5156 world (CLAUDE_CONFIG_DIR at HOME) with a *kind* skills link."""
    world = _World(tmp_path, apm_binary_path)
    outside = world.root / "outside"
    if kind == "outside":
        outside.mkdir()
    link = world.home / "skills"
    link.symlink_to("skills" if kind == "loop" else outside, target_is_directory=True)
    return world, outside


def _squeezed(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


@pytest.mark.parametrize("kind", ["dangling", "loop"])
def test_install_refuses_a_broken_project_skills_link_before_any_write(
    kind: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    world, outside = _project(tmp_path, apm_binary_path, kind)
    before = tree_digest(world.project)

    result = world.apm("install", expect=None)
    text = _squeezed(result)

    assert result.returncode == 1, text
    assert "Traceback" not in text and "This is a bug" not in text, text
    assert (
        f"Refusing deployment through a symlinked path component: .claude/skills. {_REMEDY}" in text
    )
    assert not outside.exists(), "wrote through the link"
    assert not (world.project / "apm.lock.yaml").exists()
    assert tree_digest(world.project) == before


@pytest.mark.parametrize("kind", ["dangling", "loop"])
def test_install_g_refuses_a_broken_skills_link_before_any_write(
    kind: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    world, outside = _user(tmp_path, apm_binary_path, kind)
    before = world.state()

    result = world.install()
    text = _squeezed(result)

    assert result.returncode == 1, text
    assert "Traceback" not in text and "This is a bug" not in text, text
    if kind == "dangling":  # #5174's standard refusal already covers this kind
        assert f"Refusing deployment through a symlinked path component: skills. {_REMEDY}" in text
    assert world.state() == before, text
    assert (world.home / "skills").is_symlink() and not outside.exists()
    assert not (world.manifest.parent / "apm.lock.yaml").exists()


@pytest.mark.parametrize(
    ("scope", "kind"),
    [("project", "outside"), ("project", "dangling"), ("project", "loop"), ("user", "loop")],
)
def test_a_symlinked_deploy_refusal_names_the_link_and_next_step(
    scope: str, kind: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    """One refusal whatever the scope or link kind: the link, then the next step (#5174)."""
    if scope == "project":
        world, _outside = _project(tmp_path, apm_binary_path, kind)
        result, link = world.apm("install", expect=None), ".claude/skills"
    else:
        world, _outside = _user(tmp_path, apm_binary_path, kind)
        result, link = world.install(), "skills"
    text = _squeezed(result)

    standard = f"Refusing deployment through a symlinked path component: {link}. {_REMEDY}"
    assert result.returncode == 1, text
    assert standard in text, f"nonstandard symlink refusal: {text}"
