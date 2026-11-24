"""Install keeps primitive writes inside the project when deploy paths are links.

A committed symlink at a primitive deploy directory (``.github/prompts``,
``.claude/commands`` ...) or at a deploy file location must never redirect
``apm install`` output to a directory outside the project. Install refuses
and the link target stays untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._containment_kit import (
    build_containment_kit,
    output,
    snapshot,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows"),
    pytest.mark.e2e,
]


@pytest.mark.parametrize(
    "linked_dir",
    [
        ".github/prompts",
        ".github/instructions",
        ".github/hooks",
        ".claude/agents",
        ".claude/commands",
    ],
)
def test_install_refuses_linked_primitive_directory(
    tmp_path: Path, apm_binary_path: Path, linked_dir: str
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("copilot", "claude"))
    link = kit.project / linked_dir
    link.parent.mkdir(parents=True)
    os.symlink(kit.outside, link, target_is_directory=True)

    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert "symlinked target directory" in output(result)
    assert snapshot(kit.outside) == {}
    assert link.is_symlink()


def test_install_refuses_linked_destination_file(tmp_path: Path, apm_binary_path: Path) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("copilot",))
    hooks = kit.project / ".github/hooks"
    hooks.mkdir(parents=True)
    os.symlink(kit.outside / "created.json", hooks / "pkg-h.json")

    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert "symlinked destination" in output(result)
    assert snapshot(kit.outside) == {}


def test_install_deploys_into_regular_directories(tmp_path: Path, apm_binary_path: Path) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("copilot", "claude"))

    result = kit.run("install", "--no-policy")

    assert result.returncode == 0, output(result)
    assert (kit.project / ".github/prompts/p.prompt.md").is_file()
    assert (kit.project / ".github/hooks/pkg-h.json").is_file()
    assert (kit.project / ".claude/commands/p.md").is_file()


@pytest.mark.parametrize(
    ("linked_root", "subdirs"),
    [
        (".claude", ("agents", "commands")),
        (".github", ("prompts", "instructions", "hooks")),
    ],
)
def test_install_refuses_a_linked_target_root_whose_subdirectories_exist(
    tmp_path: Path, apm_binary_path: Path, linked_root: str, subdirs: tuple[str, ...]
) -> None:
    """An existing deploy directory reached through a linked target root is still refused.

    Install leaves an existing real directory as it is (#5204); reached through
    a linked root, that directory lies outside the project, so the deploy must
    refuse and write nothing there.
    """
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("copilot", "claude"))
    for name in subdirs:
        (kit.outside / name).mkdir(parents=True)
    (kit.outside / subdirs[0] / "keep.md").write_text("# outside\n", encoding="utf-8")
    before = snapshot(kit.outside)
    link = kit.project / linked_root
    os.symlink(kit.outside, link, target_is_directory=True)

    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert "symlink" in output(result).lower(), output(result)
    assert snapshot(kit.outside) == before, output(result)
    assert link.is_symlink()


@pytest.mark.parametrize("linked_dir", [".claude/commands", ".github/prompts"])
def test_install_refuses_a_deploy_directory_linked_inside_the_project(
    tmp_path: Path, apm_binary_path: Path, linked_dir: str
) -> None:
    """A deploy directory linked to another in-project directory is not written through.

    The link resolves inside the project, so containment alone passes; writing
    through it would replace a user file the lock never recorded.
    """
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("copilot", "claude"))
    docs = kit.project / "docs"
    docs.mkdir()
    for name in ("p.md", "p.prompt.md"):
        (docs / name).write_text("# user notes\n", encoding="utf-8")
    before = snapshot(docs)
    link = kit.project / linked_dir
    link.parent.mkdir(parents=True)
    os.symlink(Path("..") / "docs", link)

    result = kit.run("install", "--no-policy")

    assert snapshot(docs) == before, output(result)
    assert result.returncode != 0, output(result)
    assert "symlink" in output(result).lower(), output(result)
    assert link.is_symlink()
